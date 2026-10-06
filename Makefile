SERVICE := anythingllm.service
CONTAINER := systemd-anythingllm
# This machine's settings (PUBLIC_HOST, ANYTHINGLLM_STORAGE); see host.env.example.
-include host.env
export PUBLIC_HOST ANYTHINGLLM_STORAGE
# Expanded only by targets that touch storage, so `make test` works without host.env.
STORAGE = $(or $(ANYTHINGLLM_STORAGE),$(error no ANYTHINGLLM_STORAGE: copy host.env.example to host.env and fill it in))
# The sites package follows ANYTHINGLLM_STORAGE; naming it here stops a target without host.env.
HOST_SITES_ENV = ANYTHINGLLM_STORAGE=$(STORAGE)
# packages/hostctl, run with the system python3 before any venv exists (standard library only,
# like the apps registry's reader it imports).
PY = PYTHONPATH=$(CURDIR)/packages/hostctl/src:$(CURDIR)/packages/apps/src python3

.PHONY: help install units diff deploy skills skills-check import-skill import-job import-command restart logs status health test test-skills mcp-sync sites-build apps serve-setup sandbox-images

help:            ## list the targets
	@awk -F':.*## ' '/^[a-z%-]+:.*## / { printf "  %-18s %s\n", $$1, $$2 }' $(MAKEFILE_LIST)

install:         ## set this machine up from the repo, or bring it up to date; ends with what's left to do in AnythingLLM's UI
	$(PY) -m hostctl.machine check
	$(MAKE) --no-print-directory units
	$(PY) -m hostctl.machine wait-api
	$(MAKE) --no-print-directory deploy
	$(PY) -m hostctl.machine wait-api
	$(PY) -m hostctl.machine search
	$(MAKE) --no-print-directory serve-setup
	$(PY) -m hostctl.appctl setup --installed
	$(PY) -m hostctl.machine wait-api
	-$(MAKE) --no-print-directory health
	@$(PY) -m hostctl.machine checklist

units:           ## render host/quadlet/ and host/systemd/ into this machine's unit folders (backs up first), reload systemd, restart what changed
	$(PY) -m hostctl.units install

diff: skills-check ## show what deploy would change in live storage, and where the installed units differ from the repo's
	$(PY) -m hostctl.sync diff
	@$(PY) -m hostctl.units diff

deploy: skills-check ## write skills, jobs, slash commands, the system prompt and MCP config live (backs up first), refresh MCP deps, restart AnythingLLM, rebuild the sites
	$(PY) -m hostctl.sync deploy
	$(MAKE) --no-print-directory mcp-sync restart
	$(MAKE) --no-print-directory sites-build

skills:          ## write the agent skills that forward an op to a host service, from the fronts' `skills` (hostrpc.skillgen)
	uv run --all-packages python -m hostctl.skills

skills-check:    ## stop if the generated skills don't match their declarations (diff and deploy run it)
	uv run --all-packages python -m hostctl.skills --check

import-skill:    ## copy a live skill into the repo: make import-skill NAME=foo
	$(PY) -m hostctl.sync import-skill $(NAME)

import-job:      ## copy a live scheduled job into the repo: make import-job NAME="Daily News Page"
	$(PY) -m hostctl.sync import-job "$(NAME)"

import-command:  ## copy a live slash command into the repo: make import-command NAME=/foo
	$(PY) -m hostctl.sync import-command "$(NAME)"

restart:         ## restart AnythingLLM (deep-research runs carry on: they run in research-runner)
	systemctl --user restart $(SERVICE)

logs:            ## follow AnythingLLM's container log
	podman logs -f --tail 100 $(CONTAINER)

status:          ## show AnythingLLM's unit status
	systemctl --user status $(SERVICE) --no-pager

health:          ## check every app's units, ports and runners, and each MCP server (e.g. after a reboot)
	packages/hostctl/health.sh

test:            ## run tests for all MCP servers and agent skills
	uv run --all-packages --all-extras pytest -q
	$(MAKE) --no-print-directory test-skills

test-skills:     ## run agent skill and log filter tests inside the AnythingLLM container (its Node, repo at /mcp)
	podman exec -e NODE_OPTIONS= -w /tmp $(CONTAINER) node --test /mcp/anythingllm/

# The container's uv syncs one --package at a time: the first sync is exact (it removes
# whatever no MCP server needs), the rest only add.
mcp-sync:        ## install/refresh the MCP servers' deps inside the AnythingLLM container, and only theirs
	set -e; mode=; for pkg in $$($(PY) -m hostctl.sync mcp-packages); do \
	  podman exec -w /tmp -e UV_PROJECT_ENVIRONMENT=/app/server/storage/everythingllm/mcp/venv \
	    -e UV_CACHE_DIR=/app/server/storage/everythingllm/mcp/uv-cache -e UV_PYTHON_DOWNLOADS=never \
	    $(CONTAINER) uv sync --frozen --no-dev --package $$pkg $$mode --project /mcp; \
	  mode=--inexact; done


# $(call internal-net,name,subnet): a podman network with no route out and no DNS.
internal-net = podman network exists $(1) || podman network create --internal --disable-dns --subnet $(2) $(1)

SANDBOX := host/containers/sandbox

# The apps (packages/apps/src/apps/apps.toml) are set up, mapped and followed by
# hostctl.appctl: `make apps` lists them.
apps:            ## list the apps, for make <app>-setup and make <app>-logs
	@$(PY) -m hostctl.appctl list

%-setup: units   ## set an app up: its steps, tailnet paths, units (restarted; asks first while one of its runs is going, FORCE=1 doesn't) and timers
	$(PY) -m hostctl.appctl setup $*

%-logs:          ## follow an app's units (make apps lists them)
	$(PY) -m hostctl.appctl logs $*

serve-setup:     ## map the apps' tailnet HTTPS paths with tailscale serve (other mappings are left alone)
	$(PY) -m hostctl.appctl serve

sandbox-images:  ## build the sandbox's images and its internal network (make sandbox-setup runs this first)
	podman build -t localhost/everythingllm-sandbox -f $(SANDBOX)/Containerfile.sandbox $(SANDBOX)
	podman build -t localhost/everythingllm-sandbox-proxy -f $(SANDBOX)/Containerfile.proxy $(SANDBOX)
	$(call internal-net,sandbox-net,10.89.77.0/24)

sites-build:     ## rebuild all Zola sites by hand (sites-runner does this on every write)
	$(HOST_SITES_ENV) uv run --package sites sites-build
