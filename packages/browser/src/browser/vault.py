"""The workspaces' saved logins: browser-runner's vault, which the agent can use but never
read. Like Muse's, it lives outside the agent and outside the browser: the runner keeps it
on the host, and a login's password (or 2FA code) goes from here straight into a field on
the login's own site (browser.driver), never into a reply, a log or the model.

One file per workspace, `<folder>/<workspace>.vault` (mode 0600), as the profiles are: a
login saved in `career` is never `education`'s. Each file is AES-GCM with a key kept apart
from the data dir and its backups (`key_file`, made on first use), and the workspace's name
as associated data, so one workspace's file can't stand in for another's.

A login: {id, site, username, password, totp, ask, added, used}. `site` is a host name
(browser.origin); `totp` an optional base32 2FA secret, from which the runner makes the
current code; `ask` whether the user wants to approve each use. What leaves the vault for
anyone but the driver is `public()`: no password, no secret.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import hostrpc
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from browser.origin import normal_site

MAGIC = b"bwv1"
MAX_LOGINS = 200
MAX_FIELD = 1000  # characters in a username or password


class VaultError(hostrpc.RunnerError):
    pass


def totp_secret(text: str) -> str:
    """A 2FA secret as saved: base32, upper case, no spaces, from the secret itself or an
    otpauth:// address (a QR code's). VaultError for anything else, or for an otpauth that
    asks for other than SHA-1, 6 digits and 30 seconds, the codes `totp` makes."""
    text = (text or "").strip()
    if text.lower().startswith("otpauth://"):
        query = {k: v[0] for k, v in parse_qs(urlsplit(text).query).items()}
        if (
            query.get("algorithm", "SHA1").upper() != "SHA1"
            or query.get("digits", "6") != "6"
            or query.get("period", "30") != "30"
        ):
            raise VaultError(
                "only 2FA codes of 6 digits, every 30 seconds, by SHA-1 are supported"
            )
        text = query.get("secret", "")
    secret = re.sub(r"[\s-]", "", text).upper().rstrip("=")
    try:
        if len(base64.b32decode(secret + "=" * (-len(secret) % 8))) < 10:
            raise ValueError
    except ValueError:
        raise VaultError(
            "that isn't a 2FA secret (the base32 text under a QR code)"
        ) from None
    return secret


def totp(secret: str, now: float | None = None) -> str:
    """The 2FA code for `secret` at `now` (RFC 6238: SHA-1, 30 seconds, 6 digits)."""
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    counter = int((time.time() if now is None else now) // 30)
    mac = hmac.new(key, counter.to_bytes(8, "big"), hashlib.sha1).digest()
    at = mac[-1] & 0x0F
    return str(
        (int.from_bytes(mac[at : at + 4], "big") & 0x7FFFFFFF) % 1_000_000
    ).zfill(6)


def public(login: dict[str, Any]) -> dict[str, Any]:
    """What may be shown of a login: never its password or 2FA secret."""
    return {
        "id": login["id"],
        "site": login["site"],
        "username": login["username"],
        "totp": bool(login.get("totp")),
        "ask": bool(login.get("ask")),
        "used": login.get("used", ""),
    }


class Vault:
    def __init__(self, folder: Path, key_file: Path):
        self.folder = folder
        self.key_file = key_file
        # The runner calls in from threads (asyncio.to_thread): a change is a load and a
        # save, which two at once would lose one of.
        self.lock = threading.RLock()
        self._key: bytes | None = None

    def key(self) -> bytes:
        """The vault's key, made (0600, in a 0700 folder) the first time it's needed."""
        with self.lock:
            if self._key is not None:
                return self._key
            try:
                try:
                    key = self.key_file.read_bytes()
                except FileNotFoundError:
                    self.key_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    key = AESGCM.generate_key(bit_length=256)
                    fd = os.open(
                        self.key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                    )
                    with os.fdopen(fd, "wb") as f:
                        f.write(key)
            except OSError as e:
                raise VaultError(
                    f"the vault's key {self.key_file} can't be read or made ({e.strerror or e})"
                ) from None
            if len(key) != 32:
                raise VaultError(f"{self.key_file} isn't a vault key")
            self._key = key
            return key

    def file(self, workspace: str) -> Path:
        return self.folder / f"{workspace}.vault"

    def load(self, workspace: str) -> list[dict[str, Any]]:
        try:
            data = self.file(workspace).read_bytes()
        except FileNotFoundError:
            return []
        except OSError as e:
            raise VaultError(
                f"{self.file(workspace)} can't be read ({e.strerror or e})"
            ) from None
        if not data.startswith(MAGIC):
            raise VaultError(f"{self.file(workspace)} isn't a vault")
        nonce, sealed = data[len(MAGIC) : len(MAGIC) + 12], data[len(MAGIC) + 12 :]
        try:
            plain = AESGCM(self.key()).decrypt(nonce, sealed, self.aad(workspace))
        except InvalidTag:
            raise VaultError(
                f"{self.file(workspace)} doesn't open with {self.key_file} (another key, or not this workspace's)"
            ) from None
        return json.loads(plain)

    def save(self, workspace: str, logins: list[dict[str, Any]]) -> None:
        nonce = secrets.token_bytes(12)
        sealed = AESGCM(self.key()).encrypt(
            nonce, json.dumps(logins).encode(), self.aad(workspace)
        )
        try:
            self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            hostrpc.atomic_write(
                self.file(workspace), MAGIC + nonce + sealed, mode=0o600
            )
        except OSError as e:
            raise VaultError(
                f"{self.file(workspace)} can't be saved ({e.strerror or e})"
            ) from None

    @contextlib.contextmanager
    def changing(self, workspace: str) -> Iterator[list[dict[str, Any]]]:
        """The workspace's logins, to change in place and saved after (unless that raised),
        one change at a time."""
        with self.lock:
            logins = self.load(workspace)
            yield logins
            self.save(workspace, logins)

    @staticmethod
    def aad(workspace: str) -> bytes:
        return f"everythingllm-browser-vault:{workspace}".encode()

    # --- what the runner asks ---

    def logins(self, workspace: str) -> list[dict[str, Any]]:
        return [public(login) for login in self.load(workspace)]

    def get(self, workspace: str, login_id: str) -> dict[str, Any]:
        login = next((x for x in self.load(workspace) if x["id"] == login_id), None)
        if login is None:
            raise VaultError(
                f"there's no saved login '{login_id}' in this workspace; list them first"
            )
        return login

    def add(
        self,
        workspace: str,
        site: str,
        username: str,
        password: str,
        totp: str = "",
        ask: bool = False,
    ) -> dict[str, Any]:
        """Save a login, or replace the password (and 2FA secret, if given) of the one with
        the same site and username. `ask` turns asking first on, never off: saving a new
        password keeps a login the user said to ask about asking (the list's toggle turns
        it off). Returns it, public."""
        try:
            site = normal_site(site)
        except ValueError as e:
            raise VaultError(str(e)) from None
        username, password = str(username or ""), str(password or "")
        if not password and not totp:
            raise VaultError("a login needs a password or a 2FA secret")
        if len(username) > MAX_FIELD or len(password) > MAX_FIELD:
            raise VaultError("that's too long for a login")
        secret = totp_secret(totp) if totp else ""
        with self.changing(workspace) as logins:
            login = next(
                (x for x in logins if x["site"] == site and x["username"] == username),
                None,
            )
            if login is None:
                if len(logins) >= MAX_LOGINS:
                    raise VaultError(f"a workspace keeps at most {MAX_LOGINS} logins")
                login = {"id": secrets.token_hex(4), "site": site, "username": username,
                         "added": time.strftime("%Y-%m-%d"), "used": ""}  # fmt: skip
                logins.append(login)
            if password:
                login["password"] = password
            if secret:
                login["totp"] = secret
            login["ask"] = bool(ask) or bool(login.get("ask"))
        return public(login)

    def update(self, workspace: str, login_id: str, **fields: Any) -> dict[str, Any]:
        """Set `ask` or `used` on a login."""
        with self.changing(workspace) as logins:
            login = next((x for x in logins if x["id"] == login_id), None)
            if login is None:
                raise VaultError(f"there's no saved login '{login_id}'")
            login.update({k: v for k, v in fields.items() if k in ("ask", "used")})
        return public(login)

    def delete(self, workspace: str, login_id: str) -> None:
        with self.changing(workspace) as logins:
            kept = [x for x in logins if x["id"] != login_id]
            if len(kept) == len(logins):
                raise VaultError(f"there's no saved login '{login_id}'")
            logins[:] = kept
