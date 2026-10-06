"""`make agents-setup`'s first step: is agents-runner's secrets file there and private?

~/.config/everythingllm/agents.env holds ANYTHINGLLM_API_KEY, a developer API key made in
AnythingLLM's Settings > Developer API and put there by hand; the agent never writes it.
This exits 1 (so the unit isn't started into a crash loop) while the file or the key is
missing, and makes the file private (mode 600). It never prints the key. Standard library
only, run with the system python3.

    python3 -m hostctl.agents_env [path]   # with hostctl and apps on PYTHONPATH, as make does
"""

import sys
from pathlib import Path

from hostctl.units import env_file

DEFAULT = Path("~/.config/everythingllm/agents.env").expanduser()


def main() -> None:
    path = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else DEFAULT
    if not path.exists():
        sys.exit(
            f"agents: make {path} (mode 600) with ANYTHINGLLM_API_KEY=<a developer API key from "
            "AnythingLLM's Settings > Developer API>, then run make agents-setup again"
        )
    path.chmod(0o600)
    if not env_file(path).get("ANYTHINGLLM_API_KEY"):
        sys.exit(
            f"agents: fill in ANYTHINGLLM_API_KEY in {path}, then run make agents-setup again"
        )
    print(f"{path} has what agents-runner needs")


if __name__ == "__main__":
    main()
