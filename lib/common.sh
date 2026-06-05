#!/usr/bin/env bash
# Shared logging + prompt helpers for the hermes-headroom scripts.
# Sourced by setup.sh, hermes-headroom.sh, and unwrap.sh.
#
# IMPORTANT: prompts (ask/confirm) write to stderr, not stdout, so they stay
# visible on screen AND are not captured when used as  x="$(ask '...')".

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'
BOLD='\033[1m'; RESET='\033[0m'

info()    { echo -e "${BLUE}[INFO]${RESET}  $*"; }
success() { echo -e "${GREEN}[OK]${RESET}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${RESET}  $*"; }
error()   { echo -e "${RED}[ERROR]${RESET} $*" >&2; }

# confirm "Question?"  -> returns 0 for yes (default Yes), 1 for no
confirm() {
  printf '%s%s%s [Y/n] ' "$YELLOW" "$1" "$RESET" >&2
  read -r reply
  [[ ! "${reply:-Y}" =~ ^[Nn]$ ]]
}

# ask "Prompt"  -> prompt goes to stderr; the typed answer is echoed to stdout
ask() {
  printf '%s%s%s ' "$YELLOW" "$1" "$RESET" >&2
  read -r reply
  printf '%s\n' "$reply"
}
