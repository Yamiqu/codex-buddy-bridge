#!/usr/bin/env bash
# Codex Desktop Buddy Bridge installer.
#
# What this does:
#   1. Creates a venv and installs bleak.
#   2. Renders the launchd plist with absolute paths and loads it.
#   3. Enables [features] codex_hooks = true in ~/.codex/config.toml.
#   4. Writes ~/.codex/hooks.json with a PermissionRequest entry pointing
#      at hooks/permission_request.py. Existing hooks.json is backed up.
#
# Re-running this script is idempotent.

set -euo pipefail

BRIDGE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${BRIDGE_ROOT}/.venv"
VENV_PYTHON="${VENV_DIR}/bin/python3"
SOCKET_PATH="${CODEX_BUDDY_SOCKET:-/tmp/codex-buddy.sock}"
PLIST_LABEL="com.claudecodebuddy.codex-buddy"
PLIST_TARGET="${HOME}/Library/LaunchAgents/${PLIST_LABEL}.plist"
PLIST_TEMPLATE="${BRIDGE_ROOT}/launchd/${PLIST_LABEL}.plist.template"
LOG_DIR="${HOME}/Library/Logs"
LOG_PATH="${LOG_DIR}/codex-buddy.log"
CODEX_DIR="${HOME}/.codex"
CONFIG_TOML="${CODEX_DIR}/config.toml"
HOOKS_JSON="${CODEX_DIR}/hooks.json"

step() { printf '\n\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*" >&2; }

step "Bridge root: ${BRIDGE_ROOT}"

step "Creating Python venv and installing bleak"
if [[ ! -x "${VENV_PYTHON}" ]]; then
    python3 -m venv "${VENV_DIR}"
fi
"${VENV_PYTHON}" -m pip install --quiet --upgrade pip
"${VENV_PYTHON}" -m pip install --quiet -r "${BRIDGE_ROOT}/requirements.txt"

step "Rendering launchd plist"
mkdir -p "${LOG_DIR}" "$(dirname "${PLIST_TARGET}")"
sed \
    -e "s|__VENV_PYTHON__|${VENV_PYTHON}|g" \
    -e "s|__BRIDGE_ROOT__|${BRIDGE_ROOT}|g" \
    -e "s|__SOCKET_PATH__|${SOCKET_PATH}|g" \
    -e "s|__LOG_PATH__|${LOG_PATH}|g" \
    "${PLIST_TEMPLATE}" > "${PLIST_TARGET}"

if launchctl list | grep -q "${PLIST_LABEL}"; then
    launchctl unload "${PLIST_TARGET}" 2>/dev/null || true
fi
launchctl load -w "${PLIST_TARGET}"
step "launchd loaded ${PLIST_LABEL}; logs at ${LOG_PATH}"

step "Enabling codex_hooks feature flag in ${CONFIG_TOML}"
mkdir -p "${CODEX_DIR}"
touch "${CONFIG_TOML}"
if ! grep -q "^\[features\]" "${CONFIG_TOML}"; then
    {
        printf '\n[features]\n'
        printf 'codex_hooks = true\n'
    } >> "${CONFIG_TOML}"
elif ! awk '/^\[features\]/,/^\[/' "${CONFIG_TOML}" | grep -q "codex_hooks"; then
    # [features] section exists but no codex_hooks line — insert it after the header.
    python3 - "$CONFIG_TOML" <<'PY'
import pathlib, sys
path = pathlib.Path(sys.argv[1])
lines = path.read_text().splitlines()
out = []
inserted = False
for i, line in enumerate(lines):
    out.append(line)
    if not inserted and line.strip() == "[features]":
        out.append("codex_hooks = true")
        inserted = True
path.write_text("\n".join(out) + "\n")
PY
fi

step "Writing ${HOOKS_JSON}"
PERM_HOOK="${BRIDGE_ROOT}/hooks/permission_request.py"
chmod +x "${PERM_HOOK}"

if [[ -f "${HOOKS_JSON}" ]]; then
    cp "${HOOKS_JSON}" "${HOOKS_JSON}.bak.$(date +%s)"
    warn "Existing hooks.json was backed up next to it"
fi

cat > "${HOOKS_JSON}" <<EOF
{
  "hooks": {
    "PermissionRequest": [
      {
        "matcher": ".*",
        "hooks": [
          {
            "type": "command",
            "command": "${PERM_HOOK}",
            "timeout": 115,
            "statusMessage": "ClaudeCodeBuddy approval"
          }
        ]
      }
    ]
  }
}
EOF

CLI_TOOL="${BRIDGE_ROOT}/codex-buddy"
chmod +x "${CLI_TOOL}"

cat <<EOF

✓ Install complete.

Quick CLI:
    ${CLI_TOOL} status        agent loaded? + last log lines
    ${CLI_TOOL} on            load the launchd agent
    ${CLI_TOOL} off           unload it (releases BLE for Claude Hardware Buddy)
    ${CLI_TOOL} restart
    ${CLI_TOOL} log           tail -f the daemon log
    ${CLI_TOOL} foreground    run in this terminal with --debug
    ${CLI_TOOL} uninstall     remove plist + hooks.json

Tip: alias it in your shell rc, e.g.
    alias cbuddy='${CLI_TOOL}'

Next steps:

  1. Restart Codex Desktop and any open Codex CLI sessions so they pick up
     ${HOOKS_JSON}.

  2. On the first Codex approval after install, macOS will prompt for
     Bluetooth permission for the Python interpreter at:
       ${VENV_PYTHON}
     Approve it. Subsequent launchd-spawned runs keep the permission.

  3. The daemon does NOT hold BLE while idle — Claude Hardware Buddy works
     normally most of the time. BLE is acquired only when a Codex approval
     fires (3-5s connect overhead) and released immediately after.

  4. Test: in Codex, run a command that needs approval. The buddy switches
     to its approval screen; press A to allow or B to deny. If Claude
     happens to be paired at that moment, Codex falls back to its native
     prompt — no error, no hang.

EOF
