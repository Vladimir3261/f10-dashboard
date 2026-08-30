# f10 infrastructure - the ANALYTICS SERVER. Provisioning the Raspberry Pi
# lives in ../hardware (`cd hardware && make pi-setup`).
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

# One source of truth for the Grafana allowlist: GF_ALLOWED_IPS in .env
# drives BOTH the DigitalOcean firewall (through Terraform) and the nginx
# allow/deny rules (Ansible reads the env var directly).
TF_VAR_grafana_allowed_cidrs := $(GF_ALLOWED_IPS)
TF_VAR_grafana_public_port   := $(if $(GF_PUBLIC_PORT),$(GF_PUBLIC_PORT),80)
# Both domains as one comma-separated value; Terraform compacts the empties,
# so "", "a," and "," all behave correctly.
TF_VAR_tls_domains := $(GRAFANA_DOMAIN),$(DASHBOARD_DOMAIN)
export TF_VAR_grafana_allowed_cidrs TF_VAR_grafana_public_port TF_VAR_tls_domains

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

.PHONY: do-keys
do-keys: ## List SSH keys registered on your DigitalOcean account
	@curl -sS -H "Authorization: Bearer $$DIGITALOCEAN_TOKEN" \
	  "https://api.digitalocean.com/v2/account/keys?per_page=100" \
	  | python3 -c "import json,sys; d=json.load(sys.stdin); \
[print('  %-24s %s' % (k['name'], k['fingerprint'])) for k in d.get('ssh_keys', [])] \
or print('  (no keys, or API error: %s)' % d.get('message',''))"

# ------------------------------------------------------------------- seam
.PHONY: inventory
inventory: ## Write the Ansible inventory from terraform output
	python3 scripts/gen_inventory.py

.PHONY: provision
provision: apply inventory ## Stage 1 + seam: create the droplet and build the inventory

# ---------------------------------------------------------------- stage 2
.PHONY: galaxy
galaxy: ## Install the Ansible collections the playbook needs
	cd $(ANSIBLE_DIR) && ansible-galaxy collection install -r requirements.yml

.PHONY: ping
ping: ## Check Ansible can reach the droplet
	cd $(ANSIBLE_DIR) && ansible all -m ping

.PHONY: deploy
deploy: galaxy ## Configure the droplet: hardening + Docker + stack + WireGuard
	cd $(ANSIBLE_DIR) && ansible-playbook site.yml

.PHONY: deploy-check
deploy-check: galaxy ## Dry-run the playbook (--check, no changes)
	cd $(ANSIBLE_DIR) && ansible-playbook site.yml --check

# ------------------------------------------------------------------- lake
.PHONY: lake-status
lake-status: ## Show stack health + row counts on the analytics server
	@scripts/lake_status.sh

.PHONY: lake-migrate
lake-migrate: ## Apply the ClickHouse schema migrations (idempotent)
	@scripts/lake_migrate.sh $(ARGS)

.PHONY: lake-migrate-check
lake-migrate-check: ## List the migrations that would be applied
	@scripts/lake_migrate.sh --dry-run

.PHONY: migrate-lake
migrate-lake: ## Copy the lake from an old server: make migrate-lake FROM=root@old-host
	@test -n "$(FROM)" || { echo "usage: make migrate-lake FROM=root@old-host [ARGS='--dry-run']"; exit 1; }
	scripts/migrate_lake.sh --from "$(FROM)" $(ARGS)

# ------------------------------------------------------------------ checks
.PHONY: validate
validate: ## Static checks: terraform validate/fmt
	$(TF) fmt -check
	$(TF) validate
