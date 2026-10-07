"""Which site a saved login belongs to, and whether a page is on it. A login is saved for a
site (a host name, like `linkedin.com`) and fills only on that host or its subdomains, as a
password manager's does, and only over https on its usual port (`secure`), so a page that
talks the agent into it can't have a login typed into another site or sent in the clear. Standard library only: the driver checks it in the browser container
too, against the frame the field is really in.

A site is never a public suffix (`co.uk`, `github.io`), whose subdomains belong to anyone:
a login for `github.io` would fill on `attacker.github.io`. The suffixes are Mozilla's Public
Suffix List, kept beside this module (`public_suffix_list.dat`, MPL-2.0, its version in its
header); refresh it now and then from https://publicsuffix.org/list/public_suffix_list.dat.
"""

import functools
import ipaddress
import re
from pathlib import Path
from urllib.parse import urlsplit

SUFFIX_LIST = Path(__file__).with_name("public_suffix_list.dat")

# Names that only mean something on a local network, never a public site's.
LOCAL_TLDS = {"local", "localhost", "internal", "lan", "home", "corp", "intranet", "arpa", "test", "invalid"}  # fmt: skip
HOST_RE = re.compile(
    r"(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)(\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+"
)


def normal_site(text: str) -> str:
    """The site a login is saved for, from what was typed (`https://www.LinkedIn.com/login`
    -> `linkedin.com`): the host alone, lower case, without `www.`, a name in Unicode in
    its punycode form as a browser's addresses carry it. ValueError for anything that isn't
    a dotted host name (an address, `localhost`, a bare word)."""
    text = (text or "").strip().lower()
    host = urlsplit(text if "://" in text else f"https://{text}").hostname or ""
    host = host.rstrip(".").removeprefix("www.")
    if not host.isascii():
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            raise ValueError(
                f"'{text}' isn't a site's name, like linkedin.com"
            ) from None
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError(f"'{text}' is an address; save a login for a site's name")
    last = host.rsplit(".", 1)[-1]
    if not HOST_RE.fullmatch(host) or last.isdigit() or last in LOCAL_TLDS:
        raise ValueError(f"'{text}' isn't a site's name, like linkedin.com")
    if is_public_suffix(host):
        raise ValueError(
            f"'{text}' is shared by many sites' owners; save the login for your own, like you.{host}"
        )
    return host


def site_matches(host: str, site: str) -> bool:
    """Whether a page on `host` is on `site`: the site itself, or a subdomain of it with no
    public suffix between them (a login for `windows.net` doesn't fill on
    `anyone.blob.core.windows.net`, whose `blob.core.windows.net` is a public suffix). So
    never for a public suffix, should a login have been saved for one before they were
    refused."""
    host = (host or "").lower().rstrip(".")
    return (
        bool(site)
        and (host == site or host.endswith("." + site))
        and site.endswith("." + public_suffix(host))
    )


@functools.cache
def suffix_rules() -> frozenset[str]:
    """The list's rules (`co.uk`, `*.ck`, `!www.ck`), each also in its punycode form, the
    one a browser's addresses carry."""
    rules = set()
    for line in SUFFIX_LIST.read_text(encoding="utf-8").splitlines():
        rule = line.strip().lower()
        if not rule or rule.startswith("//"):
            continue
        rules.add(rule)
        mark = rule[0] if rule[0] == "!" else "*." if rule.startswith("*.") else ""
        name = rule[len(mark) :]
        if not name.isascii():
            try:
                rules.add(mark + name.encode("idna").decode("ascii"))
            except UnicodeError:
                pass
    return frozenset(rules)


def public_suffix(host: str) -> str:
    """The public suffix `host` ends in (`co.uk` for `bbc.co.uk`, `github.io` for
    `alice.github.io`): the longest matching rule, an exception rule's parent first, or else
    the last label."""
    labels = host.lower().rstrip(".").split(".")
    rules = suffix_rules()
    for i in range(len(labels)):
        name, parent = ".".join(labels[i:]), ".".join(labels[i + 1 :])
        if "!" + name in rules:
            return parent
        if name in rules or (parent and "*." + parent in rules):
            return name
    return labels[-1]


def is_public_suffix(host: str) -> bool:
    return public_suffix(host) == host.lower().rstrip(".")


def host_of(url: str) -> str:
    return (urlsplit(url or "").hostname or "").lower()


def secure(url: str) -> bool:
    """Whether a page at `url` may have a login filled: https, on its usual port."""
    try:
        parts = urlsplit(url or "")
        return parts.scheme == "https" and parts.port in (None, 443)
    except ValueError:  # a port that isn't a number
        return False


def registrable(host: str) -> str:
    """Who a host belongs to: its name just above its public suffix (`evil.app` for
    `accounts.google.com.verify.evil.app`), the part to show first when asking about it."""
    host = (host or "").lower().rstrip(".")
    suffix = public_suffix(host)
    if host == suffix:
        return host
    return host[: -len(suffix) - 1].rsplit(".", 1)[-1] + "." + suffix
