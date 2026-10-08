"""A browser-runner with podman faked: `podman run` serves a fake driver on the socket
folder it mounts at /run/browser, as a container's browser.driver would, and `podman
stop` or `rm -f` takes it away."""

import asyncio
import base64
import io
from pathlib import Path

import hostrpc
from browser import config as config_mod
from PIL import Image

IPS = {"browser-1": "10.89.79.32", "browser-2": "10.89.79.33"}


def passkey(rp_id="github.com", credential_id="q1079Y6M5OeiRR2o", **more) -> dict:
    """A passkey as Chromium's virtual authenticator gives one (WebAuthn's Credential)."""
    return {"credentialId": credential_id, "isResidentCredential": True, "rpId": rp_id,
            "privateKey": "MIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQg",
            "userHandle": "AQID", "signCount": 1, "userName": "alice", **more}  # fmt: skip


def as_given(credential: dict) -> dict:
    """A passkey as the runner gives it to the driver: without the site or user's name."""
    return {k: v for k, v in credential.items() if k not in ("rpId", "userName")}


def jpeg(colour=(200, 30, 30), size=(1280, 800)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, colour).save(out, "JPEG")
    return out.getvalue()


class FakeDriver(hostrpc.Service):
    def __init__(self):
        super().__init__()
        self.pages: dict[str, str] = {}  # thread -> url
        self.calls: list[tuple[str, dict]] = []
        self.filled: list[dict] = []
        self.typed: list[
            tuple
        ] = []  # what the user sent from the take-over view's field
        self.capturing = self.taken = False
        self.offers: dict[str, dict] = {}
        self.making = False
        self.made: list[dict] = []  # what a site makes while making
        # Whether the page asks for the passkey on the click.
        self.asked_for_passkey = True

    def view(self, thread):
        url = self.pages[thread]
        return {
            "title": f"Title of {url}",
            "url": url,
            "elements": ['[e1] button "Sign in"', '[e2] link "Home" -> /'],
            "text": "Welcome\nSign in to go on",
            "more": False,
            "notes": [],
        }

    async def op_open(self, thread, url):
        self.calls.append(("open", {"thread": thread, "url": url}))
        self.pages[thread] = url
        return self.view(thread)

    async def op_act(self, thread, action, ref="", text=""):
        self.calls.append(
            ("act", {"thread": thread, "action": action, "ref": ref, "text": text})
        )
        if thread not in self.pages:
            raise hostrpc.RunnerError("this chat has no page open; open one first")
        return self.view(thread)

    async def op_read(self, thread):
        self.calls.append(("read", {"thread": thread}))
        return self.view(thread)

    async def op_screenshot(self, thread=""):
        if thread not in self.pages:
            return {"jpeg": "", "title": "", "url": ""}
        return {
            "jpeg": base64.b64encode(jpeg()).decode(),
            "title": "Shot",
            "url": self.pages[thread],
        }

    async def op_front(self, thread):
        self.calls.append(("front", {"thread": thread}))
        return {"url": self.pages.get(thread, "")}

    async def op_user_type(self, text, secret=False, thread=""):
        if not self.capturing:
            raise hostrpc.RunnerError("only the user types here, while they have it")
        self.typed.append((text, secret, thread))
        return {}

    async def op_user_key(self, key, thread=""):
        if not self.capturing:
            raise hostrpc.RunnerError("only the user types here, while they have it")
        self.typed.append((key, "key", thread))
        return {}

    async def op_fill_login(
        self,
        thread,
        site,
        username="",
        password="",
        user_ref="",
        pass_ref="",
        submit=False,
    ):
        self.filled.append({"thread": thread, "site": site, "username": username, "password": password,
                            "user_ref": user_ref, "pass_ref": pass_ref, "submit": submit})  # fmt: skip
        return self.view(thread)

    async def op_fill_code(self, thread, site, code, ref, submit=False):
        self.filled.append(
            {"thread": thread, "site": site, "code": code, "ref": ref, "submit": submit}
        )
        return self.view(thread)

    async def op_capture(self, on, user=False):
        if not on:
            self.making = False
        self.capturing, self.taken = on, user
        return {}

    async def op_sign_in_passkey(self, thread, site, credential, ref):
        self.filled.append(
            {"thread": thread, "site": site, "credential": credential, "ref": ref}
        )
        count = credential["signCount"] + 1 if self.asked_for_passkey else None
        return {**self.view(thread), "sign_count": count}

    async def op_make_passkeys(self, on):
        if on and not self.capturing:
            raise hostrpc.RunnerError(
                "only the user makes passkeys, while they have it"
            )
        self.making = on
        return {}

    async def op_made(self):
        made, self.made = self.made, []
        if made:  # one is what the user asked for
            self.making = False
        return {"making": self.making, "made": made}

    async def op_offers(self):
        self.calls.append(("offers", {}))
        return [
            {"id": k, "site": o["site"], "username": o["username"]}
            for k, o in self.offers.items()
        ]

    async def op_peek_offer(self, id):
        if id not in self.offers:
            raise hostrpc.RunnerError("that login isn't waiting to be saved any more")
        return dict(self.offers[id])

    async def op_drop_offer(self, id):
        self.offers.pop(id, None)
        return {}

    async def op_close(self, thread):
        self.calls.append(("close", {"thread": thread}))
        self.pages.pop(thread, None)
        return {}


class FakePodman:
    """Records every podman call; `run -d` starts a FakeDriver for the container."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.drivers: dict[str, FakeDriver] = {}  # container name -> its driver
        self.stops: dict[str, asyncio.Event] = {}
        self.tasks: dict[str, asyncio.Task] = {}

    async def __call__(self, args, timeout):
        self.calls.append(args)
        if args[:2] == ["run", "-d"]:
            name = args[args.index("--name") + 1]
            folder = next(
                Path(v.split(":")[0])
                for v in (args[i + 1] for i, a in enumerate(args) if a == "-v")
                if ":/run/browser:" in v
            )
            self.drivers[name] = driver = FakeDriver()
            self.stops[name] = stop = asyncio.Event()
            self.tasks[name] = asyncio.create_task(
                hostrpc.serve(driver, folder / "driver.sock", stop=stop)
            )
        elif args[0] in ("stop", "rm") and args[-1] in self.stops:
            await self.kill(args[-1])
        return 0, "", ""

    async def kill(self, name):
        if name in self.stops:
            self.stops.pop(name).set()
            await self.tasks.pop(name)

    async def close(self):
        for name in list(self.stops):
            await self.kill(name)

    def runs(self):
        return [c for c in self.calls if c[:2] == ["run", "-d"]]


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def config(tmp_path, **kw) -> config_mod.Config:
    (tmp_path / "workspaces").mkdir(exist_ok=True)  # as serve() makes it
    return config_mod.Config(
        root=tmp_path / "workspaces",
        data=tmp_path / "data",
        ips=dict(IPS),
        network="egress-net",
        proxy="http://10.89.79.2:3129",
        pages_url=kw.pop("pages_url", "https://host.example.ts.net:8445/"),
        takeover_url="https://host.example.ts.net:8454/",
        repo=Path("/repo"),
        vault_key=tmp_path / "config" / "browser-vault.key",
        **kw,
    )


def scope(workspace="career", thread="7"):
    return {"workspace": workspace, "thread": thread}
