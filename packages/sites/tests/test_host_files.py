"""The pages site's Caddyfile: the policies the pages are served under."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def parse_csp(csp: str) -> dict[str, str]:
    return dict(rule.strip().split(" ", 1) for rule in csp.split(";"))


def caddy_policies() -> dict[str, dict[str, dict[str, str]]]:
    """Each site's CSPs by matcher ("default" for the header with none), by its port."""
    text = (ROOT / "host" / "caddy" / "pages.Caddyfile").read_text()
    sites = re.split(r"^:(\d+) \{$", text, flags=re.MULTILINE)[1:]
    return {
        port: {
            name or "default": parse_csp(csp)
            for name, csp in re.findall(
                r'header (?:@(\w+) )?Content-Security-Policy "([^"]*)"', block
            )
        }
        for port, block in zip(sites[::2], sites[1::2])
    }


def test_pages_csp_still_forbids_scripts():
    """The system sites are only safe while this holds: no scripts, and nothing fetched
    from another host, so CSS can't send anything out either."""
    sites = caddy_policies()
    assert set(sites) == {"8445", "8447"}
    pages, workspaces = sites["8445"], sites["8447"]
    assert set(pages) == set(workspaces) == {"default"}
    rules = pages["default"]
    assert rules["default-src"] == "'self'" and rules["script-src"] == "'none'"
    assert (
        rules["frame-ancestors"]
        == rules["form-action"]
        == rules["base-uri"]
        == "'none'"
    )
    assert "sandbox" not in rules and "style-src" not in rules


def test_workspace_pages_run_scripts_only_in_a_sandbox():
    """Agent-written pages may run inline and same-site scripts, but each in an opaque
    origin of its own: allow-same-origin would let one workspace's scripts read and change
    every other's pages, and forms and popups would let a page post or open anything.
    Nothing else is loosened (packages/sandbox/tests/test_pages_browser.py tries it)."""
    pages, workspaces = caddy_policies()["8445"], caddy_policies()["8447"]
    sandbox = workspaces["default"]["sandbox"].split()
    assert sandbox == ["allow-scripts", "allow-downloads"]
    for never in ("allow-same-origin", "allow-forms", "allow-popups"):
        assert never not in sandbox
    assert workspaces["default"] == {
        **pages["default"],
        "script-src": "'self' 'unsafe-inline'",
        "style-src": "'self' 'unsafe-inline'",
        "sandbox": "allow-scripts allow-downloads",
    }
