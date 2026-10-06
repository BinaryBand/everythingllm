"""The saved logins (browser.vault, browser.origin): sealed per workspace with a key kept
apart, tied to their site, and never shown with their secrets."""

import os
import stat
import threading
import time

import pytest
from browser import origin
from browser.vault import Vault, VaultError, public, totp, totp_secret

RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # RFC 6238's "12345678901234567890"


@pytest.fixture
def vault(tmp_path):
    return Vault(tmp_path / "data" / "vault", tmp_path / "config" / "browser-vault.key")


def test_a_saved_login_is_sealed_and_comes_back_only_for_its_workspace(vault, tmp_path):
    saved = vault.add(
        "career",
        "https://www.LinkedIn.com/login",
        "alice@example.com",
        "hunter2",
        ask=True,
    )
    assert saved == {"id": saved["id"], "site": "linkedin.com", "username": "alice@example.com",
                     "totp": False, "ask": True, "used": ""}  # fmt: skip
    raw = vault.file("career").read_bytes()
    assert b"hunter2" not in raw and b"alice" not in raw and raw.startswith(b"bwv1")
    assert stat.S_IMODE(os.stat(vault.file("career")).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(vault.key_file).st_mode) == 0o600
    assert vault.get("career", saved["id"])["password"] == "hunter2"
    assert vault.logins("education") == []
    # One workspace's file can't stand in for another's, nor open with another key.
    vault.file("education").write_bytes(raw)
    with pytest.raises(VaultError, match="doesn't open"):
        vault.logins("education")
    other = Vault(vault.folder, tmp_path / "other.key")
    with pytest.raises(VaultError, match="doesn't open"):
        other.logins("career")


def test_saving_the_same_site_and_username_again_replaces_its_password(vault):
    first = vault.add("career", "linkedin.com", "alice", "old")
    again = vault.add("career", "www.linkedin.com", "alice", "new", totp=RFC_SECRET)
    assert again["id"] == first["id"] and again["totp"] and not again["ask"]
    assert vault.get("career", first["id"])["password"] == "new"
    other = vault.add("career", "linkedin.com", "bob", "pw")
    assert len(vault.logins("career")) == 2 and other["id"] != first["id"]
    vault.update("career", other["id"], ask=True, used="2026-10-07", password="nope")
    assert vault.get("career", other["id"])["password"] == "pw"
    assert (
        vault.logins("career")[1]["ask"]
        and vault.logins("career")[1]["used"] == "2026-10-07"
    )
    vault.delete("career", first["id"])
    assert [x["username"] for x in vault.logins("career")] == ["bob"]
    with pytest.raises(VaultError, match="no saved login"):
        vault.get("career", first["id"])


@pytest.mark.parametrize(
    "site",
    ["", "localhost", "192.168.1.10", "http://[::1]/", "intranet", "-x.com",
     "github.io", "co.uk", "https://www.co.uk/", "s3.amazonaws.com", "foo.ck"],
)  # fmt: skip
def test_a_login_is_for_a_sites_name(vault, site):
    with pytest.raises(VaultError):
        vault.add("career", site, "a", "b")


def test_saving_a_login_again_keeps_it_asking_first(vault):
    first = vault.add("career", "linkedin.com", "alice", "old", ask=True)
    again = vault.add("career", "linkedin.com", "alice", "new")
    assert again["id"] == first["id"] and again["ask"]
    assert not vault.update("career", first["id"], ask=False)["ask"]
    assert vault.add("career", "linkedin.com", "alice", "newer", ask=True)["ask"]


def test_a_key_that_cant_be_read_is_the_vaults_error(vault):
    vault.add("career", "x.com", "a", "b")
    fresh = Vault(vault.folder, vault.key_file)
    vault.key_file.chmod(0o000)
    try:
        if os.access(vault.key_file, os.R_OK):  # root reads it anyway
            pytest.skip("this user reads any file")
        with pytest.raises(VaultError, match="can't be read or made"):
            fresh.logins("career")
    finally:
        vault.key_file.chmod(0o600)


def test_a_login_needs_something_secret(vault):
    with pytest.raises(VaultError, match="needs a password"):
        vault.add("career", "x.com", "a", "")
    assert vault.add("career", "x.com", "a", "", totp=RFC_SECRET)["totp"]


def test_public_never_has_the_secrets():
    shown = public(
        {
            "id": "1",
            "site": "x.com",
            "username": "a",
            "password": "p",
            "totp": "S",
            "ask": False,
        }
    )
    assert "p" not in shown.values() and "S" not in shown.values()
    assert (
        set(shown) == {"id", "site", "username", "totp", "ask", "used"}
        and shown["totp"] is True
    )


def test_2fa_codes_are_rfc_6238s():
    assert totp(RFC_SECRET, 59) == "287082"
    assert totp(RFC_SECRET, 1111111109) == "081804"
    assert totp_secret("gezd gnbv gy3t qojq gezd gnbv gy3t qojq") == RFC_SECRET
    assert (
        totp_secret(
            f"otpauth://totp/LinkedIn:alice?secret={RFC_SECRET}&issuer=LinkedIn"
        )
        == RFC_SECRET
    )
    for bad in (
        "not base32!",
        "AAAA",
        f"otpauth://totp/x?secret={RFC_SECRET}&digits=8",
    ):
        with pytest.raises(VaultError):
            totp_secret(bad)


def test_a_login_fills_only_on_its_site_and_its_subdomains():
    assert (
        origin.normal_site("https://WWW.LinkedIn.com:443/login?x=1") == "linkedin.com"
    )
    assert origin.site_matches("www.linkedin.com", "linkedin.com")
    assert origin.site_matches("linkedin.com", "linkedin.com")
    for host in ("evil-linkedin.com", "linkedin.com.evil.example", "evil.example", ""):
        assert not origin.site_matches(host, "linkedin.com"), host
    assert not origin.site_matches("linkedin.com", "")
    assert origin.host_of("https://Login.Example.com/a") == "login.example.com"


def test_a_login_is_never_for_a_shared_suffix(vault):
    # Anyone can have a subdomain of github.io: a login for it would fill on all of them.
    with pytest.raises(VaultError, match="shared by many"):
        vault.add("career", "github.io", "a", "b")
    assert vault.add("career", "alice.github.io", "a", "b")["site"] == "alice.github.io"
    assert vault.add("career", "www.bbc.co.uk", "a", "b")["site"] == "bbc.co.uk"
    assert origin.site_matches("x.alice.github.io", "alice.github.io")
    assert not origin.site_matches("attacker.github.io", "alice.github.io")
    # A login saved for one before they were refused fills nowhere.
    assert not origin.site_matches("attacker.github.io", "github.io")
    assert not origin.site_matches("github.io", "github.io")
    # The list's wildcard and exception rules, and its names in punycode too.
    assert origin.is_public_suffix("foo.ck") and not origin.is_public_suffix("www.ck")
    assert origin.public_suffix("a.b.co.uk") == "co.uk"
    assert origin.public_suffix("example.unlisted") == "unlisted"
    assert origin.is_public_suffix("公司.cn")
    assert origin.is_public_suffix("xn--55qx5d.cn")
    # Nor does a login fill across a public suffix below its site.
    assert not origin.site_matches("anyone.blob.core.windows.net", "windows.net")
    assert not origin.site_matches("bucket.s3.amazonaws.com", "amazonaws.com")
    assert origin.site_matches("portal.windows.net", "windows.net")
    # A name typed in Unicode is saved as the browser's addresses carry it.
    assert (
        vault.add("career", "https://www.Bücher.de/", "a", "b")["site"]
        == "xn--bcher-kva.de"
    )


def test_changes_at_once_are_all_kept(vault, monkeypatch):
    load = vault.load

    def slow_load(workspace):  # a wide window between a change's load and its save
        logins = load(workspace)
        time.sleep(0.01)
        return logins

    monkeypatch.setattr(vault, "load", slow_load)
    start = threading.Barrier(8)

    def add(n):
        start.wait()
        vault.add("career", "x.com", f"user{n}", "pw")

    threads = [threading.Thread(target=add, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(x["username"] for x in vault.logins("career")) == [
        f"user{n}" for n in range(8)
    ]
    # A change that fails part way saves nothing.
    with pytest.raises(RuntimeError), vault.changing("career") as logins:
        logins.clear()
        raise RuntimeError
    assert len(vault.logins("career")) == 8
