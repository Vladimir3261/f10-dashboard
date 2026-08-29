# f10 hardware — the physical devices, as opposed to the server.
#
#   infra/     the analytics server (Terraform + Ansible)
#   hardware/  the Raspberry Pi in the car, and future boards  <- you are here
#
# Targets here run on YOUR LAPTOP and produce things you then apply to a
# device. The scripts that run ON the Pi live in
# raspberry-pi/f10pi/scripts/ and are invoked by the generated setup script.

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

.PHONY: pi-setup
pi-setup: ## Generate local/pi-setup.sh for the Raspberry Pi from current infra state
	python3 scripts/gen_pi_setup.py

.PHONY: check
check: ## Syntax-check the on-device shell scripts
	@for f in raspberry-pi/f10pi/scripts/*.sh; do \
	  bash -n "$$f" && echo "  ok  $$f" || exit 1; \
	done
