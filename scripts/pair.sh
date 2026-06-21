#!/usr/bin/env bash
set -euo pipefail

PREFIX="Claude-"
MAC=""
SCAN_SECONDS=8

usage() {
    cat <<'EOF'
Usage:
  scripts/pair.sh [--prefix Claude-] [--mac XX:XX:XX:XX:XX:XX] [--scan-seconds 8]

Examples:
  scripts/pair.sh
  scripts/pair.sh --prefix Codex-
  scripts/pair.sh --mac 70:04:1D:DC:52:55
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --prefix)
            PREFIX="${2:-}"
            shift 2
            ;;
        --mac)
            MAC="${2:-}"
            shift 2
            ;;
        --scan-seconds)
            SCAN_SECONDS="${2:-8}"
            shift 2
            ;;
        -h|--help|help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown arg: $1" >&2
            usage
            exit 1
            ;;
    esac
done

if ! command -v bluetoothctl >/dev/null 2>&1; then
    echo "error: bluetoothctl not found. Install BlueZ utilities first." >&2
    exit 1
fi

if [[ -z "${MAC}" ]]; then
    echo "Scanning ${SCAN_SECONDS}s for BLE devices with prefix '${PREFIX}'..."
    bluetoothctl --timeout "${SCAN_SECONDS}" scan on >/dev/null 2>&1 || true
    MAC="$(bluetoothctl devices | awk -v pfx="${PREFIX}" '$0 ~ pfx {print $2; exit}')"
fi

if [[ -z "${MAC}" ]]; then
    echo "error: no device found for prefix '${PREFIX}'." >&2
    echo "hint: keep the stick awake and advertising, then retry." >&2
    exit 1
fi

echo "Target device: ${MAC}"
echo "Pairing (if stick shows passkey, enter it on host when prompted)..."

bluetoothctl <<EOF
power on
agent on
default-agent
scan off
pair ${MAC}
trust ${MAC}
connect ${MAC}
info ${MAC}
quit
EOF

INFO="$(bluetoothctl info "${MAC}" 2>/dev/null || true)"
PAIRED="$(printf '%s\n' "${INFO}" | awk -F': ' '/Paired:/ {print $2; exit}')"
TRUSTED="$(printf '%s\n' "${INFO}" | awk -F': ' '/Trusted:/ {print $2; exit}')"
CONNECTED="$(printf '%s\n' "${INFO}" | awk -F': ' '/Connected:/ {print $2; exit}')"

echo
echo "Result:"
echo "  Paired:    ${PAIRED:-unknown}"
echo "  Trusted:   ${TRUSTED:-unknown}"
echo "  Connected: ${CONNECTED:-unknown}"

if [[ "${PAIRED}" != "yes" || "${TRUSTED}" != "yes" ]]; then
    echo "warning: pairing/trust did not fully complete. Retry while the stick is awake." >&2
    exit 2
fi

echo "Pairing complete."
