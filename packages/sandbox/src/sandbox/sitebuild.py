"""Builds a Zola site inside a sandbox build container (see Runner.op_build_site): copies the
site to /tmp, puts the theme its zola.toml names in place, and runs zola into /out/site.

The runner copies this file into the run's read-only /sandbox from the repo, so nothing in
a workspace's folders can change what a build runs. Standard library only: it runs with the
sandbox image's python.

A site picks its theme in zola.toml:

    theme = "agent-site"
    [extra.build]
    theme_from = "system"   # /system/themes, the repo's; or a workspace, for /shared/<it>/themes

Without `theme_from`, the site's own themes/ folder is used as it is.

A theme from another workspace is that workspace's to change, and runs in this one's build,
where this workspace's /project and /work are mounted: zola copies a static file through a
symlink, so a theme's `static/x -> /project` would publish them. So a theme comes in
without its symlinks, and must be a folder in its workspace's /shared, not one reached
through a symlink.

A system site (news, research, status: Runner.op_build_system_site) is its repo source plus
its entries, which stay on the host and come in read-only; they're copied into its content/
the way sites.build assembles it on the host (entries only: the sections' _index.md files
come from the repo).

  python sitebuild.py <site folder> <base url> [<entries folder>]
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import tomllib

NAME_RE = re.compile(r"^[a-z0-9_][a-z0-9_-]{0,99}$")  # theme and workspace names
SYSTEM = Path("/system/themes")
SHARED = Path("/shared")
OUT = Path("/out/site")
WORK = Path("/tmp/site")


class BuildError(Exception):
    """Why a site can't be built, for the agent."""


def theme_source(
    conf: dict, system: Path = SYSTEM, shared: Path = SHARED
) -> Path | None:
    """The theme folder the site's zola.toml asks for, or None to use the site's own."""
    origin = conf.get("extra", {}).get("build", {}).get("theme_from")
    if origin is None:
        return None
    theme = conf.get("theme")
    if not isinstance(theme, str) or not NAME_RE.fullmatch(theme):
        raise BuildError(
            "zola.toml's theme must be a theme's folder name, e.g. 'agent-site'"
        )
    if not isinstance(origin, str) or not NAME_RE.fullmatch(origin):
        raise BuildError(
            "[extra.build] theme_from must be 'system' or a workspace's name, e.g. 'career'"
        )
    root = system if origin == "system" else shared / origin / "themes"
    if not (root / theme / "theme.toml").is_file():
        raise BuildError(
            f"there's no theme '{theme}' in {root} (no {theme}/theme.toml)"
        )
    if origin != "system" and any(
        p.is_symlink() for p in (root, root / theme, root / theme / "theme.toml")
    ):
        raise BuildError(
            f"the theme '{theme}' in {root} is a symlink; a theme from a workspace must "
            "be a folder in its /shared"
        )
    return root / theme


def no_symlinks(folder: str, names: list[str]) -> list[str]:
    """copytree's ignore: leave out every symlink."""
    return [n for n in names if os.path.islink(os.path.join(folder, n))]


def assemble(
    source: Path,
    work: Path,
    system: Path = SYSTEM,
    shared: Path = SHARED,
    entries: Path | None = None,
) -> None:
    """Copy the site to `work` (leaving out .git and an old public/) with its theme in place,
    and a system site's entries in its content/."""
    if not (source / "zola.toml").is_file():
        raise BuildError(f"{source} has no zola.toml, so it isn't a Zola site")
    try:
        conf = tomllib.loads((source / "zola.toml").read_text())
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise BuildError(f"zola.toml doesn't parse: {e}") from None
    theme = theme_source(conf, system, shared)
    shutil.copytree(
        source, work, symlinks=True, ignore=shutil.ignore_patterns(".git", "public")
    )
    if theme is not None:
        dest = work / "themes" / theme.name
        shutil.rmtree(dest, ignore_errors=True)
        shutil.copytree(theme, dest, symlinks=True, ignore=no_symlinks)
    if entries is not None and entries.is_dir():
        shutil.copytree(
            entries,
            work / "content",
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("_index.md", ".*"),
        )


def main() -> None:
    source, base_url = Path(sys.argv[1]), sys.argv[2]
    entries = Path(sys.argv[3]) if len(sys.argv) > 3 else None
    try:
        assemble(source, WORK, entries=entries)
    except BuildError as e:
        print(e, file=sys.stderr)
        sys.exit(2)
    done = subprocess.run(
        [
            "zola",
            "--root",
            str(WORK),
            "build",
            "--base-url",
            base_url,
            "--output-dir",
            str(OUT),
        ],
        check=False,
    )
    sys.exit(done.returncode)


if __name__ == "__main__":
    main()
