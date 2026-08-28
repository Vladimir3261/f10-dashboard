#!/usr/bin/env bash
# Shared helpers for the f10pi provisioning scripts. Sourced, not run.
# No secrets here.

set -euo pipefail

# Resolve directories relative to this file so scripts work from anywhere.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
F10PI_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
CONFIG_DIR="${F10PI_DIR}/config"

log()  { printf '\033[1;34m[f10pi]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[f10pi] WARN:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[f10pi] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

require_root() {
  [[ ${EUID} -eq 0 ]] || die "must run as root (use sudo)"
}

# Source a config file if it exists; warn (don't fail) if only the template
# is present, so scripts can still be inspected on a dev machine.
load_config() {
  local name="$1" real="${CONFIG_DIR}/$1"
  if [[ -f "${real}" ]]; then
    # shellcheck disable=SC1090
    set -a; source "${real}"; set +a
  else
    warn "missing ${real} — copy the .example and fill it in"
    return 1
  fi
}

have() { command -v "$1" >/dev/null 2>&1; }
