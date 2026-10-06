SERVICE := anythingllm.service
CONTAINER := systemd-anythingllm
# This machine's settings (PUBLIC_HOST, ANYTHINGLLM_STORAGE); see host.env.example.
-include host.env
export PUBLIC_HOST ANYTHINGLLM_STORAGE
# Expanded only by targets that touch storage, so `make test` works without host.env.
STORAGE = $(or $(ANYTHINGLLM_STORAGE),$(error no ANYTHINGLLM_STORAGE: copy host.env.example to host.env and fill it in))
# The sites package follows ANYTHINGLLM_STORAGE; naming it here stops a target without host.env.
HOST_SITES_ENV = ANYTHINGLLM_STORAGE=$(STORAGE)

.PHONY: help install units diff deploy import-skill import-job import-command restart logs status health test test-skills mcp-sync claude-rc-logs sites-build serve-setup claude-rc-setup sandbox-setup sandbox-logs podcasts-setup podcasts-logs podcasts-web-logs news-audio-setup news-audio-logs research-setup sites-setup audit-setup relay-setup

help:            ## list the targets
	@awk -F':.*## ' '/^[a-z%-]+:.*## / { printf "  %-18s %s\n", $$1, $$2 }' $(MAKEFILE_LIST)

install:         ## set this machine up from the repo, or bring it up to date; ends with what's left to do in AnythingLLM's UI
	python3 scripts/machine.py check
	$(MAKE) --no-print-directory units
	python3 scripts/machine.py wait-api
	$(MAKE) --no-print-directory deploy
	python3 scripts/machine.py wait-api
	python3 scripts/machine.py search
	$(MAKE) --no-print-directory serve-setup sandbox-setup podcasts-setup news-audio-setup research-setup sites-setup audit-setup
	python3 scripts/machine.py wait-api
	-$(MAKE) --no-print-directory health
	@python3 scripts/machine.py checklist

units:           ## render host/quadlet/ and host/systemd/ into this machine's unit folders (backs up first), reload systemd, restart what changed
	python3 scripts/units.py install

diff:            ## show what deploy would change in live storage, and where the installed units differ from the repo's
	python3 scripts/sync.py diff
	@python3 scripts/units.py diff

deploy:          ## write skills, jobs, slash commands, the system prompt and MCP config live (backs up first), refresh MCP deps, restart AnythingLLM, rebuild the sites
	python3 scripts/sync.py deploy
	$(MAKE) --no-print-directory mcp-sync restart
	$(MAKE) --no-print-directory sites-build

import-skill:    ## copy a live skill into the repo: make import-skill NAME=foo
	python3 scripts/sync.py import-skill $(NAME)

import-job:      ## copy a live scheduled job into the repo: make import-job NAME="Daily News Page"
	python3 scripts/sync.py import-job "$(NAME)"

import-command:  ## copy a live slash command into the repo: make import-command NAME=/foo
	python3 scripts/sync.py import-command "$(NAME)"

restart:         ## restart AnythingLLM (deep-research runs carry on: they run in research-runner)
	systemctl --user restart $(SERVICE)

logs:
	podman logs -f --tail 100 $(CONTAINER)

status:          ## show AnythingLLM's unit status
	systemctl --user status $(SERVICE) --no-pager

health:          ## check every unit, local port, the sandbox and research runners and each MCP server (e.g. after a reboot)
	scripts/health.sh

test:            ## run tests for all MCP servers and agent skills
	uv run --all-packages --all-extras pytest -q
	$(MAKE) --no-print-directory test-skills

test-skills:     ## run agent skill and log filter tests inside the AnythingLLM container (its Node, repo at /mcp)
	podman exec -e NODE_OPTIONS= -w /tmp $(CONTAINER) node --test /mcp/anythingllm/

# The container's uv syncs one --package at a time: the first sync is exact (it removes
# whatever no MCP server needs), the rest only add.
mcp-sync:        ## install/refresh the MCP servers' deps inside the AnythingLLM container, and only theirs
	set -e; mode=; for pkg in $$(python3 scripts/sync.py mcp-packages); do \
	  podman exec -w /tmp -e UV_PROJECT_ENVIRONMENT=/app/server/storage/mcp/venv \
	    -e UV_CACHE_DIR=/app/server/storage/mcp/uv-cache -e UV_PYTHON_DOWNLOADS=never \
	    $(CONTAINER) uv sync --frozen --no-dev --package $$pkg $$mode --project /mcp; \
	  mode=--inexact; done


claude-rc-logs:  ## follow the Claude Remote Control service (host, systemd user unit)
	journalctl --user -fu claude-rc.service

# $(call enable-restart,units): enable user units and (re)start them.
enable-restart = systemctl --user enable $(1) && systemctl --user restart $(1)

# $(call internal-net,name,subnet): a podman network with no route out and no DNS.
internal-net = podman network exists $(1) || podman network create --internal --disable-dns --subnet $(2) $(1)

SANDBOX := src/mcps/sandbox/containers
SANDBOX_UNITS := sandbox-proxy.service sandbox-runner.service

sandbox-setup: units ## build the sandbox images and network, enable and (re)start its host units
	podman build -t localhost/everythingllm-sandbox -f $(SANDBOX)/Containerfile.sandbox $(SANDBOX)
	podman build -t localhost/everythingllm-sandbox-proxy -f $(SANDBOX)/Containerfile.proxy $(SANDBOX)
	$(call internal-net,sandbox-net,10.89.77.0/24)
	$(call enable-restart,$(SANDBOX_UNITS))

sandbox-logs:    ## follow the sandbox runner and its proxy (host, systemd user units)
	journalctl --user -fu sandbox-runner.service -u sandbox-proxy.service

claude-rc-setup: units ## optional: enable Claude Remote Control for this repo (log in with `claude` first)
	$(call enable-restart,claude-rc.service)

serve-setup:     ## map this setup's tailnet HTTPS ports with tailscale serve (other mappings are left alone)
	tailscale serve status | grep -q ':8445 ' || sudo tailscale serve --bg --https=8445 http://127.0.0.1:8445
	tailscale serve status | grep -q '/news/write' || \
	  sudo tailscale serve --bg --https=8445 --set-path=/news/write http://127.0.0.1:8448
	tailscale serve status | grep -q '/podcasts' || \
	  sudo tailscale serve --bg --https=8445 --set-path=/podcasts http://127.0.0.1:8449
	tailscale serve status | grep -q ':8888 ' || sudo tailscale serve --bg --https=8888 http://127.0.0.1:8888
	tailscale serve status | grep -q ':3001 ' || sudo tailscale serve --bg --https=3001 http://127.0.0.1:3001
	tailscale serve status | grep -q ':8446 ' || sudo tailscale serve --bg --https=8446 http://127.0.0.1:8446

podcasts-setup: units serve-setup ## enable and (re)start podcasts-runner and podcasts-web (:8445/podcasts), and start the 6-hourly sync and transcription timers
	$(call enable-restart,podcasts-runner.service podcasts-web.service)
	systemctl --user enable --now podcasts-sync.timer podcasts-transcribe.timer

podcasts-logs:   ## follow podcasts-runner and the transcription runs (the syncs log to storage/podcasts/sync.log)
	journalctl --user -fu podcasts-runner.service -u podcasts-transcribe.service

podcasts-web-logs: ## follow podcasts-web, which serves the podcasts
	journalctl --user -fu podcasts-web.service

news-audio-setup: units ## enable and start the timer that reads each Daily News edition aloud
	systemctl --user enable --now news-audio.timer

news-audio-logs: ## follow the Daily News read-aloud runs
	journalctl --user -fu news-audio.service

research-setup: units ## enable and (re)start research-runner, which runs deep research for the skill; asks first while a run is going (FORCE=1 doesn't)
	systemctl --user enable research-runner.service
	python3 scripts/research_guard.py
	systemctl --user restart research-runner.service

sites-setup: units serve-setup ## enable and (re)start sites-runner (and the article writer it serves at :8445/news/write), which writes the sites' entries and builds them for the sites MCP server
	$(call enable-restart,sites-runner.service)

audit-setup: units ## enable and (re)start audit-runner, which runs the audit MCP server's checks on the host
	$(call enable-restart,audit-runner.service)

relay-setup: units serve-setup ## make the Nilson relay's secrets file (~/.config/everythingllm/relay.env) if missing, then enable and (re)start the relay (tailnet https :8446)
	python3 scripts/relay_env.py
	$(call enable-restart,relay.service)

%-logs:          ## follow <name>-runner or <name> (research, sites, audit, relay)
	journalctl --user -f -u $*-runner.service -u $*.service

sites-build:     ## rebuild all Zola sites by hand (sites-runner does this on every write)
	$(HOST_SITES_ENV) uv run --package sites sites-build
