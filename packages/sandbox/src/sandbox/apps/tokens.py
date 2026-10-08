"""Apps' write-back tokens: each app's current one, which its rendered page carries, and
the few before it, which mark a page as out of date rather than foreign. Kept host-only,
one file per app (mode 600), in the runner's app_state folder."""

import hmac
import json
import secrets
from pathlib import Path

import hostrpc

from sandbox.errors import BadToken, StaleToken

OLD = 8  # an app's earlier tokens, known as a stale page's


class Tokens:
    def __init__(self, folder: Path):
        self.folder = folder

    def file(self, workspace: str, name: str) -> Path:
        return self.folder / workspace / f"{name}.json"

    def held(self, workspace: str, name: str) -> tuple[str, list[str]]:
        """The app's current token ("" for none) and its earlier ones, newest first."""
        try:
            held = json.loads(self.file(workspace, name).read_text())
        except (OSError, ValueError):
            return "", []
        if not isinstance(held, dict):
            return "", []
        current = held.get("token")
        old = held.get("old")
        return (
            current if isinstance(current, str) else "",
            [t for t in old if isinstance(t, str)] if isinstance(old, list) else [],
        )

    def rotate(self, workspace: str, name: str) -> str:
        """A new token for the app; the one before joins the stale ones."""
        current, old = self.held(workspace, name)
        token = secrets.token_urlsafe(24)
        file = self.file(workspace, name)
        file.parent.mkdir(parents=True, exist_ok=True)
        kept = [t for t in (current, *old) if t][:OLD]
        hostrpc.atomic_write(file, json.dumps({"token": token, "old": kept}), 0o600)
        return token

    def check(self, workspace: str, name: str, token: str) -> None:
        """Nothing for the app's current token; StaleToken for one it had before,
        BadToken for any other."""
        current, old = self.held(workspace, name)
        if current and hmac.compare_digest(current, token):
            return
        if any(hmac.compare_digest(t, token) for t in old):
            raise StaleToken("this page is out of date; reload it")
        raise BadToken("this isn't the app's page")

    def remove(self, workspace: str, name: str) -> None:
        self.file(workspace, name).unlink(missing_ok=True)
