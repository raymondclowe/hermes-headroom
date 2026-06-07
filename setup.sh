#!/usr/bin/env bash
#
# hermes-headroom setup — one-time. Checks prerequisites, optionally installs
# Headroom, and creates your .env. Safe to re-run (won't overwrite an existing .env).
#
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "${SCRIPT_DIR}/lib/common.sh"

echo ""
echo -e "${BOLD}${BLUE}hermes-headroom setup${RESET}"
echo -e "Run Hermes Agent with Headroom token-compression in front (via OpenRouter)."
echo ""

# ── hermes ────────────────────────────────────────────────────────────────────
if command -v hermes >/dev/null 2>&1; then
	hermes_ver=$(hermes --version 2>/dev/null || echo '?')
	success "hermes found: ${hermes_ver}"
else
	warn "hermes-agent CLI not found. Install it, then re-run this script:"
	echo "    pip install hermes-agent      # or: github.com/NousResearch/hermes-agent"
fi

# ── uv (used to edit Hermes' YAML config safely) ──────────────────────────────
if command -v uv >/dev/null 2>&1; then
	uv_ver=$(uv --version 2>/dev/null || echo '?')
	success "uv found: ${uv_ver}"
else
	warn "uv not found (needed to edit Hermes' config safely). Install with:"
	echo "    curl -LsSf https://astral.sh/uv/install.sh | sh"
fi

# ── headroom ──────────────────────────────────────────────────────────────────
if command -v headroom >/dev/null 2>&1; then
	headroom_ver=$(headroom --version 2>/dev/null || echo '?')
	success "headroom found: ${headroom_ver}"
else
	warn "headroom not installed."
	echo "  The proxy + MCP need the 'headroom-ai' package."
	echo "  • [proxy,mcp] extras = small; covers compression AND the retrieve MCP tool."
	echo "  • [all] extra        = adds an ML text-compressor model — LARGE download."
	if command -v uv >/dev/null 2>&1; then
		# shellcheck disable=SC2310
		if confirm "Install headroom-ai[proxy,mcp] now with 'uv tool install'?"; then
			info "Installing… (this can take a minute)"
			uv tool install "headroom-ai[proxy,mcp]"
			info "Syncing local virtual environment..."
			uv sync
			success "headroom installed."
		else
			info 'Skipped. Install later with:  uv tool install "headroom-ai[proxy,mcp]"'
		fi
	else
		info 'Install uv first, then:  uv tool install "headroom-ai[proxy,mcp]"'
	fi
fi

# ── .env ──────────────────────────────────────────────────────────────────────
ENV_FILE="${SCRIPT_DIR}/.env"
if [[ -f ${ENV_FILE} ]]; then
	success ".env already exists — leaving it untouched."
else
	echo ""
	info "Let's create your .env (your key is stored here, never in a script)."
	cp "${SCRIPT_DIR}/.env.example" "${ENV_FILE}"
	chmod 600 "${ENV_FILE}"

	key="$(ask 'Paste your OpenRouter API key (openrouter.ai/keys):')"
	model="$(ask 'OpenRouter model id [Enter for deepseek/deepseek-v4-flash]:')"
	model="${model:-deepseek/deepseek-v4-flash}"
	ctx="$(ask 'Context window in tokens [Enter for 1000000]:')"
	ctx="${ctx:-1000000}"

	# | as the sed delimiter so model ids containing / and : are safe.
	sed -i "s|^OPENROUTER_API_KEY=.*|OPENROUTER_API_KEY=${key}|" "${ENV_FILE}"
	sed -i "s|^MODEL=.*|MODEL=${model}|" "${ENV_FILE}"
	sed -i "s|^CONTEXT_LENGTH=.*|CONTEXT_LENGTH=${ctx}|" "${ENV_FILE}"
	success "Wrote ${ENV_FILE}"
fi

echo ""
echo -e "${BOLD}Next step:${RESET}  ./install.sh"
echo "            (runs the proxy as a service + points Hermes at it, persistently)"
echo "            Then restart your gateway:  hermes gateway restart"
echo ""
info "Check savings any time:    headroom perf"
info "Fully revert everything:   ./uninstall.sh"
echo ""
