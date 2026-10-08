"""hostctl runs with any python3 (health.sh, the apps' `before` steps, the templates'
ExecStartPre), with nothing but its own src and hostenv's on PYTHONPATH: so both import only
the standard library and each other, and every such PYTHONPATH names both."""

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OURS = {"hostctl", "hostenv"}


def imported(path: Path) -> set[str]:
    names = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_hostctl_and_hostenv_import_only_the_standard_library_and_each_other():
    for package in OURS:
        for path in (ROOT / "packages" / package / "src").rglob("*.py"):
            other = imported(path) - set(sys.stdlib_module_names) - OURS
            assert not other, f"{path.relative_to(ROOT)} imports {sorted(other)}"


def test_every_pythonpath_with_hostctl_has_hostenv_too():
    found = 0
    for path in [
        *(ROOT / "host").rglob("*.in"),
        *(ROOT / "packages" / "hostctl").rglob("*.sh"),
        *(ROOT / "packages" / "hostctl" / "src").rglob("*.py"),
    ]:
        for line in path.read_text().splitlines():
            for value in re.findall(r"PYTHONPATH=(\S+)", line):
                if "hostctl/src" in value:
                    found += 1
                    assert "hostenv/src" in value, f"{path.relative_to(ROOT)}: {line}"
    assert found >= 4  # health.sh twice, the research template, hostctl's docstring
    from hostctl import appctl

    assert "packages/hostenv/src" in appctl.PYTHONPATH
