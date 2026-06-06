#!/usr/bin/env bash
#
# uninstall — fully revert what install.sh did:
#   • stop, disable, and remove the Headroom proxy user service
#   • remove the gateway boot-ordering drop-in
#   • remove the OPENROUTER_BASE_URL block from ~/.hermes/.env (restoring any
#     prior value), and strip the optional MCP entry / any legacy wiring
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

# ── 2. Revert the global routing + any config edits ───────────────────────────
if ! command -v uv >/dev/null 2>&1; then
  warn "uv not found; will still remove the env block, but skip config.yaml edits."
fi

# 2a. Remove our OPENROUTER_BASE_URL managed block from ~/.hermes/.env.
ENVFILE="$HOME/.hermes/.env"
if [[ -f "$ENVFILE" ]]; then
  info "Removing the OPENROUTER_BASE_URL block from ${ENVFILE}…"
  tmp="$(mktemp)"
  awk '/# >>> hermes-headroom >>>/{skip=1} /# <<< hermes-headroom <<</{skip=0; next} !skip' \
    "$ENVFILE" > "$tmp" && mv "$tmp" "$ENVFILE"
  chmod 600 "$ENVFILE"
  success "Removed global routing (any prior OPENROUTER_BASE_URL is restored)."
fi

# 2b. Undo the optional MCP entry + any legacy default-config wiring.
if command -v uv >/dev/null 2>&1; then
  STATE_DIR="$SCRIPT_DIR/.runtime/state"
  # Legacy: older versions rewrote the default model block — restore it if a
  # snapshot exists, then strip the headroom provider/MCP either way.
  if [[ -f "$SCRIPT_DIR/.runtime/model_state.json" ]]; then
    info "Reverting legacy default-config model wiring (if present)…"
    uv run --quiet --with ruamel.yaml -- python "$SCRIPT_DIR/lib/configure_hermes.py" \
      --config "$HOME/.hermes/config.yaml" --state-file "$SCRIPT_DIR/.runtime/model_state.json" \
      --provider-name "$PROVIDER_NAME" restore || true
  fi
  info "Removing the Headroom MCP entry from the default config (if present)…"
  uv run --quiet --with ruamel.yaml -- python "$SCRIPT_DIR/lib/configure_hermes.py" \
    --config "$HOME/.hermes/config.yaml" --provider-name "$PROVIDER_NAME" remove || true

  # Defensive: clean up any per-profile wiring left by an interim version.
  shopt -s nullglob
  for pcfg in "$HOME"/.hermes/profiles/*/config.yaml; do
    if grep -q "custom:$PROVIDER_NAME\|name: $PROVIDER_NAME" "$pcfg" 2>/dev/null; then
      pname="$(basename "$(dirname "$pcfg")")"
      info "Cleaning legacy wiring from profile '${pname}'…"
      uv run --quiet --with ruamel.yaml -- python "$SCRIPT_DIR/lib/configure_hermes.py" \
        --config "$pcfg" --state-file "$STATE_DIR/profile-$pname.json" \
        --provider-name "$PROVIDER_NAME" restore || true
      uv run --quiet --with ruamel.yaml -- python "$SCRIPT_DIR/lib/configure_hermes.py" \
        --config "$pcfg" --provider-name "$PROVIDER_NAME" remove || true
    fi
  done
  shopt -u nullglob
fi

echo ""
success "Reverted. Restart your gateway so it drops the proxy:  hermes gateway restart"
echo ""
