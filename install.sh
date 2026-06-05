#!/usr/bin/env bash
#
# install — make Headroom a PERSISTENT, global layer in front of Hermes.
#
# It does two things, once:
#   1. installs + starts the Headroom proxy as a `systemd --user` service so it
#      is always running (just like your hermes-gateway service)
#   2. permanently points Hermes' config at that proxy (provider + model + the
#      retrieve MCP tool)
#
# After this, EVERYTHING Hermes does — the gateway, Discord, crons, and the
# plain `hermes` CLI — routes through Headroom automatically. No per-session
# launcher. Run ./uninstall.sh to fully revert.
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

# ── 4. Point Hermes' config at the proxy (persistently) ───────────────────────
info "Pointing Hermes at the proxy (model: ${MODEL}, context: ${CONTEXT_LENGTH})…"
APPLY_MCP_ARGS=()
if [[ "$ENABLE_MCP" != "0" ]]; then
  APPLY_MCP_ARGS=(--with-mcp --proxy-url "$PROXY_BASE" --headroom-bin "$HEADROOM_BIN")
fi
uv run --quiet --with ruamel.yaml -- \
  python "$SCRIPT_DIR/lib/configure_hermes.py" \
  --state-file "$STATE_FILE" --provider-name "$PROVIDER_NAME" \
  apply --base-url "$BASE_URL" --model "$MODEL" --context-length "$CONTEXT_LENGTH" \
  "${APPLY_MCP_ARGS[@]}"

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
info "From now on the gateway, Discord, crons, and the 'hermes' CLI all route"
info "through the proxy automatically. Check savings any time:  headroom stats"
info "To fully revert everything:  ./uninstall.sh"
echo ""
