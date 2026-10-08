"""What sets up, syncs and checks this machine, as one command (cli: `uv run hostctl
<command>`): rendering the units (units), syncing storage (sync), setting up apps from the
registry (appctl), the restart guard (run_guard), the machine checks around `uv run hostctl
install` (machine), which workspaces' prompt block is behind (prompt) and the secrets files' checks (agents_env, relay_env).

`uv run` puts hostctl in the dev venv, syncing it
first, so it works on a fresh clone with nothing but uv. The modules still use only the
standard library and hostenv (standard library only too), so health.sh and the apps' `before`
steps can run them with any `python3`, with both src folders on PYTHONPATH:

    PYTHONPATH=packages/hostctl/src:packages/hostenv/src python3 -m hostctl.appctl health
"""
