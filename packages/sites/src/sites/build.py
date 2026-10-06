"""Build the Zola sites into the pages site.

Each site is assembled in a temporary directory from its source in the repo
(SITES_SOURCE/<name>/), the shared themes beside it (../themes) and the entries in
SITES_CONTENT/<name>/ (default ~/.local/share/everythingllm/pages/entries, out of the AnythingLLM
container's reach, since only host services read or write entries). It's built straight next to its destination and swapped in at
SITES_OUTPUT/<name>/, so readers never see a half-built site. The output carries a marker
file; a directory without one (a page the sandbox published, the podcasts) is never
replaced. Builds hold a lock on SITES_CONTENT/.build.lock, so sites-runner and a deploy on
the host never overlap.

zola runs with no network (in a user and network namespace of its own, through `unshare`),
so a template can't fetch anything while the site builds (load_data takes URLs), and with
nothing in its environment but PATH and HOME. A build that takes longer than BUILD_SECONDS
is stopped, so it fails before the MCP tool call that asked for it gives up. A machine
without unprivileged user namespaces builds without the namespace; `sandboxed()` says which,
and a Builder's `sandbox` asks it (tests give their own).

sites-runner builds a site after every write or delete; `make deploy` builds
them all through the `sites-build` command, with host paths in the environment.
"""

import argparse
import fcntl
import functools
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import hostrpc

from sites.store import site_url

LOCK = ".build.lock"
MARKER = ".zola-site"  # in every built site; the sandbox won't publish over it
BUILD_SECONDS = 40  # per site; hostrpc's caller gives up after 55
SANDBOX = ("unshare", "--user", "--map-root-user", "--net")

log = logging.getLogger(__name__)
REPO_SITES = (
    Path(__file__).resolve().parents[4] / "zola" / "sites"
)  # <repo>/packages/sites/src/sites/


class BuildError(RuntimeError):
    """A site didn't build; the message says why."""


@functools.cache
def sandboxed() -> bool:
    """Whether zola can run without a network here (unprivileged user namespaces work)."""
    try:
        # subprocess.call, not run: tests stand in for subprocess.run to fake zola.
        ok = (
            subprocess.call(
                [*SANDBOX, "true"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            == 0
        )
    except OSError:
        ok = False
    if not ok:
        log.warning(
            "can't run zola without a network here (%s failed); building without that",
            " ".join(SANDBOX),
        )
    return ok


@dataclass(frozen=True)
class Builder:
    source: Path
    themes: Path
    content: Path
    output: Path
    zola: str
    sandbox: Callable[[], bool] = sandboxed  # whether zola runs without a network

    @classmethod
    def from_env(cls) -> "Builder":
        """Paths from the environment, with the host's defaults: builds run on the host, from
        entries in hostrpc.data_dir()/pages/entries into the pages site (hostrpc.site_dir()), with its zola (tools/machine.py checks it's there),
        from the sites in the repo this package is in."""
        get = os.environ.get
        source = Path(get("SITES_SOURCE", REPO_SITES))
        content = Path(get("SITES_CONTENT", hostrpc.data_dir() / "pages" / "entries"))
        return cls(
            source=source,
            themes=source.parent / "themes",
            content=content,
            output=Path(get("SITES_OUTPUT", hostrpc.site_dir())),
            zola=get("ZOLA", "/usr/local/bin/zola"),
        )

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
        with open(self.content / LOCK, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            for name in names:
                try:
                    built.append(self._build_one(name))
                except (BuildError, OSError) as e:
                    errors.append(str(e))
        if errors:
            raise BuildError("\n".join(errors))
        return built

    def _assemble(self, name: str, tmp: Path) -> Path:
        """The site's source, ready for zola, in `tmp`."""
        src = tmp / name
        shutil.copytree(self.source / name, src)
        shutil.copytree(self.themes, src / "themes", dirs_exist_ok=True)
        entries = self.content / name
        if entries.is_dir():
            # Only entries; section _index.md files come from the repo.
            shutil.copytree(
                entries,
                src / "content",
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns("_index.md", ".*"),
            )
        return src

    def _zola(self, name: str, src: Path, out: Path) -> None:
        """Build `src` into `out`; nothing is left in `out` if it fails."""
        try:
            # No environment beyond PATH: templates (and anything that slips into
            # content) can call get_env, so there must be nothing in it to leak.
            # No network either, for load_data (see the module docstring).
            # The public URL comes from host.env, so zola.toml doesn't name the host.
            base = site_url(self.source, name)
            # Looked up here: under unshare, a missing zola would only be unshare failing.
            zola = shutil.which(self.zola)
            if zola is None:
                raise OSError("not found")
            result = subprocess.run(
                [
                    *(SANDBOX if self.sandbox() else ()),
                    zola,
                    "--root",
                    str(src),
                    "build",
                    "--output-dir",
                    str(out),
                    *(["--base-url", base.rstrip("/")] if base else []),
                ],
                capture_output=True,
                text=True,
                check=False,  # a failed build is reported below, with zola's output
                timeout=BUILD_SECONDS,
                env={
                    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                    "HOME": str(src.parent),
                },
            )
        except subprocess.TimeoutExpired:
            shutil.rmtree(out, ignore_errors=True)
            raise BuildError(
                f"zola build for {name} took longer than {BUILD_SECONDS} s; stopped"
            ) from None
        except OSError as e:
            raise BuildError(f"can't run zola ({self.zola}): {e}") from None
        if result.returncode != 0:
            shutil.rmtree(out, ignore_errors=True)
            raise BuildError(
                f"zola build failed for {name}:\n{(result.stderr or result.stdout).strip()}"
            )

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

        with tempfile.TemporaryDirectory(prefix=f"zola-{name}-") as tmp:
            self._zola(name, self._assemble(name, Path(tmp)), new)
        (new / MARKER).write_text("Built by sites-build; replaced on every build.\n")

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
