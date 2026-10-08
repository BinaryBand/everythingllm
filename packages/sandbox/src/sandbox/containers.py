"""The sandbox's containers: running podman, and the podman arguments of a run's container
(on egress-net, the egress proxy its only way out) and of a site build's (no network).

Both are hardened alike (`hardening`): a read-only root, every capability dropped, no new
privileges, a keep-id user namespace, and memory, CPU, process, file size and open file
limits. The repo's
files a container needs (SITEBUILD, MODEL_CLIENT) are copied into its read-only /sandbox."""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path

from sandbox.access import NO_ACCESS, Access
from sandbox.errors import SandboxError
from sandbox.workspace import KEY_RE, Config, Scope

# Also named in hostctl's sandbox-images (cli.py).
IMAGE = "localhost/everythingllm-sandbox"
PROXY_CONTAINER = "systemd-egress-proxy"  # the egress proxy, as Quadlet names it
LABEL = "everythingllm-sandbox=1"
# Shared and system folders are data: nothing in them runs as ./file.
DATA_RW = "rw,noexec,nosuid,nodev"
DATA_RO = "ro,noexec,nosuid,nodev"
MEMORY = "1g"
SITEBUILD = Path(__file__).with_name("sitebuild.py")  # the build helper, from the repo
MODEL_CLIENT = Path(__file__).with_name("model_client.py")  # a run's way to ask a model
MODELS_DIR = "/run/everythingllm"  # where a run with model access finds its socket
OUTPUT_BYTES = 20_000  # per stream of a run
FILE_MAX_BYTES = 2 << 30
OPEN_FILES = 4096

# What podman produced: exit code, stdout, stderr, and whether it was killed for time.
PodmanResult = tuple[int, str, str, bool]
Podman = Callable[[list[str], float | None, str | None], Awaitable[PodmanResult]]


def joined(head: bytes | bytearray, tail: bytes | bytearray, cut: int) -> str:
    """Text kept from both ends of something longer; the end of a traceback matters most."""
    gap = f"\n… [{cut} bytes cut] …\n" if cut else ""
    return head.decode(errors="replace") + gap + tail.decode(errors="replace")


async def capture(stream: asyncio.StreamReader, limit: int) -> str:
    """Read a stream to the end, keeping its first tenth of `limit` bytes and the rest from its end."""
    keep_head = limit // 10
    keep_tail = limit - keep_head
    head = bytearray()
    tail = bytearray()
    cut = 0
    while chunk := await stream.read(65536):
        if len(head) < keep_head:
            take = keep_head - len(head)
            head += chunk[:take]
            chunk = chunk[take:]
        tail += chunk
        if len(tail) > keep_tail:
            cut += len(tail) - keep_tail
            del tail[: len(tail) - keep_tail]
    return joined(head, tail, cut)


async def podman(
    args: list[str], timeout: float | None, kill: str | None
) -> PodmanResult:
    """Run podman; on timeout, kill the container named `kill` and report it."""
    proc = await asyncio.create_subprocess_exec(
        "podman",
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stdout is not None and proc.stderr is not None  # both are PIPEs
    reading = asyncio.gather(
        capture(proc.stdout, OUTPUT_BYTES), capture(proc.stderr, OUTPUT_BYTES)
    )
    timed_out = False
    try:
        await asyncio.wait_for(proc.wait(), timeout)
    except TimeoutError:
        timed_out = True
        if kill:
            killer = await asyncio.create_subprocess_exec(
                "podman",
                "kill",
                kill,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:  # a hung podman mustn't keep the run, its lock and its slot forever
                await asyncio.wait_for(killer.wait(), 30)
            except TimeoutError:
                killer.kill()
                await killer.wait()
        try:
            await asyncio.wait_for(proc.wait(), 30)
        except TimeoutError:
            proc.kill()
            await proc.wait()
    out, err = await reading
    return proc.returncode or 0, out, err, timed_out


def hardening(name: str) -> list[str]:
    """Every sandbox container's podman arguments up to its network and mounts."""
    return [
        "run",
        "--name",
        name,
        "--label",
        LABEL,
        "--read-only",
        "--tmpfs",
        "/tmp:rw,size=256m,mode=1777",
        "--memory",
        MEMORY,
        "--memory-swap",
        MEMORY,
        "--cpus",
        "1",
        "--pids-limit",
        "256",
        "--ulimit",
        f"fsize={FILE_MAX_BYTES}:{FILE_MAX_BYTES}",
        "--ulimit",
        f"nofile={OPEN_FILES}:{OPEN_FILES}",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--userns",
        "keep-id",
    ]


def others_shared(config: Config, workspace: str) -> list[tuple[str, Path]]:
    """Every other workspace's shared folder, as (workspace, folder): workspaces that
    have used the sandbox, so their folders exist."""
    return sorted(
        (d.name, d / "shared")
        for d in config.root.iterdir()
        if d.name != workspace
        and KEY_RE.fullmatch(d.name)
        and not d.is_symlink()
        and (d / "shared").is_dir()
        and not (d / "shared").is_symlink()
    )


def read_only_mounts(config: Config, workspace: str, others: bool = True) -> list[str]:
    """Every other workspace's shared folder (without `others`, none) and the repo's
    themes, read-only."""
    return [
        *(
            a
            for other, folder in (others_shared(config, workspace) if others else ())
            for a in ("-v", f"{folder}:/shared/{other}:{DATA_RO}")
        ),
        "-v",
        f"{config.system_themes}:/system/themes:{DATA_RO}",
    ]


def model_dir(config: Config, run: str) -> Path:
    """The folder of a run's model socket, mounted into its container (MODELS_DIR)."""
    return config.model_sockets / run


def prepare(
    config: Config,
    name: str,
    scope: Scope,
    run_dir: Path,
    script: str,
    code: str,
    ip: str,
    access: Access = NO_ACCESS,
) -> list[str]:
    """Write the run's script and return its podman arguments: egress-net at `ip`,
    with the egress proxy as its only way out; the workspace's own folders read-write,
    every other workspace's shared folder and the repo's themes read-only. With web
    access, through the proxy's public-only port, and without the other workspaces'
    shared folders, which a run that reads the web could send anywhere. With model
    access, its socket's folder and the client that asks through it."""
    proxy = config.public_proxy if access.web else config.proxy
    (run_dir / "code").mkdir(parents=True)
    (run_dir / "code" / script).write_text(code)
    asking = []
    if access.models:  # the run's own socket (served by execute) and the client
        sockets = model_dir(config, name)
        if len(str(sockets / "sock")) > 100:  # AF_UNIX's limit is 108
            raise SandboxError(
                f"{config.model_sockets} is too long a path for model sockets"
            )
        sockets.mkdir(parents=True)
        shutil.copyfile(MODEL_CLIENT, run_dir / "code" / "everythingllm_models.py")
        asking = [
            "-v",
            f"{sockets}:{MODELS_DIR}:ro",
            "-e",
            f"EVERYTHINGLLM_MODELS={MODELS_DIR}/sock",
        ]
    # No --rm: the container is kept until execute has asked podman whether it ran out of memory.
    return [
        *hardening(name),
        "--network",
        f"{config.network}:ip={ip}",
        "--dns",
        "none",
        "-e",
        f"http_proxy={proxy}",
        "-e",
        f"https_proxy={proxy}",
        *(
            a
            for mount, root in scope.roots.items()
            for a in (
                "-v",
                f"{root}:{mount}"
                + (
                    f":{DATA_RW}"
                    if mount == "/public" or mount.startswith("/shared/")
                    else ""
                ),
            )
        ),
        *read_only_mounts(config, scope.workspace, others=not access.web),
        *asking,
        "-v",
        f"{run_dir / 'code'}:/sandbox:ro",
        IMAGE,
    ]


def prepare_build(config: Config, name: str, scope: Scope, run_dir: Path) -> list[str]:
    """A build container's podman arguments: no network, the workspace's own folders
    (but /public) and everything else read-only, an empty /out, and the helper."""
    (run_dir / "code").mkdir(parents=True)
    (run_dir / "out").mkdir()
    shutil.copyfile(SITEBUILD, run_dir / "code" / "sitebuild.py")
    return [
        *hardening(name),
        "--network",
        "none",
        *(
            a
            for mount, root in scope.roots.items()
            if mount != "/public"
            for a in ("-v", f"{root}:{mount}:{DATA_RO}")
        ),
        *read_only_mounts(config, scope.workspace),
        "-v",
        f"{run_dir / 'out'}:/out:rw,noexec,nosuid,nodev",
        "-v",
        f"{run_dir / 'code'}:/sandbox:ro",
        IMAGE,
    ]
