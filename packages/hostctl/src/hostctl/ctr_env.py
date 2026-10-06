"""A service container's share of AnythingLLM's .env: the few keys it needs, never the file.

AnythingLLM's .env holds every provider's key, its password and its signing secrets. The
containers that parse the web, feeds and audio need one or two of them, so each template's
ExecStartPre runs this on the host before every start, and the container mounts the file
it writes read-only, with ANYTHINGLLM_ENV naming it (README, "Service containers"):

    python3 -m hostctl.ctr_env <AnythingLLM's .env> <out file> KEY... [KEY?...]

A plain KEY is copied as it is. `KEY?` is a key whose value is never needed, only whether
it's set (hostrpc.anythingllm_headers takes a set JWT_SECRET to mean the password is on):
it's written as `set` when it is, so the secret itself stays on the host. A key that isn't
set is left out. The file is mode 600 in a mode 700 folder and replaced whole, so a
restart takes up a key changed in AnythingLLM's settings. With hostctl and apps on
PYTHONPATH, as the templates run it; standard library only, like the rest of hostctl.
"""

import os
import sys
import tempfile
from pathlib import Path

from hostctl.units import env_file

PRESENT = "set"  # what a `KEY?` gets when the .env has it


def share(env: dict[str, str], keys: list[str]) -> str:
    """The lines a container gets of `env` for `keys` (KEY, or KEY? for presence only)."""
    lines = []
    for key in keys:
        name = key.removesuffix("?")
        if not name.replace("_", "").isalnum():
            raise ValueError(f"not a key: {key!r}")
        if env.get(name):
            lines.append(f"{name}={PRESENT if key.endswith('?') else env[name]}")
    return "".join(f"{line}\n" for line in lines)


def write(out: Path, text: str) -> None:
    """Replace `out` with `text`, mode 600, in a mode 700 folder."""
    out.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    out.parent.chmod(0o700)
    fd, tmp = tempfile.mkstemp(dir=out.parent, prefix=f".{out.name}.")
    try:
        with os.fdopen(fd, "w") as f:  # mkstemp makes it 600
            f.write(text)
        os.replace(tmp, out)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    if len(args) < 3:
        sys.exit("usage: python3 -m hostctl.ctr_env <.env> <out file> KEY... [KEY?...]")
    source, out, keys = Path(args[0]), Path(args[1]), args[2:]
    env = env_file(source)
    if not env:
        print(f"ctr_env: nothing read from {source}; {out.name} gets no keys")
    write(out, share(env, keys))


if __name__ == "__main__":
    main()
