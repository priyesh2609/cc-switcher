#!/usr/bin/env bash
# Remove cc-switcher. By default your saved logins are kept, so reinstalling picks them up again.
#
#   scripts/uninstall.sh [--purge [--yes]]
#
#   --purge   also delete the app data folder (~/.config/cc-switcher), the usage logs, and the saved
#             login of every registered account (the "cc-switcher:<id>" Keychain items)
#   --yes     with --purge: do not ask for confirmation
#
# Claude Code's own login in the Keychain is never touched, so `claude` keeps working.
# Written for the bash 3.2 that ships with macOS.
set -euo pipefail

PURGE=0
ASSUME_YES=0
LABEL="dev.cc-switcher.menubar"
APP_DIR="${CC_SWITCHER_DIR:-${HOME}/.config/cc-switcher}"
LOG_DIR="${HOME}/Library/Logs/cc-switcher"
PLIST="${HOME}/Library/LaunchAgents/${LABEL}.plist"

if [ -t 1 ]; then
  BOLD=$'\033[1m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'; RESET=$'\033[0m'
else
  BOLD=''; GREEN=''; YELLOW=''; RED=''; RESET=''
fi
STEP=0
step() { STEP=$((STEP + 1)); printf '\n%s[%s] %s%s\n' "${BOLD}" "${STEP}" "$1" "${RESET}"; }
ok()   { printf '    %s✓%s %s\n' "${GREEN}" "${RESET}" "$1"; }
warn() { printf '    %s!%s %s\n' "${YELLOW}" "${RESET}" "$1"; }
info() { printf '      %s\n' "$1"; }
die()  { printf '\n%s✗ %s%s\n' "${RED}" "$1" "${RESET}" >&2; exit 1; }

usage() {
  sed -n '2,10p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

for arg in "$@"; do
  case "${arg}" in
    --purge) PURGE=1 ;;
    --yes|-y) ASSUME_YES=1 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done

[ "$(uname -s)" = "Darwin" ] || die "cc-switcher only works on macOS."
export PATH="${HOME}/.local/bin:${HOME}/.cargo/bin:${PATH}"

find_ccs() {
  if command -v ccs >/dev/null 2>&1; then command -v ccs; return 0; fi
  if command -v uv >/dev/null 2>&1; then
    local bin
    bin="$(uv tool dir --bin 2>/dev/null || true)"
    if [ -n "${bin}" ] && [ -x "${bin}/ccs" ]; then echo "${bin}/ccs"; return 0; fi
  fi
  return 1
}

# Saved-login ids, read BEFORE anything is deleted. plutil reads JSON natively on macOS.
registered_ids() {
  local file="${APP_DIR}/accounts.json" i=0 id
  [ -f "${file}" ] || return 0
  while id="$(plutil -extract "accounts.${i}.id" raw -o - "${file}" 2>/dev/null)"; do
    printf '%s\n' "${id}"
    i=$((i + 1))
  done
  return 0
}

# --------------------------------------------------------------------------- 1. start-at-login
step "Stopping the menu bar app"
if CCS="$(find_ccs)"; then
  if "${CCS}" agent uninstall; then
    ok "LaunchAgent removed"
  else
    warn "ccs could not remove the LaunchAgent; removing it directly"
    launchctl bootout "gui/$(id -u)/${LABEL}" >/dev/null 2>&1 || true
    rm -f "${PLIST}"
  fi
else
  launchctl bootout "gui/$(id -u)/${LABEL}" >/dev/null 2>&1 || true
  rm -f "${PLIST}"
  ok "LaunchAgent removed (ccs was not found, so it was done directly)"
fi

# --------------------------------------------------------------------------- 2. read ids while accounts.json still exists
IDS=""
if [ "${PURGE}" = "1" ]; then
  IDS="$(registered_ids)"
fi

# --------------------------------------------------------------------------- 3. the tool
step "Removing the ccs command"
TOOLS=""
if command -v uv >/dev/null 2>&1; then TOOLS="$(uv tool list 2>/dev/null || true)"; fi
# here-string, not a pipe: with pipefail, `grep -q` closing the pipe early would fail the test
if grep -q '^cc-switcher ' <<< "${TOOLS}"; then
  uv tool uninstall cc-switcher
  ok "cc-switcher uninstalled"
else
  info "cc-switcher is not installed as a uv tool; nothing to remove"
fi

# --------------------------------------------------------------------------- 4. data
step "Saved logins and data"
if [ "${PURGE}" != "1" ]; then
  ok "kept ${APP_DIR} and the saved logins in your Keychain (use --purge to delete them)"
else
  case "${APP_DIR}" in
    ""|"/"|"${HOME}"|"${HOME}/") die "refusing to delete '${APP_DIR}'" ;;
  esac
  COUNT=0
  if [ -n "${IDS}" ]; then COUNT="$(printf '%s\n' "${IDS}" | wc -l | tr -d ' ')"; fi
  info "This deletes:"
  info "  - ${APP_DIR}"
  info "  - ${LOG_DIR}"
  info "  - ${COUNT} saved login(s) in your Keychain (service names starting with cc-switcher:)"
  info "Your Claude Code login and your Claude accounts are not affected."
  if [ "${ASSUME_YES}" != "1" ]; then
    [ -t 0 ] || die "--purge needs a terminal to confirm. Add --yes to run it unattended."
    printf '    Type "delete" to continue: '
    read -r reply
    [ "${reply}" = "delete" ] || die "cancelled; nothing was deleted."
  fi
  if [ -n "${IDS}" ]; then
    printf '%s\n' "${IDS}" | while IFS= read -r id; do
      [ -n "${id}" ] || continue
      if security delete-generic-password -s "cc-switcher:${id}" >/dev/null 2>&1; then
        ok "deleted the saved login for ${id}"
      else
        info "no saved login found for ${id}"
      fi
    done
  fi
  rm -rf "${APP_DIR}" "${LOG_DIR}"
  ok "deleted ${APP_DIR} and ${LOG_DIR}"
fi

printf '\n%sDone.%s\n' "${BOLD}" "${RESET}"
