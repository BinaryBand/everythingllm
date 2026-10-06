SERVICE := anythingllm.service
CONTAINER := systemd-anythingllm
# This machine's settings (PUBLIC_HOST, ANYTHINGLLM_STORAGE); see host.env.example.
-include host.env
export PUBLIC_HOST ANYTHINGLLM_STORAGE
# Expanded only by targets that touch storage, so `make test` works without host.env.
STORAGE = $(or $(ANYTHINGLLM_STORAGE),$(error no ANYTHINGLLM_STORAGE: copy host.env.example to host.env and fill it in))
# The sites package follows ANYTHINGLLM_STORAGE; naming it here stops a target without host.env.
HOST_SITES_ENV = ANYTHINGLLM_STORAGE=$(STORAGE)

.PHONY: help install units diff deploy skills import-skill import-job import-command restart logs status health test test-skills mcp-sync sites-build apps serve-setup sandbox-images

help:            ## list the targets
	@awk -F':.*## ' '/^[a-z%-]+:.*## / { printf "  %-18s %s\n", $$1, $$2 }' $(MAKEFILE_LIST)

install:         ## set this machine up from the repo, or bring it up to date; ends with what's left to do in AnythingLLM's UI
	python3 tools/machine.py check
	$(MAKE) --no-print-directory units
	python3 tools/machine.py wait-api
	$(MAKE) --no-print-directory deploy
	python3 tools/machine.py wait-api
	python3 tools/machine.py search
	$(MAKE) --no-print-directory serve-setup
	python3 tools/appctl.py setup --installed
	python3 tools/machine.py wait-api
	-$(MAKE) --no-print-directory health
	@python3 tools/machine.py checklist

units:           ## render host/quadlet/ and host/systemd/ into this machine's unit folders (backs up first), reload systemd, restart what changed
	python3 tools/units.py install

diff:            ## show what deploy would change in live storage, and where the installed units differ from the repo's
	uv run --all-packages python tools/skills.py --check
	python3 tools/sync.py diff
	@python3 tools/units.py diff

deploy:          ## write skills, jobs, slash commands, the system prompt and MCP config live (backs up first), refresh MCP deps, restart AnythingLLM, rebuild the sites
	uv run --all-packages python tools/skills.py --check
	python3 tools/sync.py deploy
	$(MAKE) --no-print-directory mcp-sync restart
	$(MAKE) --no-print-directory sites-build

skills:          ## write the agent skills that forward an op to a host service, from the fronts' `skills` (hostrpc.skillgen)
	uv run --all-packages python tools/skills.py

import-skill:    ## copy a live skill into the repo: make import-skill NAME=foo
	python3 tools/sync.py import-skill $(NAME)

import-job:      ## copy a live scheduled job into the repo: make import-job NAME="Daily News Page"
	python3 tools/sync.py import-job "$(NAME)"

import-command:  ## copy a live slash command into the repo: make import-command NAME=/foo
	python3 tools/sync.py import-command "$(NAME)"

restart:         ## restart AnythingLLM (deep-research runs carry on: they run in research-runner)
	systemctl --user restart $(SERVICE)

logs:            ## follow AnythingLLM's container log
	podman logs -f --tail 100 $(CONTAINER)

status:          ## show AnythingLLM's unit status
	systemctl --user status $(SERVICE) --no-pager

health:          ## check every app's units, ports and runners, and each MCP server (e.g. after a reboot)
	tools/health.sh

test:            ## run tests for all MCP servers and agent skills
	uv run --all-packages --all-extras pytest -q
	$(MAKE) --no-print-directory test-skills

test-skills:     ## run agent skill and log filter tests inside the AnythingLLM container (its Node, repo at /mcp)
	podman exec -e NODE_OPTIONS= -w /tmp $(CONTAINER) node --test /mcp/anythingllm/

# The container's uv syncs one --package at a time: the first sync is exact (it removes
# whatever no MCP server needs), the rest only add.
mcp-sync:        ## install/refresh the MCP servers' deps inside the AnythingLLM container, and only theirs
	set -e; mode=; for pkg in $$(python3 tools/sync.py mcp-packages); do \
	  podman exec -w /tmp -e UV_PROJECT_ENVIRONMENT=/app/server/storage/everythingllm/mcp/venv \
	    -e UV_CACHE_DIR=/app/server/storage/everythingllm/mcp/uv-cache -e UV_PYTHON_DOWNLOADS=never \
	    $(CONTAINER) uv sync --frozen --no-dev --package $$pkg $$mode --project /mcp; \
	  mode=--inexact; done


# $(call internal-net,name,subnet): a podman network with no route out and no DNS.
internal-net = podman network exists $(1) || podman network create --internal --disable-dns --subnet $(2) $(1)

SANDBOX := host/containers/sandbox

# The apps (packages/apps/src/apps/apps.toml) are set up, mapped and followed by
# tools/appctl.py: `make apps` lists them.
apps:            ## list the apps, for make <app>-setup and make <app>-logs
	@python3 tools/appctl.py list

%-setup: units   ## set an app up: its steps, tailnet paths, units (restarted; asks first while one of its runs is going, FORCE=1 doesn't) and timers
	python3 tools/appctl.py setup $*

%-logs:          ## follow an app's units (make apps lists them)
	python3 tools/appctl.py logs $*

serve-setup:     ## map the apps' tailnet HTTPS paths with tailscale serve (other mappings are left alone)
	python3 tools/appctl.py serve

sandbox-images:  ## build the sandbox's images and its internal network (make sandbox-setup runs this first)
	podman build -t localhost/everythingllm-sandbox -f $(SANDBOX)/Containerfile.sandbox $(SANDBOX)
	podman build -t localhost/everythingllm-sandbox-proxy -f $(SANDBOX)/Containerfile.proxy $(SANDBOX)
	$(call internal-net,sandbox-net,10.89.77.0/24)

sites-build:     ## rebuild all Zola sites by hand (sites-runner does this on every write)
	$(HOST_SITES_ENV) uv run --package sites sites-build
