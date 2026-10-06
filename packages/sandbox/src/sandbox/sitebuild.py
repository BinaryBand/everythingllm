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

  python sitebuild.py <site folder> <base url>
"""

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
    return root / theme


def assemble(
    source: Path, work: Path, system: Path = SYSTEM, shared: Path = SHARED
) -> None:
    """Copy the site to `work` (leaving out .git and an old public/) with its theme in place."""
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
        shutil.copytree(theme, dest, symlinks=True)


def main() -> None:
    source, base_url = Path(sys.argv[1]), sys.argv[2]
    try:
        assemble(source, WORK)
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
