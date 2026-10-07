"""What sets up, syncs and checks this machine, as one command (cli: `uv run hostctl
<command>`): rendering the units (units), syncing storage (sync), setting up apps from the
registry (appctl), the restart guard (run_guard), the machine checks around `uv run hostctl
install` (machine), the system prompt's block and version (prompt) and the secrets files' checks (agents_env, relay_env).

`uv run` puts hostctl in the dev venv, syncing it
first, so it works on a fresh clone with nothing but uv. The modules still use only the
standard library, so health.sh and the apps' `before` steps can run them with any `python3`,
with hostctl's src on PYTHONPATH:

    PYTHONPATH=packages/hostctl/src python3 -m hostctl.appctl health

The exception is skills, which imports the fronts, so it runs in the whole workspace's venv
(`uv run hostctl skills`).
"""
