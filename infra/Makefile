# f10 infrastructure - one entrypoint for the provisioning stages.
#
#   Stage 1  Terraform  create the droplet          make apply
#   (seam)   gen_inventory  terraform -> ansible     make inventory
#   Stage 2  Ansible    configure the droplet        make deploy
#
# `make provision` runs stage 1 + the seam in one go.
#
# Secrets: copy .env.example -> .env (gitignored) and fill it in. This
# Makefile loads .env and exports it, so DIGITALOCEAN_TOKEN (and the rest)
# reach terraform/ansible with no manual `export`. You can still export the
# token in your shell instead if you prefer.
#
# Requires: terraform. Stage 2 also requires ansible (ansible-playbook).

# Load infra/.env if present and export every value to recipe commands.
# `-include` is a no-op when the file is absent. .env uses KEY=value lines.
-include .env
export

TF      := terraform -chdir=terraform
ANSIBLE_DIR := ansible

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------- stage 1
.PHONY: init
init: ## terraform init (run once, and after provider changes)
	$(TF) init

.PHONY: fmt
fmt: ## terraform fmt (format the .tf files)
	$(TF) fmt

.PHONY: plan
plan: ## Show what terraform would change
	$(TF) plan

.PHONY: apply
apply: ## Create/update the droplet
	$(TF) apply

.PHONY: destroy
destroy: ## Tear the droplet down
	$(TF) destroy

.PHONY: output
output: ## Show terraform outputs
	$(TF) output

# ------------------------------------------------------------------- seam
.PHONY: inventory
inventory: ## Write the Ansible inventory from terraform output
	python3 scripts/gen_inventory.py

.PHONY: provision
provision: apply inventory ## Stage 1 + seam: create the droplet and build the inventory

# ---------------------------------------------------------------- stage 2
.PHONY: ping
ping: ## Check Ansible can reach the droplet
	cd $(ANSIBLE_DIR) && ansible all -m ping

.PHONY: deploy
deploy: ## Configure the droplet (run the playbook)
	cd $(ANSIBLE_DIR) && ansible-playbook site.yml

# ------------------------------------------------------------------ checks
.PHONY: validate
validate: ## Static checks: terraform validate/fmt
	$(TF) fmt -check
	$(TF) validate
