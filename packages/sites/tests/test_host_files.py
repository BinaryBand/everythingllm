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
    """Agent-written pages are only safe while this holds: no scripts, and nothing fetched
    from another host, so CSS can't send anything out either."""
    sites = caddy_policies()
    assert set(sites) == {"8445", "8447"}
    pages, workspaces = sites["8445"], sites["8447"]
    assert set(pages) == {"default"} and set(workspaces) == {"default", "scripts"}
    for rules in (pages["default"], workspaces["default"]):
        assert rules["default-src"] == "'self'" and rules["script-src"] == "'none'"
        assert (
            rules["frame-ancestors"]
            == rules["form-action"]
            == rules["base-uri"]
            == "'none'"
        )
    # The workspaces' pages may use inline CSS; nothing else is loosened.
    assert workspaces["default"] == {
        **pages["default"],
        "style-src": "'self' 'unsafe-inline'",
    }


def test_no_workspace_runs_scripts_yet():
    """The scripts policy is there to be switched on for one workspace, on its own origin
    (:8447); until then its matcher matches nothing, and it only ever adds scripts."""
    workspaces = caddy_policies()["8447"]
    assert workspaces["scripts"] == {
        **workspaces["default"],
        "script-src": "'self' 'unsafe-inline'",
    }
    text = (ROOT / "host" / "caddy" / "pages.Caddyfile").read_text()
    assert re.findall(r"^\s*@scripts (.*)$", text, flags=re.MULTILINE) == [
        "expression false"
    ]
