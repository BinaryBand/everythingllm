"""Build the Zola sites into the pages site, through the sandbox.

Every site is built by the sandbox runner in a container with no network (its op
build_system_site, through `remote`), from its source in the repo
(SITES_SOURCE/<name>/), the repo's themes and its entries in SITES_CONTENT/<name>/
(default ~/.local/share/everythingllm/pages/entries, out of the AnythingLLM container's
reach, since only host services read or write entries). The sandbox writes the result
next to its destination, SITES_OUTPUT/.<name>.new, and it's swapped in here at
SITES_OUTPUT/<name>/, so readers never see a half-built site. The output carries a
marker file; a directory without one (the link cards) is never replaced. Builds hold a
lock on SITES_CONTENT/.build.lock, so sites-runner and a deploy on the host never overlap.

So zola runs only in the sandbox image; no host and no service container has one. A site
names its theme's origin in its zola.toml (`[extra.build] theme_from`, "system" for the
repo's themes); one that names none is refused with a BuildError that says so. Every repo
site names one; a test holds them to it.

sites-runner builds a site after every write or delete; `uv run hostctl deploy` builds
them all through the `sites-build` command, with host paths in the environment.

Config (environment):
  SITES_SOURCE        repo directory holding one Zola site per subdirectory (default this
                      repo's packages/sites/zola/sites)
  SITES_CONTENT       the entries (default <data dir>/pages/entries)
  SITES_OUTPUT        the pages site's root (default <data dir>/pages/public)
  SANDBOX_BUILD_SOCKET  the sandbox runner's socket for system site builds, which serves
                      nothing else (default <storage>/everythingllm/sandbox-build/runner.sock;
                      on the host, unset and missing, the runner's own socket)
  SANDBOX_SOCKET      the sandbox runner's own socket, that fallback (default
                      <storage>/everythingllm/sandbox/runner.sock)
"""

import argparse
import fcntl
import logging
import os
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import hostrpc
import tomllib

LOCK = ".build.lock"
MARKER = ".zola-site"  # in every built site; the sandbox won't publish over it
BUILD_SECONDS = 40  # per site, the sandbox's limit; hostrpc's caller gives up after 50

log = logging.getLogger(__name__)
REPO_SITES = (
    Path(__file__).resolve().parents[2] / "zola" / "sites"
)  # <repo>/packages/sites/zola/sites; this file is in <repo>/packages/sites/src/sites/


class BuildError(RuntimeError):
    """A site didn't build; the message says why."""


def build_socket() -> Path:
    """The sandbox runner's build socket, or, on the host, while the runner predates it
    (a deploy before `uv run hostctl sandbox-setup`), its own socket, which serves
    build_system_site too. A service container mounts only the first."""
    sock = hostrpc.socket_path("sandbox-build", "SANDBOX_BUILD_SOCKET")
    if not sock.exists() and not os.environ.get("SANDBOX_BUILD_SOCKET"):
        old = hostrpc.socket_path("sandbox", "SANDBOX_SOCKET")
        if old.exists():
            log.warning("no %s yet; building through %s", sock, old)
            return old
    return sock


def sandbox_build(name: str) -> Path:
    """Have the sandbox runner build a theme_from site; where its output went."""
    try:
        result = hostrpc.request_sync(
            build_socket(),
            "build_system_site",
            {"site": name},
            BUILD_SECONDS + 10,
            name="sandbox runner",
        )
    except hostrpc.RunnerError as e:
        raise BuildError(
            f"zola build failed for {name} (in the sandbox): {e}"
        ) from None
    return Path(result["path"])


@dataclass(frozen=True)
class Builder:
    source: Path
    content: Path
    output: Path
    remote: Callable[
        [str], Path
    ]  # builds a site into output/.<name>.new; where it went

    @classmethod
    def from_env(cls) -> "Builder":
        """Paths from the environment, with the host's defaults: entries in
        hostrpc.data_dir()/pages/entries, built by the sandbox into the pages site
        (hostrpc.site_dir()), from the sites in the repo this package is in."""
        get = os.environ.get
        return cls(
            source=Path(get("SITES_SOURCE", REPO_SITES)),
            content=Path(
                get("SITES_CONTENT", hostrpc.data_dir() / "pages" / "entries")
            ),
            output=Path(get("SITES_OUTPUT", hostrpc.site_dir())),
            remote=sandbox_build,
        )

    def theme_from(self, name: str) -> str | None:
        """Where the site's zola.toml takes its theme from, if it says ([extra.build])."""
        conf = tomllib.loads((self.source / name / "zola.toml").read_text())
        origin = conf.get("extra", {}).get("build", {}).get("theme_from")
        return origin if isinstance(origin, str) and origin else None

    def site_names(self) -> list[str]:
        return sorted(
            p.name for p in self.source.iterdir() if (p / "zola.toml").is_file()
        )

    def build(self, *names: str) -> list[Path]:
        """Build the named sites (all when none are named); returns where each went.
        One site failing doesn't stop the others; all failures are raised together after."""
        names = names or tuple(self.site_names())
        self.content.mkdir(parents=True, exist_ok=True)
        built, errors = [], []
        # Opened without following a symlink or truncating: the sites container can
        # write pages/entries, and a symlink here would have this empty any file.
        lock = os.open(
            self.content / LOCK,
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            0o644,
        )
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            for name in names:
                try:
                    built.append(self._build_one(name))
                except (BuildError, OSError) as e:
                    errors.append(str(e))
        finally:
            os.close(lock)
        if errors:
            raise BuildError("\n".join(errors))
        return built

    def _build_one(self, name: str) -> Path:
        if not (self.source / name / "zola.toml").is_file():
            raise BuildError(f"no Zola site '{name}' in {self.source}")
        dest = self.output / name
        if dest.exists() and not (dest / MARKER).exists():
            raise BuildError(
                f"{dest} exists and wasn't built from sites/{name}; not replacing it"
            )
        new, old = self.output / f".{name}.new", self.output / f".{name}.old"
        for stale in (new, old):
            shutil.rmtree(stale, ignore_errors=True)

        if not self.theme_from(name):
            raise BuildError(
                f"{name} can't be built: only the sandbox builds sites, and its zola.toml "
                'names no theme for it; add [extra.build] theme_from = "system"'
            )
        if (went := self.remote(name)) != new:
            shutil.rmtree(went, ignore_errors=True)
            raise BuildError(
                f"the sandbox built {name} into {went}, not {new}: SITES_OUTPUT and "
                "the sandbox runner's SANDBOX_SITE_DIR differ"
            )
        mark(new)

        if dest.exists():
            os.rename(dest, old)
        try:
            os.rename(new, dest)
        except OSError:
            if old.exists():
                os.rename(old, dest)  # put the last good build back
            shutil.rmtree(new, ignore_errors=True)
            raise
        shutil.rmtree(old, ignore_errors=True)
        return dest


def mark(new: Path) -> None:
    """Put MARKER in a fresh build. The pages site is writable by the sites and research
    containers, and host processes build there too, so this follows no
    symlink, neither for the folder nor for the file, and writes over nothing."""
    try:
        folder = os.open(new, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            fd = os.open(
                MARKER,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o644,
                dir_fd=folder,
            )
        finally:
            os.close(folder)
    except OSError as e:
        raise BuildError(f"couldn't mark {new}: {e.strerror or e}") from None
    with os.fdopen(fd, "w") as f:
        f.write("Built by sites-build; replaced on every build.\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("sites", nargs="*", help="sites to build (default: all)")
    args = parser.parse_args()
    builder = Builder.from_env()
    try:
        for dest in builder.build(*args.sites):
            print(f"built {dest.name} -> {dest}")
    except BuildError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
