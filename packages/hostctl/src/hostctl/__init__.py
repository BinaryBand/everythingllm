"""What sets up, syncs and checks this machine, as one command (cli: `python3 -m hostctl
<command>`, which each make target is an alias for): rendering the units (units), syncing
storage (sync), setting up apps from the registry (appctl), the restart guard (run_guard),
the machine checks around `make install` (machine) and the secrets files' checks
(agents_env, relay_env).

They run before any venv exists, so they use only the standard library and run with the
system `python3`, with this package and packages/apps (the registry's reader, standard library
only too) on PYTHONPATH, as the Makefile's HOSTCTL sets them:

    PYTHONPATH=packages/hostctl/src:packages/apps/src python3 -m hostctl diff

The exception is skills, which imports the fronts and runs in the dev venv (`make skills`).
"""
