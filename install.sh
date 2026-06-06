#!/usr/bin/env bash
#
# install — make Headroom a PERSISTENT, global layer in front of Hermes.
#
# It does two things, once:
#   1. installs + starts the Headroom proxy as a `systemd --user` service so it
#      is always running (just like your hermes-gateway service)
#   2. sets OPENROUTER_BASE_URL in ~/.hermes/.env — the single global lever that
#      routes every Hermes process through that proxy
#
# After this, EVERYTHING Hermes routes via OpenRouter — the gateway, Discord,
# all profiles, crons, kanban workers, delegated subagents, and the plain
# `hermes` CLI — flows through Headroom automatically. No per-profile edits, and
# it survives profile/model changes. Run ./uninstall.sh to fully revert.
#
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

RUNTIME_DIR="$SCRIPT_DIR/.runtime"
STATE_FILE="$RUNTIME_DIR/model_state.json"
PROVIDER_NAME="headroom"
UNIT_NAME="headroom-proxy.service"
USER_UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$RUNTIME_DIR" "$USER_UNIT_DIR"

# ── 1. Load + validate settings ───────────────────────────────────────────────
if [[ ! -f "$SCRIPT_DIR/.env" ]]; then
  error "No .env found. Run ./setup.sh first (it creates one and asks for your key)."
  exit 1
fi
set -a; source "$SCRIPT_DIR/.env"; set +a
: "${OPENROUTER_API_KEY:?Set OPENROUTER_API_KEY in .env (run ./setup.sh)}"
: "${MODEL:?Set MODEL in .env (run ./setup.sh)}"
PORT="${PORT:-8787}"
CONTEXT_LENGTH="${CONTEXT_LENGTH:-1000000}"
ENABLE_MCP="${ENABLE_MCP:-1}"
BASE_URL="http://127.0.0.1:${PORT}/v1"
PROXY_BASE="http://127.0.0.1:${PORT}"

# ── 2. Pre-flight checks ──────────────────────────────────────────────────────
command -v headroom >/dev/null 2>&1 || { error "headroom not installed. Run ./setup.sh."; exit 1; }
command -v hermes   >/dev/null 2>&1 || { error "hermes not installed. See github.com/NousResearch/hermes-agent"; exit 1; }
command -v uv       >/dev/null 2>&1 || { error "uv not installed. Run ./setup.sh."; exit 1; }
HEADROOM_BIN="$(command -v headroom)"

# systemd --user needs a session bus; on some headless/sudo shells it is unset.
if ! systemctl --user show-environment >/dev/null 2>&1; then
  error "Can't reach 'systemctl --user'. If this is a headless/SSH session, run:"
  echo "    export XDG_RUNTIME_DIR=/run/user/\$(id -u)"
  echo "  then re-run ./install.sh. (On a desktop login this is already set.)"
  exit 1
fi

# ── 3. Install + start the proxy as a user service ────────────────────────────
info "Installing the Headroom proxy service (port ${PORT})…"
sed -e "s|__ENVFILE__|$SCRIPT_DIR/.env|" \
    -e "s|__HEADROOM_BIN__|$HEADROOM_BIN|" \
    -e "s|__PORT__|$PORT|" \
    "$SCRIPT_DIR/lib/headroom-proxy.service.in" > "$USER_UNIT_DIR/$UNIT_NAME"

systemctl --user daemon-reload
systemctl --user enable --now "$UNIT_NAME"

# Wait for the proxy to answer (any HTTP status means it's listening).
info "Waiting for the proxy to be ready…"
ready=""
for _ in $(seq 1 40); do
  code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}/stats" 2>/dev/null || echo 000)"
  if [[ "$code" != "000" ]]; then ready=1; break; fi
  sleep 0.5
done
if [[ -n "$ready" ]]; then
  success "Proxy service is up and enabled (starts on login)."
else
  warn "Proxy not responding yet. Check:  systemctl --user status $UNIT_NAME"
  warn "                          logs:  journalctl --user -u $UNIT_NAME -e"
fi

# ── 4. Route ALL Hermes traffic through the proxy (one global, canonical lever) ─
# Hermes reads OPENROUTER_BASE_URL from ~/.hermes/.env and applies it to EVERY
# process that uses the openrouter provider: gateway, all profiles, crons,
# kanban workers, delegated subagents, and the CLI. No config.yaml/profile edits,
# survives profile + model changes.  (Ref: Hermes env-vars reference.)
STATE_DIR="$RUNTIME_DIR/state"
mkdir -p "$STATE_DIR"

# 4a. If a previous version wired the default config to custom:headroom, undo it
#     so everything uniformly uses provider: openrouter + the global base-url.
if [[ -f "$RUNTIME_DIR/model_state.json" ]]; then
  info "Reverting a previous default-config wiring (migrating to the env lever)…"
  uv run --quiet --with ruamel.yaml -- python "$SCRIPT_DIR/lib/configure_hermes.py" \
    --config "$HOME/.hermes/config.yaml" --state-file "$RUNTIME_DIR/model_state.json" \
    --provider-name "$PROVIDER_NAME" restore || true
  uv run --quiet --with ruamel.yaml -- python "$SCRIPT_DIR/lib/configure_hermes.py" \
    --config "$HOME/.hermes/config.yaml" --provider-name "$PROVIDER_NAME" remove || true
fi

# 4b. Set OPENROUTER_BASE_URL in ~/.hermes/.env via an idempotent, removable block.
#     A pre-existing value (if any) is preserved below our block and restored on
#     uninstall (ours wins while installed because it is written last).
ENVFILE="$HOME/.hermes/.env"
info "Setting OPENROUTER_BASE_URL=${BASE_URL} in ${ENVFILE} …"
touch "$ENVFILE"; chmod 600 "$ENVFILE"
tmp="$(mktemp)"
awk '/# >>> hermes-headroom >>>/{skip=1} /# <<< hermes-headroom <<</{skip=0; next} !skip' \
  "$ENVFILE" > "$tmp" && mv "$tmp" "$ENVFILE"
chmod 600 "$ENVFILE"
{
  echo "# >>> hermes-headroom >>>"
  echo "# Route ALL Hermes OpenRouter traffic through the local Headroom proxy."
  echo "# Remove this block (or run ./uninstall.sh) to disable."
  echo "OPENROUTER_BASE_URL=${BASE_URL}"
  echo "# <<< hermes-headroom <<<"
} >> "$ENVFILE"
success "Global routing set — every Hermes process now flows through Headroom."

# 4c. (Optional) Add Headroom's MCP retrieve tool to the DEFAULT config only.
#     MCP servers have no global env lever, so this can't be made profile-wide
#     without per-profile edits (which we deliberately avoid). Default-only gives
#     the interactive CLI + default-profile sessions the retrieve tool.
if [[ "$ENABLE_MCP" != "0" ]]; then
  info "Adding the Headroom MCP retrieve tool to the default config only…"
  uv run --quiet --with ruamel.yaml -- python "$SCRIPT_DIR/lib/configure_hermes.py" \
    --config "$HOME/.hermes/config.yaml" --state-file "$STATE_DIR/mcp-default.json" \
    --provider-name "$PROVIDER_NAME" \
    mcp-add --proxy-url "$PROXY_BASE" --headroom-bin "$HEADROOM_BIN" || \
    warn "Could not add the MCP tool (compression still works without it)."
fi

# 4d. Register live OpenRouter prices for brand-new models (e.g. deepseek-v4-*)
#     into LiteLLM's local cost map so `headroom perf` shows $ savings. Paired
#     with LITELLM_LOCAL_MODEL_COST_MAP=true in .env. Safe + idempotent; re-run
#     after `uv tool upgrade headroom-ai`.
info "Registering current OpenRouter prices into LiteLLM (for perf \$ display)…"
python3 "$SCRIPT_DIR/lib/register_pricing.py" --from-logs \
  --slugs "deepseek/deepseek-v4-flash,deepseek/deepseek-v4-pro,${MODEL}" || \
  warn "Price registration skipped (compression unaffected; perf \$ may read 'unknown')."

# ── 5. Make the gateway wait for the proxy at boot (if a gateway unit exists) ──
GATEWAY_UNIT="$USER_UNIT_DIR/hermes-gateway.service"
if [[ -f "$GATEWAY_UNIT" ]]; then
  DROPIN_DIR="$USER_UNIT_DIR/hermes-gateway.service.d"
  mkdir -p "$DROPIN_DIR"
  cat > "$DROPIN_DIR/10-headroom.conf" <<EOF
[Unit]
After=$UNIT_NAME
Wants=$UNIT_NAME
EOF
  systemctl --user daemon-reload
  success "Linked the gateway to start after the proxy at boot."
  GATEWAY_PRESENT=1
else
  info "No user hermes-gateway service found — skipping boot-ordering link."
  GATEWAY_PRESENT=0
fi

# ── 6. Done — tell the user the one remaining manual step ──────────────────────
echo ""
success "Headroom is now a persistent layer in front of Hermes."
echo ""
info "Restart your gateway once so it picks up the new config:"
if [[ "$GATEWAY_PRESENT" == "1" ]]; then
  echo "    hermes gateway restart"
else
  echo "    (start your gateway however you normally do, e.g. 'hermes gateway start')"
fi
echo ""
info "From now on the gateway, Discord, crons (all profiles), and the 'hermes'"
info "CLI route through the proxy automatically. See savings:  headroom perf"
info "To fully revert everything:  ./uninstall.sh"
echo ""
