#!/usr/bin/env bash
# cc-switcher installer for a Mac. Safe to run again: every step checks before it acts.
#
#   scripts/install.sh [--no-agent]
#
# Environment:
#   CC_SWITCHER_SOURCE       what to install: a path or a git URL (default: the repo this script is in)
#   CC_SWITCHER_MCP_PROFILE  an MCP profile file to apply after installing (optional)
#
# Written for the bash 3.2 that ships with macOS.
set -euo pipefail

ORIGINAL_PATH="${PATH}"  # what the user's shell had, before this script adds anything
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
SOURCE="${CC_SWITCHER_SOURCE:-${REPO_ROOT}}"
INSTALL_AGENT=1

CLAUDE_INSTALL_HINT='curl -fsSL https://claude.ai/install.sh | bash   (docs: https://docs.claude.com/en/docs/claude-code/setup)'

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
  cat <<'EOF'
Usage: scripts/install.sh [--no-agent]

Installs the `ccs` command and menu bar app for the current user.

  --no-agent   do not start the menu bar app at login
  -h, --help   show this help

Environment:
  CC_SWITCHER_SOURCE       path or git URL to install from (default: this repository)
  CC_SWITCHER_MCP_PROFILE  MCP profile file to apply after installing
EOF
}

for arg in "$@"; do
  case "${arg}" in
    --no-agent) INSTALL_AGENT=0 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done

# --------------------------------------------------------------------------- 1. macOS
step "Checking macOS"
[ "$(uname -s)" = "Darwin" ] || die "cc-switcher only works on macOS."
MACOS_VERSION="$(sw_vers -productVersion)"
MACOS_MAJOR="${MACOS_VERSION%%.*}"
if [ "${MACOS_MAJOR}" -lt 12 ] 2>/dev/null; then
  die "macOS ${MACOS_VERSION} is too old. cc-switcher needs macOS 12 or later."
fi
ok "macOS ${MACOS_VERSION}"

# --------------------------------------------------------------------------- 2. Claude Code
step "Checking Claude Code"
CLAUDE_BIN=""
if [ -n "${CC_SWITCHER_CLAUDE_PATH:-}" ]; then
  [ -x "${CC_SWITCHER_CLAUDE_PATH}" ] || die "CC_SWITCHER_CLAUDE_PATH is not executable."
  CLAUDE_BIN="${CC_SWITCHER_CLAUDE_PATH}"
elif command -v claude >/dev/null 2>&1; then
  CLAUDE_BIN="$(command -v claude)"
else
  for candidate in "${HOME}/.local/bin/claude" /opt/homebrew/bin/claude /usr/local/bin/claude; do
    if [ -x "${candidate}" ]; then CLAUDE_BIN="${candidate}"; break; fi
  done
fi
if [ -n "${CLAUDE_BIN}" ]; then
  export CC_SWITCHER_CLAUDE_PATH="${CLAUDE_BIN}"
  ok "found ${CLAUDE_BIN}"
else
  warn "Claude Code is not installed. cc-switcher switches its logins, so install it first:"
  info "${CLAUDE_INSTALL_HINT}"
  info "Continuing: cc-switcher itself can be installed now."
fi

# --------------------------------------------------------------------------- 3. uv
step "Checking uv (the Python tool installer)"
export PATH="${HOME}/.local/bin:${HOME}/.cargo/bin:${PATH}"
if command -v uv >/dev/null 2>&1; then
  ok "uv $(uv --version | awk '{print $2}') at $(command -v uv)"
else
  if command -v brew >/dev/null 2>&1; then
    info "Installing uv with Homebrew"
    brew install uv
  else
    info "Installing uv with the official installer (https://astral.sh/uv)"
    curl -LsSf https://astral.sh/uv/install.sh | sh
  fi
  hash -r
  command -v uv >/dev/null 2>&1 || die "uv was installed but is not on PATH. Open a new Terminal window and run this script again."
  ok "installed uv $(uv --version | awk '{print $2}')"
fi

# --------------------------------------------------------------------------- 4. cc-switcher
step "Installing cc-switcher"
TARGET="${SOURCE}"
case "${SOURCE}" in
  git+*) ;;
  http://*|https://*|ssh://*|git@*) TARGET="git+${SOURCE}" ;;
  *)
    [ -d "${SOURCE}" ] || die "CC_SWITCHER_SOURCE '${SOURCE}' is not a folder or git URL."
    TARGET="$(cd "${SOURCE}" && pwd)"
    ;;
esac
info "source: ${TARGET}"
# --reinstall makes uv rebuild from the source instead of reusing a cached build of the same version,
# so a `git pull` followed by this script always installs the code you just pulled.
uv tool install --python 3.12 --force --reinstall "${TARGET}"
BIN_DIR="$(uv tool dir --bin)"
export PATH="${BIN_DIR}:${PATH}"  # lets `ccs agent install` find the ccs executable it should launch
CCS="${BIN_DIR}/ccs"
[ -x "${CCS}" ] || die "Expected ${CCS} after installing, but it is missing."
ok "installed $("${CCS}" --version)"

# --------------------------------------------------------------------------- 5. PATH
step "Checking that ${BIN_DIR} is on your PATH"
case ":${ORIGINAL_PATH}:" in
  *":${BIN_DIR}:"*) ok "ccs is on PATH" ;;
  *)
    warn "ccs is installed but not on your PATH yet. Add this line to ~/.zshrc, then open a new Terminal window:"
    info "export PATH=\"${BIN_DIR}:\$PATH\""
    ;;
esac

# --------------------------------------------------------------------------- 6. import the signed-in account
step "Saving the account you are signed in to"
if [ -z "${CLAUDE_BIN}" ]; then
  warn "skipped: Claude Code is not installed. After installing it, sign in with \`claude\` and run \`ccs import\`."
elif "${CLAUDE_BIN}" auth status >/dev/null 2>&1; then
  "${CCS}" import
  ok "account saved (run \`ccs add --label work\` for each additional account)"
else
  warn "not signed in to Claude Code. Run \`claude\`, sign in, then run \`ccs import\`."
fi

# --------------------------------------------------------------------------- 7. MCP profile
step "MCP servers"
if [ -n "${CC_SWITCHER_MCP_PROFILE:-}" ]; then
  if "${CCS}" mcp apply "${CC_SWITCHER_MCP_PROFILE}"; then
    ok "applied ${CC_SWITCHER_MCP_PROFILE}"
  else
    warn "could not apply ${CC_SWITCHER_MCP_PROFILE}. Fix the problem above, then run \`ccs mcp apply ${CC_SWITCHER_MCP_PROFILE}\`."
  fi
else
  info "no profile given (set CC_SWITCHER_MCP_PROFILE=/path/to/mcp-profile.json to apply one). See docs/MCP.md."
fi

# --------------------------------------------------------------------------- 8. start at login
step "Menu bar app"
if [ "${INSTALL_AGENT}" = "1" ]; then
  if "${CCS}" agent install; then
    ok "the menu bar app starts at login and is running now"
  else
    warn "could not install the LaunchAgent. Run \`ccs menubar\` to start the app by hand."
  fi
else
  info "skipped (--no-agent). Run \`ccs menubar\` to start it, or \`ccs agent install\` to start it at login."
fi

# --------------------------------------------------------------------------- 9. doctor
step "Running ccs doctor"
if ! "${CCS}" doctor; then
  warn "doctor reported problems (see the lines marked with ✗). Fix them, then run \`ccs doctor\` again."
fi

printf '\n%sDone.%s Next: `ccs list`, and `ccs add --label <name>` for each other account. Guide: docs/INSTALL.md\n' "${BOLD}" "${RESET}"
