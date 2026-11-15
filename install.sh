#!/usr/bin/env bash
# audiosync installer - writes user files under $HOME.  The optional SMB
# mount is installed by scripts/setup-smb-mount.sh and is only reported or
# removed by --uninstall after explicit confirmation.  Re-running is safe
# (idempotent), and --uninstall removes the files this installer added.
set -euo pipefail

PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/audiosync"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
PIPEWIRE_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/pipewire/pipewire.conf.d"
APP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
BIN_DIR="$HOME/.local/bin"
FSTAB="${AUDIOSYNC_FSTAB:-/etc/fstab}"
SMB_CREDENTIALS="${AUDIOSYNC_SMB_CREDENTIALS:-/etc/samba/credentials-storage}"
SMB_MOUNTPOINT="${AUDIOSYNC_SMB_MOUNT:-/mnt/storage}"
DRY_RUN=0
REMOVE_SMB=0
NO_SMB=0
ACTION=install
PURGE=0

die() { printf 'audiosync: %s\n' "$*" >&2; exit 1; }
say() { printf '\033[1m==>\033[0m %s\n' "$*"; }

usage() {
  cat <<'EOF'
Usage: install.sh [--cleanup-legacy] [--uninstall|--purge] [options]

Installs the Audiosync user services and launchers under $HOME.

Uninstall options:
  --uninstall       remove the user services, launchers, and desktop entry
  --purge           also remove the Audiosync config and state directories
  --dry-run         report what would be removed; change nothing
  --remove-smb      explicitly allow SMB system-file removal (asks by default)
  --no-smb          leave /etc/fstab, Samba credentials, and the mountpoint alone
  --help            show this help

The SMB mount itself is installed by scripts/setup-smb-mount.sh.  Uninstall
never removes a non-empty mountpoint and never recursively deletes media.
EOF
}

# Old test builds put a persistent PipeWire RTP receiver here.  It is not
# part of the current Snapcast design and survives PipeWire restarts, so
# migrate it out of the config directory instead of only unloading it at
# runtime.  Keep a recoverable backup.
cleanup_legacy_rtp() {
  local file backup
  for file in "$PIPEWIRE_DIR/98-rtp-receive.conf" "$PIPEWIRE_DIR/99-synced-stereo.conf"; do
    [ -f "$file" ] || continue
    if grep -qE 'rtp_synced_in|rtp_out_|libpipewire-module-rtp-(source|sink)' "$file"; then
      backup="${file}.audiosync-disabled"
      [ -e "$backup" ] && backup="${file}.audiosync-disabled.$(date +%s)"
      if [ "$DRY_RUN" -eq 1 ]; then
        say "dry-run: would disable legacy Audiosync RTP config: $file"
        continue
      fi
      mv "$file" "$backup"
      say "disabled legacy Audiosync RTP config: $file -> $backup"
    fi
  done
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --cleanup-legacy)
      ACTION=cleanup-legacy
      shift
      ;;
    --uninstall)
      ACTION=uninstall
      shift
      ;;
    --purge)
      ACTION=uninstall
      PURGE=1
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --remove-smb)
      REMOVE_SMB=1
      shift
      ;;
    --no-smb)
      NO_SMB=1
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      die "unknown argument: $1 (use --help)"
      ;;
  esac
done

[ "$(id -u)" -eq 0 ] && die "do not run as root; run user installs as yourself (SMB removal uses sudo only after confirmation)"
[ "$DRY_RUN" -eq 1 ] && [ "$ACTION" != "uninstall" ] && die "--dry-run is only supported with --uninstall"
[ "$REMOVE_SMB" -eq 1 ] && [ "$ACTION" != "uninstall" ] && die "--remove-smb is only supported with --uninstall"
[ "$NO_SMB" -eq 1 ] && [ "$ACTION" != "uninstall" ] && die "--no-smb is only supported with --uninstall"

if [ "$ACTION" = "cleanup-legacy" ]; then
  cleanup_legacy_rtp
  if [ "$DRY_RUN" -eq 1 ]; then
    say "dry-run: would restart pipewire and wireplumber"
  else
    systemctl --user restart pipewire wireplumber 2>/dev/null || true
  fi
  say "legacy RTP cleanup complete"
  exit 0
fi

# --------------------------------------------------------------------------
# uninstall
# --------------------------------------------------------------------------
smb_cleanup() {
  if [ "$NO_SMB" -eq 1 ]; then
    say "SMB system files left alone (--no-smb)"
    return
  fi
  # system_files backs up fstab to .audiosync.bak before removing matching lines.
  local smb_args=(--fstab "$FSTAB" --credentials "$SMB_CREDENTIALS" --mountpoint "$SMB_MOUNTPOINT")
  if ! PYTHONPATH="$PROJECT" python3 -m audiosync.system_files --check "${smb_args[@]}" >/dev/null 2>&1; then
    say "no SMB system artifacts found for $SMB_MOUNTPOINT"
    return
  fi
  say "detected optional SMB mount artifacts:"
  PYTHONPATH="$PROJECT" python3 -m audiosync.system_files "${smb_args[@]}" || true
  if [ "$DRY_RUN" -eq 1 ]; then
    say "dry-run: would offer to remove these artifacts; nothing changed"
    return
  fi
  local answer=no
  if [ "$REMOVE_SMB" -eq 1 ]; then
    answer=yes
  elif [ -t 0 ]; then
    printf 'Remove the fstab entry, credentials, and an empty mountpoint? [y/N] '
    read -r answer || answer=no
  else
    say "non-interactive: leaving SMB files in place; use --remove-smb or --dry-run"
    return
  fi
  case "$answer" in
    y|Y|yes|YES) ;;
    *)
      say "SMB files left in place"
      return
      ;;
  esac
  if command -v sudo >/dev/null 2>&1; then
    sudo env PYTHONPATH="$PROJECT" python3 -m audiosync.system_files --remove "${smb_args[@]}"
  else
    say "sudo is not available; run manually:"
    say "  sudo env PYTHONPATH=\"$PROJECT\" python3 -m audiosync.system_files --remove ${smb_args[*]}"
  fi
}

if [ "$ACTION" = "uninstall" ]; then
  if [ "$DRY_RUN" -eq 1 ]; then
    say "dry-run: would stop and remove user units and launchers"
    cleanup_legacy_rtp
    smb_cleanup
    if [ "$PURGE" -eq 1 ]; then
      say "dry-run: would remove $CONFIG_DIR and the Audiosync state directory"
    fi
    say "dry-run complete; nothing changed"
    exit 0
  fi
  say "stopping and removing user units"
  systemctl --user disable --now snapserver.service snapcast-capture.service snapclient.service 2>/dev/null || true
  systemctl --user disable --now audiosync-update.timer 2>/dev/null || true
  rm -f "$UNIT_DIR/snapserver.service" "$UNIT_DIR/snapcast-capture.service" "$UNIT_DIR/snapclient.service"
  rm -f "$UNIT_DIR/audiosync-update.service" "$UNIT_DIR/audiosync-update.timer"
  rm -f "$PIPEWIRE_DIR/10-audiosync-sink.conf"
  rm -f "$APP_DIR/audiosync.desktop" "$BIN_DIR/audiosync-gui" "$BIN_DIR/audiosync-snapclient"
  cleanup_legacy_rtp
  systemctl --user daemon-reload 2>/dev/null || true
  systemctl --user restart pipewire wireplumber 2>/dev/null || true
  smb_cleanup
  if [ "$PURGE" -eq 1 ]; then
    say "removing configuration and state"
    rm -rf "$CONFIG_DIR" "${XDG_STATE_HOME:-$HOME/.local/share}/audiosync"
  fi
  say "done. The snapserver/snapclient packages were left installed."
  exit 0
fi

# --------------------------------------------------------------------------
# preflight
# --------------------------------------------------------------------------
say "checking dependencies"
missing=()
command -v python3 >/dev/null || missing+=("python3")
command -v snapserver >/dev/null || missing+=("snapserver")
command -v snapclient >/dev/null || missing+=("snapclient")
python3 -c 'import PySide6' 2>/dev/null || missing+=("python3-pyside6")
python3 -c 'import zeroconf' 2>/dev/null || missing+=("python3-zeroconf")
command -v pactl >/dev/null || missing+=("pulseaudio-utils")
distro_family() {
  local id=""
  if [ -r /etc/os-release ]; then
    id="$(. /etc/os-release 2>/dev/null; printf '%s' "${ID:-}")"
  fi
  case "$id" in
    fedora|rhel|centos|rocky|almalinux) echo fedora ;;
    arch|manjaro|endeavouros|garuda) echo arch ;;
    debian|ubuntu|linuxmint|pop|elementary) echo debian ;;
    *) echo generic ;;
  esac
}

repo_hint() {
  case "$(distro_family)" in
    fedora)
      cat <<'EOF'
Snapcast is not in Fedora's main repos, and the older COPR builds fail on
Fedora 44 (they link against libFLAC.so.12, which F44 no longer ships).
This repo carries 0.34 builds for F44 - copy/paste:

  sudo tee /etc/yum.repos.d/audiosync-snapcast.repo >/dev/null <<'REPO'
[copr-cargsl-snapcast]
name=COPR cargsl/snapcast (Fedora 44 builds)
baseurl=https://download.copr.fedorainfracloud.org/results/cargsl/snapcast/fedora-44-$basearch/
type=rpm-md
gpgcheck=1
gpgkey=https://download.copr.fedorainfracloud.org/results/cargsl/snapcast/pubkey.gpg
enabled=1
REPO

  sudo dnf install snapserver snapclient python3-pyside6 python3-zeroconf
EOF
      ;;
    arch)
      cat <<'EOF'
Install the runtime from the Arch repositories, then Snapcast from the AUR
(no official package ships snapserver/snapclient):

  sudo pacman -S --needed pyside6 python-zeroconf pulseaudio-utils \
      bluez bluez-utils networkmanager
  yay -S snapcast

With PipeWire instead of PulseAudio, pw-cat replaces parec.
EOF
      ;;
    debian)
      cat <<'EOF'
Install the runtime from the Debian/Ubuntu repositories:

  sudo apt install python3-pyside6.qtwidgets python3-zeroconf \
      pulseaudio-utils snapserver snapclient

On a release without snapserver, build it from source; the app only needs the
snapserver and snapclient binaries on PATH.
EOF
      ;;
    *)
      cat <<'EOF'
Install these, then re-run this script:
  snapserver, snapclient, PySide6, zeroconf, pactl, and parec or pw-cat
EOF
      ;;
  esac
}

if [ ${#missing[@]} -gt 0 ]; then
  printf 'missing: %s\n\n' "${missing[*]}" >&2
  repo_hint >&2
  die "install the packages above, then re-run this script"
fi

SNAPSERVER_BIN="$(command -v snapserver)"
SNAPCLIENT_BIN="$(command -v snapclient)"
BINDIR="$(dirname "$SNAPSERVER_BIN")"

RECORDER_BIN=""
RECORDER_ARGS=""
if command -v parec >/dev/null; then
  RECORDER_BIN="$(command -v parec)"
  RECORDER_ARGS="--raw --format=s16le --rate=48000 --channels=2"
elif command -v pw-cat >/dev/null; then
  RECORDER_BIN="$(command -v pw-cat)"
  RECORDER_ARGS="--record --raw --format=s16 --rate=48000 --channels=2"
else
  die "need parec (pulseaudio-utils) or pw-cat (pipewire-utils) for the FIFO bridge"
fi

PLAYER=pipewire
if ! "$SNAPCLIENT_BIN" --help 2>&1 | grep -q pipewire; then
  PLAYER=alsa
fi

# --------------------------------------------------------------------------
# sink first, then let PipeWire settle before probing the hardware
# --------------------------------------------------------------------------
cleanup_legacy_rtp
say "installing the PipeWire sink"
mkdir -p "$PIPEWIRE_DIR"
cp "$PROJECT/pipewire/10-audiosync-sink.conf" "$PIPEWIRE_DIR/10-audiosync-sink.conf"
systemctl --user restart pipewire wireplumber 2>/dev/null || true
sleep 2

say "detecting hardware output"
HW_INFO=""
for _ in 1 2 3; do
  HW_INFO="$(python3 - "$PROJECT" <<'PY'
import subprocess, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from audiosync import outputs
try:
    sinks_text = subprocess.run(["pactl", "list", "sinks"], capture_output=True, text=True, timeout=5).stdout
except (OSError, subprocess.SubprocessError):
    sinks_text = ""
try:
    cards_text = Path("/proc/asound/cards").read_text(encoding="utf-8")
except OSError:
    cards_text = ""
choice = outputs.choose_output(outputs.parse_pactl_sinks(sinks_text), outputs.parse_asound_cards(cards_text))
print(choice.sink, choice.card, "" if choice.device is None else choice.device, sep="\t")
if choice.warning:
    print(f"warning: {choice.warning}", file=sys.stderr)
PY
)"
  [ -n "$(printf '%s' "$HW_INFO" | cut -f1)" ] && break
  sleep 1
done
IFS=$'\t' read -r HW_SINK CARD_ID CARD_DEVICE <<< "$HW_INFO"
[ -n "$HW_SINK" ] || die "no hardware sink found; is PipeWire running?"
say "hardware sink: $HW_SINK (card ${CARD_ID:-?}, device ${CARD_DEVICE:-?})"

# --------------------------------------------------------------------------
# units, wrapper, launcher
# --------------------------------------------------------------------------
say "installing user units (capture mode: fifo)"
mkdir -p "$UNIT_DIR"
install_unit() {
  local src="$1" dst="$2" recorder="$3"
  sed -e "s|@BINDIR@|$BINDIR|g" \
      -e "s|@RECORDER@|$recorder|g" \
      "$src" > "$dst"
}
install_unit "$PROJECT/systemd/snapserver.service"        "$UNIT_DIR/snapserver.service"        "$RECORDER_BIN"
install_unit "$PROJECT/systemd/snapcast-capture.service" "$UNIT_DIR/snapcast-capture.service" "$RECORDER_BIN $RECORDER_ARGS"
install_unit "$PROJECT/systemd/snapclient.service"       "$UNIT_DIR/snapclient.service"       "$RECORDER_BIN"

say "writing launchers"
mkdir -p "$BIN_DIR" "$APP_DIR"
cat > "$BIN_DIR/audiosync-gui" <<EOF
#!/usr/bin/env bash
export PYTHONPATH="$PROJECT\${PYTHONPATH:+:\$PYTHONPATH}"
exec python3 -m audiosync --gui "\$@"
EOF
chmod +x "$BIN_DIR/audiosync-gui"

# systemd would pass \${AUDIOSYNC_ARGS} through as one literal word; the
# wrapper sources client.env and lets the shell split the arguments.
cat > "$BIN_DIR/audiosync-snapclient" <<EOF
#!/bin/sh
# generated by audiosync - sources client.env, re-resolves the host name,
# then runs snapclient.  The name survives a DHCP change or a move between
# Wi-Fi and Ethernet; the stored address is only a fallback for when
# discovery is unavailable.
. "\$HOME/.config/audiosync/client.env"
host="\${AUDIOSYNC_HOST:-}"
if [ -n "\${AUDIOSYNC_HOST_NAME:-}" ]; then
  export PYTHONPATH="$PROJECT\${PYTHONPATH:+:\$PYTHONPATH}"
  resolved="\$(python3 -m audiosync --resolve "\$AUDIOSYNC_HOST_NAME" 2>/dev/null || true)"
  [ -n "\$resolved" ] && host="\$resolved"
fi
if [ -n "\$host" ]; then
  exec "$SNAPCLIENT_BIN" --host "\$host" \${AUDIOSYNC_ARGS:-}
fi
exec "$SNAPCLIENT_BIN" \${AUDIOSYNC_ARGS:-}
EOF
chmod +x "$BIN_DIR/audiosync-snapclient"

sed -e "s|@BINDIR@|$BIN_DIR|g" "$PROJECT/data/audiosync.desktop" > "$APP_DIR/audiosync.desktop"

say "recording detected hardware"
mkdir -p "$CONFIG_DIR"
AUDIOSYNC_HW_SINK="$HW_SINK" AUDIOSYNC_CARD="$CARD_ID" AUDIOSYNC_CARD_DEVICE="$CARD_DEVICE" \
AUDIOSYNC_PLAYER="$PLAYER" AUDIOSYNC_RECORDER="$(basename "$RECORDER_BIN")" python3 - "$PROJECT" <<'PY'
import os, sys
sys.path.insert(0, sys.argv[1])
from audiosync import config
cfg = config.Config.load()
cfg.hw_sink = os.environ["AUDIOSYNC_HW_SINK"]
cfg.hw_card = os.environ.get("AUDIOSYNC_CARD", "")
cfg.player = os.environ["AUDIOSYNC_PLAYER"]
cfg.recorder = os.environ["AUDIOSYNC_RECORDER"]
card_device = os.environ.get("AUDIOSYNC_CARD_DEVICE") or "0"
cfg.output_device = f"hw:{cfg.hw_card},{card_device}" if cfg.hw_card else ""
cfg.save()
PY

say "reloading systemd"
systemctl --user daemon-reload

if pactl list short sinks 2>/dev/null | awk '{print $2}' | grep -qx audiosync_sink; then
  say "audiosync_sink is available"
else
  printf 'warning: audiosync_sink did not appear yet; it should after the next login\n' >&2
fi

say "installed. Launch 'Audiosync' from the menu, or run: audiosync-gui"
say "optional Bluetooth PAN link: $PROJECT/scripts/setup-bt-pan.sh --help"
say "uninstall with: $PROJECT/install.sh --uninstall"
