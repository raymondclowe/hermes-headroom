#!/usr/bin/env bash
#
# uninstall — fully revert what install.sh did:
#   • stop, disable, and remove the Headroom proxy user service
#   • remove the gateway boot-ordering drop-in
#   • restore Hermes' previous model selection and remove the headroom
#     provider + MCP entry from ~/.hermes/config.yaml
#
# Safe to run any time. After it finishes, restart your gateway.
#
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

STATE_FILE="$SCRIPT_DIR/.runtime/model_state.json"
PROVIDER_NAME="headroom"
UNIT_NAME="headroom-proxy.service"
USER_UNIT_DIR="$HOME/.config/systemd/user"

# ── 1. Stop + remove the proxy service ────────────────────────────────────────
if systemctl --user show-environment >/dev/null 2>&1; then
  systemctl --user disable --now "$UNIT_NAME" 2>/dev/null || true
  rm -f "$USER_UNIT_DIR/$UNIT_NAME"
  rm -f "$USER_UNIT_DIR/hermes-gateway.service.d/10-headroom.conf"
  rmdir "$USER_UNIT_DIR/hermes-gateway.service.d" 2>/dev/null || true
  systemctl --user daemon-reload || true
  success "Removed the Headroom proxy service and boot-ordering link."
else
  warn "Could not reach 'systemctl --user'; remove these by hand if present:"
  echo "    $USER_UNIT_DIR/$UNIT_NAME"
  echo "    $USER_UNIT_DIR/hermes-gateway.service.d/10-headroom.conf"
fi

# ── 2. Revert the Hermes config ───────────────────────────────────────────────
if ! command -v uv >/dev/null 2>&1; then
  error "uv is required to edit the Hermes config. Install uv and re-run."
  exit 1
fi

info "Restoring your previous model selection (if Headroom is still active)…"
uv run --quiet --with ruamel.yaml -- \
  python "$SCRIPT_DIR/lib/configure_hermes.py" \
  --state-file "$STATE_FILE" --provider-name "$PROVIDER_NAME" restore || true

info "Removing the 'headroom' provider + MCP entry from ~/.hermes/config.yaml…"
uv run --quiet --with ruamel.yaml -- \
  python "$SCRIPT_DIR/lib/configure_hermes.py" \
  --provider-name "$PROVIDER_NAME" remove || true

echo ""
success "Reverted. Restart your gateway so it drops the proxy:  hermes gateway restart"
echo ""
