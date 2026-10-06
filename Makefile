# Every target is a hostctl command of the same name (packages/hostctl/src/hostctl/cli.py),
# run with the system python3 before any venv exists. `make` lists them.
# import-skill, import-job and import-command take NAME=…; FORCE=1 passes through.
HOSTCTL = PYTHONPATH=$(CURDIR)/packages/hostctl/src:$(CURDIR)/packages/apps/src python3 -m hostctl

.DEFAULT_GOAL := help
.PHONY: help
Makefile: ;

help:
	@$(HOSTCTL)

import-%:
	@$(HOSTCTL) $@ "$(NAME)"

%:
	@$(HOSTCTL) $@
