#!/usr/bin/env bash
# audiosync Bluetooth PAN helper: one point-to-point link between two devices
# in the same room, bypassing the router.  PAN is the narrowest transport the
# app supports (raw PCM alone is 1.536 Mbit/s), so the app caps the codec at
# FLAC and only uses this path when "Bluetooth" is selected explicitly.
#
# This script never calls sudo.  NetworkManager may ask polkit for privileges.
set -euo pipefail

MODE="nap"
MAC=""
CONNECTION="audiosync-bt-pan"
DRY_RUN=0

die() { printf 'audiosync-bt-pan: %s\n' "$*" >&2; exit 1; }
say() { printf '\033[1m==>\033[0m %s\n' "$*"; }
run() {
  if [ "$DRY_RUN" -eq 1 ]; then
    printf 'dry-run:'
    printf ' %q' "$@"
    printf '\n'
    return 0
  fi
  "$@"
}

usage() {
  cat <<'EOF'
Usage: setup-bt-pan.sh --mode nap|panu [options]

Creates a Bluetooth PAN connection with NetworkManager so two devices can
exchange Snapcast traffic directly instead of through the router.

  --mode nap            Share this device's connection (use on one device)
  --mode panu           Join the other device's PAN (use on the others)
  --mac ADDRESS         Bluetooth address of the NAP device (panu only)
  --connection NAME     NetworkManager connection name
                        (default: audiosync-bt-pan)
  --dry-run             print the commands without changing anything
  --help                show this help

Pair the two devices once in the desktop's Bluetooth settings first.  This
script does not pair, does not call sudo, and does not change the firewall;
it prints the firewalld command when traffic would otherwise be blocked.

Bandwidth is the limit: a PAN cannot sustain raw 48 kHz stereo, so select
"Bluetooth" in the app and let it cap the codec at FLAC.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --mode) MODE="${2:-}"; shift 2 ;;
    --mac) MAC="${2:-}"; shift 2 ;;
    --connection) CONNECTION="${2:-}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --help|-h) usage; exit 0 ;;
    *) die "unknown argument: $1 (use --help)" ;;
  esac
done

[ "$(id -u)" -eq 0 ] && die "run as your user; NetworkManager asks for privileges if needed"
case "$MODE" in nap|panu) ;; *) die "--mode must be nap or panu" ;; esac
if [ "$MODE" = "panu" ]; then
  [ -n "$MAC" ] || die "--mode panu needs --mac AA:BB:CC:DD:EE:FF"
  if ! printf '%s' "$MAC" | grep -qiE '^([0-9a-f]{2}:){5}[0-9a-f]{2}$'; then
    die "not a Bluetooth address: $MAC"
  fi
fi
command -v nmcli >/dev/null || die "NetworkManager (nmcli) is required; install networkmanager and bluez"

if nmcli -t -f NAME connection show 2>/dev/null | grep -Fxq "$CONNECTION"; then
  say "updating existing connection $CONNECTION ($MODE)"
  if [ "$MODE" = "nap" ]; then
    run nmcli connection modify "$CONNECTION" bluetooth.type nap ipv4.method shared
  else
    run nmcli connection modify "$CONNECTION" bluetooth.type panu bluetooth.bdaddr "$MAC" ipv4.method auto
  fi
else
  say "creating $CONNECTION ($MODE)"
  if [ "$MODE" = "nap" ]; then
    run nmcli connection add type bluetooth ifname '*' con-name "$CONNECTION" bluetooth.type nap ipv4.method shared
  else
    run nmcli connection add type bluetooth ifname '*' con-name "$CONNECTION" bluetooth.type panu bluetooth.bdaddr "$MAC" ipv4.method auto
  fi
fi

run nmcli connection up "$CONNECTION" || say "activation failed; pair the devices and re-run"

IFACE=""
if [ "$DRY_RUN" -eq 0 ]; then
  IFACE="$(nmcli -t -f DEVICE,TYPE connection show --active 2>/dev/null | awk -F: '$2=="bluetooth"{print $1; exit}' || true)"
fi
say "PAN interface: ${IFACE:-not up yet (expected bnep0)}"
cat <<EOF

Next steps:
  1. Confirm an address:   ip -brief addr show ${IFACE:-bnep0}
  2. Open Audiosync and choose "Bluetooth" as the network path.
  3. If audio does not flow, allow Snapcast on the PAN interface:
       sudo firewall-cmd --permanent --zone=trusted --add-interface=${IFACE:-bnep0}
       sudo firewall-cmd --reload
     (or open ports 1704/tcp, 1705/tcp and 1780/tcp on that zone)

The app caps the codec at FLAC on this path: raw PCM needs 1.536 Mbit/s, which
a Bluetooth PAN cannot sustain reliably.
EOF
