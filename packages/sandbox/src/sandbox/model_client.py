"""Ask a model from code in the sandbox (standard library only; it runs with the image's python).

The sandbox runner copies this file into a run's /sandbox as everythingllm_models.py when the
workspace has model access, and points EVERYTHINGLLM_MODELS at the run's own socket to the
server (sandbox.models). From Python:

    from everythingllm_models import ask
    print(ask("Summarize this in one line: ...", system="Be brief."))

From bash:

    python3 /sandbox/everythingllm_models.py "Say hi"
    some-command | python3 /sandbox/everythingllm_models.py - --model glm-5.3

Each call counts toward the workspace's tokens for the day; a call past them, a model the
server doesn't offer or a request too big fails with RuntimeError, saying why.
"""

import json
import os
import socket
import sys

MODELS = os.environ.get("EVERYTHINGLLM_MODELS", "")


def call(messages, model=None, max_tokens=None):
    """The server's whole answer: {text, model, tokens, tokens_left}."""
    if not MODELS:
        raise RuntimeError(
            "this workspace has no model access (the user can turn it on with sandbox-access)"
        )
    args = {"messages": messages, "model": model, "max_tokens": max_tokens}
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.connect(MODELS)
        s.sendall(json.dumps({"op": "ask", "args": args}).encode() + b"\n")
        with s.makefile("rb") as f:
            line = f.readline()
    if not line:
        raise RuntimeError("the model server closed the connection without answering")
    reply = json.loads(line)
    if not reply.get("ok"):
        raise RuntimeError(reply.get("error") or "the model server refused")
    return reply["result"]


def ask(prompt=None, *, messages=None, system=None, model=None, max_tokens=None):
    """The model's answer to `prompt` (with `system` before it), or to `messages`, a list
    of {role, content}."""
    if messages is None:
        messages = [{"role": "user", "content": prompt or ""}]
        if system:
            messages.insert(0, {"role": "system", "content": system})
    return call(messages, model, max_tokens)["text"]


def main(argv):
    model = None
    if "--model" in argv:
        i = argv.index("--model")
        model = argv[i + 1] if i + 1 < len(argv) else None
        argv = argv[:i] + argv[i + 2 :]
    if len(argv) != 1:
        sys.exit(
            'usage: python3 everythingllm_models.py "prompt" [--model name]  ("-" reads stdin)'
        )
    prompt = sys.stdin.read() if argv[0] == "-" else argv[0]
    try:
        print(ask(prompt, model=model))
    except RuntimeError as e:
        sys.exit(f"error: {e}")


if __name__ == "__main__":
    main(sys.argv[1:])
