"""The podcasts' three service containers (README, "Podcasts"): each mounts what its code
writes or reads outside the repo and nothing more, at its host path, and goes out through
the podcasts profile. test_quadlet.py holds them to the hardening every service container
shares."""

import re
from pathlib import Path

import pytest
from egress import config as egress_config
from test_quadlet import QUADLET, container_keys

DATA = "%h/.local/share/everythingllm"
STORAGE = "@ANYTHINGLLM_STORAGE@"
STATE = f"{DATA}/podcasts"  # PODCASTS_STATE, with the queue and Whisper's models/
SITE = f"{DATA}/pages/public/podcasts"  # PODCASTS_DIR
# container: {mount target: whether it's read-only}
MOUNTS = {
    "podcasts-runner": {
        "@REPO@": True,
        f"{DATA}/venvs/podcasts-runner-ctr": False,
        STATE: False,
        SITE: False,
        f"{STORAGE}/everythingllm/podcasts": False,  # its socket
    },
    "podcasts-sync-worker": {
        "@REPO@": True,
        f"{DATA}/venvs/podcasts-sync-worker-ctr": False,
        STATE: False,
        SITE: False,
        "%h/.config/everythingllm/ctr/podcasts-sync-worker.env": True,  # DeepSeek
    },
    "podcasts-transcribe-worker": {
        "@REPO@": True,  # host.env, for PODCASTS_TRANSCRIBE_THREADS at every pass
        f"{DATA}/venvs/podcasts-transcribe-worker-ctr": False,
        STATE: False,
        SITE: False,
        "%h/.config/everythingllm/ctr/podcasts-transcribe-worker.env": True,  # DeepSeek
    },
}


def mounts(keys: dict[str, list[str]]) -> dict[str, bool]:
    """Each Volume='s target, and whether it's read-only."""
    out = {}
    for volume in keys["Volume"]:
        _, target, *options = volume.split(":")
        out[target] = "ro" in options
    return out


@pytest.mark.parametrize("name", MOUNTS)
def test_each_mounts_only_what_it_uses(name):
    template = QUADLET / f"{name}.container.in"
    keys = container_keys(template)
    assert mounts(keys) == MOUNTS[name]
    # Only the runner writes in storage (its socket), so only it keeps the user's groups.
    assert keys["GroupAdd"] == (["keep-groups"] if name == "podcasts-runner" else [])
    assert not keys["PublishPort"]  # podcasts-web, on the host, serves what they write
    # What it mounts from the host is made first: podman won't mount what isn't there.
    made = re.findall(
        r"^ExecStartPre=/usr/bin/mkdir -p (.+)$", template.read_text(), re.MULTILINE
    )
    made = {path for line in made for path in line.split()}
    writable = {t for t, ro in MOUNTS[name].items() if not ro}
    assert writable <= made, writable - made


def test_the_transcription_worker_keeps_its_limits_and_its_stop():
    template = QUADLET / "podcasts-transcribe-worker.container.in"
    keys = container_keys(template)
    assert "--memory=6g" in " ".join(keys["PodmanArgs"])
    service = template.read_text().split("[Service]", 1)[1]
    assert re.search(r"^SuccessExitStatus=143$", service, re.MULTILINE)
    assert re.search(r"^Nice=19$", service, re.MULTILINE)
    # huggingface_hub's own files go to /tmp, as ~/.cache is read-only here.
    env = dict(e.partition("=")[::2] for e in keys["Environment"])
    assert env["HF_HOME"].startswith("/tmp/")


def test_the_sync_worker_has_time_to_stop_between_steps():
    keys = container_keys(QUADLET / "podcasts-sync-worker.container.in")
    assert keys["StopTimeout"] == ["60"]


def test_they_reach_public_hosts_and_nothing_else_of_ours():
    egress = egress_config.load(env={"PUBLIC_HOST": "host.example.ts.net"})
    profile = egress.profiles["podcasts"]
    assert set(profile.ips) == set(MOUNTS)
    # Feeds, episodes, iTunes search, DeepSeek, and Whisper's model (huggingface.co and
    # its CDN): all public, on 80 or 443.
    for host, port in (
        ("feeds.example.com", 80),
        ("itunes.apple.com", 443),
        ("api.deepseek.com", 443),
        ("huggingface.co", 443),
    ):
        assert profile.judge(host, port) == "public"
    for port in (3001, 8888, 8445):
        assert profile.judge("host.example.ts.net", port) is None


def test_the_mounts_are_where_the_code_goes(monkeypatch, tmp_path):
    """Every path the podcasts' code uses outside the repo, under a writable mount of each
    container that uses it."""
    import hostrpc
    from podcasts import library, transcripts
    from podcasts.library import Library

    home, storage = tmp_path / "home", tmp_path / "storage"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ANYTHINGLLM_STORAGE", str(storage))
    for var in ("PODCASTS_STATE", "PODCASTS_DIR", "PODCASTS_MODELS", "PODCASTS_SOCKET"):
        monkeypatch.delenv(var, raising=False)
    lib = Library.from_env()

    def mount(name: str, path: Path) -> bool:
        """Whether `path` is under a read-only mount of `name`; fails if it isn't mounted."""
        path = str(path).replace(str(home), "%h").replace(str(storage), STORAGE)
        path = path.replace(
            str(Path(transcripts.__file__).resolve().parents[4]), "@REPO@"
        )
        found = [t for t in MOUNTS[name] if path == t or path.startswith(t + "/")]
        assert found, f"{path} isn't mounted in {name}"
        return MOUNTS[name][found[0]]

    for name in MOUNTS:
        for path in (
            lib.state / "queue",
            lib.state / "sync.lock",
            lib.site / "index.html",
        ):
            assert not mount(name, path), (name, path)
    assert not mount(
        "podcasts-runner", hostrpc.socket_path("podcasts", "PODCASTS_SOCKET")
    )
    for name in ("podcasts-sync-worker", "podcasts-transcribe-worker"):
        # AnythingLLM's .env as the code finds it: ANYTHINGLLM_ENV, from the template.
        env = dict(
            e.partition("=")[::2]
            for e in container_keys(QUADLET / f"{name}.container.in")["Environment"]
        )
        assert mount(name, Path(env["ANYTHINGLLM_ENV"].replace("%h", str(home))))
    assert not mount("podcasts-transcribe-worker", library.models_dir("whisper"))
    assert mount("podcasts-transcribe-worker", transcripts.HOST_ENV)
