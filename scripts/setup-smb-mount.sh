#!/usr/bin/env bash
# Install the optional CIFS media mount used by the Audiosync library.
#
# This is the supported replacement for the one-off setup that used to live
# on the desktop.  It writes only the three system artifacts documented in
# the README: an fstab line, a root-owned credentials file, and the mountpoint.
# Re-running is safe; an existing fstab entry and credentials file are kept.
set -euo pipefail

FSTAB="${AUDIOSYNC_FSTAB:-/etc/fstab}"
CREDENTIALS="${AUDIOSYNC_SMB_CREDENTIALS:-/etc/samba/credentials-storage}"
MOUNTPOINT="${AUDIOSYNC_SMB_MOUNT:-/mnt/storage}"
SERVER="${AUDIOSYNC_SMB_SERVER:-}"
SHARE="${AUDIOSYNC_SMB_SHARE:-storage}"
SMB_USER="${AUDIOSYNC_SMB_USER:-}"
SMB_PASSWORD="${AUDIOSYNC_SMB_PASSWORD:-}"
OWNER="${AUDIOSYNC_SMB_OWNER:-${SUDO_USER:-$(id -un)}}"
DRY_RUN=0
ASSUME_YES=0
FORCE=0
PASSWORD_STDIN=0

usage() {
  cat <<'EOF'
Usage: sudo scripts/setup-smb-mount.sh --server HOST [options]

Install the CIFS media mount for a remote Samba share.  The script is
idempotent: an existing fstab entry is not duplicated and an existing
credentials file is kept unless --force is given.

Required:
  --server HOST          SMB server name or address (without //)

Options:
  --share NAME           Samba share name (default: storage)
  --mountpoint PATH      Local mountpoint (default: /mnt/storage)
  --credentials PATH     Root-only credentials file
                         (default: /etc/samba/credentials-storage)
  --smb-user USER        Samba username; defaults to --owner
  --owner USER           Local user whose uid/gid own the mounted files
                         (default: SUDO_USER, then the current user)
  --password-stdin       Read the SMB password from stdin instead of a prompt
  --force                Replace an existing credentials file (kept as .bak)
  --yes                   Do not ask before changing system files
  --dry-run               Print the plan; write and mount nothing
  --help                  Show this help

Environment equivalents: AUDIOSYNC_SMB_SERVER, AUDIOSYNC_SMB_SHARE,
AUDIOSYNC_SMB_MOUNT, AUDIOSYNC_SMB_CREDENTIALS, AUDIOSYNC_SMB_USER,
AUDIOSYNC_SMB_OWNER, AUDIOSYNC_FSTAB, AUDIOSYNC_SMB_PASSWORD.

The credentials file is written with mode 0600.  The fstab entry is
commented as audiosync-managed and backed up before editing.  The mountpoint
is never removed by this script; uninstall uses rmdir only when it is empty.
EOF
}

die() {
  printf 'audiosync-smb: %s\n' "$*" >&2
  exit 1
}

say() {
  printf '==> %s\n' "$*"
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --server)
      [ "$#" -ge 2 ] || die "--server needs a value"
      SERVER="$2"
      shift 2
      ;;
    --share)
      [ "$#" -ge 2 ] || die "--share needs a value"
      SHARE="$2"
      shift 2
      ;;
    --mountpoint)
      [ "$#" -ge 2 ] || die "--mountpoint needs a value"
      MOUNTPOINT="$2"
      shift 2
      ;;
    --credentials)
      [ "$#" -ge 2 ] || die "--credentials needs a value"
      CREDENTIALS="$2"
      shift 2
      ;;
    --smb-user)
      [ "$#" -ge 2 ] || die "--smb-user needs a value"
      SMB_USER="$2"
      shift 2
      ;;
    --owner)
      [ "$#" -ge 2 ] || die "--owner needs a value"
      OWNER="$2"
      shift 2
      ;;
    --password-stdin)
      PASSWORD_STDIN=1
      shift
      ;;
    --force)
      FORCE=1
      shift
      ;;
    --yes)
      ASSUME_YES=1
      shift
      ;;
    --dry-run)
      DRY_RUN=1
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

[ -n "$SERVER" ] || die "--server is required (use --help for examples)"
SERVER="${SERVER#//}"
SERVER="${SERVER%/}"
SHARE="${SHARE#/}"
SHARE="${SHARE%/}"
[ -n "$SERVER" ] || die "server cannot be empty"
[ -n "$SHARE" ] || die "share cannot be empty"
case "$SERVER$SHARE" in
  *[[:space:]]*) die "server and share must not contain whitespace" ;;
esac
case "$MOUNTPOINT" in
  /*) ;;
  *) die "mountpoint must be an absolute path" ;;
esac
[ "$MOUNTPOINT" != "/" ] || die "refusing to use / as the media mountpoint"
case "$CREDENTIALS" in
  /*) ;;
  *) die "credentials path must be absolute" ;;
esac
[ "$CREDENTIALS" != "/" ] || die "credentials path cannot be /"
[ -e "$CREDENTIALS" ] && [ -d "$CREDENTIALS" ] && die "credentials path is a directory: $CREDENTIALS"

if ! id "$OWNER" >/dev/null 2>&1; then
  die "local owner does not exist: $OWNER"
fi
OWNER_UID="$(id -u "$OWNER")"
OWNER_GID="$(id -g "$OWNER")"
[ -n "$SMB_USER" ] || SMB_USER="$OWNER"

# Never write a second fstab entry over a mountpoint that is already serving
# another filesystem (for example the local ext4 disk also called /mnt/storage).
if mountpoint -q "$MOUNTPOINT" 2>/dev/null; then
  current_fstype="$(findmnt -n -o FSTYPE -M "$MOUNTPOINT" 2>/dev/null || true)"
  current_source="$(findmnt -n -o SOURCE -M "$MOUNTPOINT" 2>/dev/null || true)"
  if [ "$current_fstype" != "cifs" ] || [ "$current_source" != "//$SERVER/$SHARE" ]; then
    if [ "$DRY_RUN" -eq 1 ]; then
      say "would refuse: $MOUNTPOINT is already mounted by $current_source ($current_fstype)"
    else
      die "$MOUNTPOINT is already mounted by $current_source ($current_fstype); choose another mountpoint or unmount it first"
    fi
  fi
fi

if [ "$DRY_RUN" -eq 0 ] && [ "$(id -u)" -ne 0 ]; then
  die "run this script with sudo so it can write /etc and mount the share"
fi
if [ "$DRY_RUN" -eq 0 ] && [ ! -f "$FSTAB" ]; then
  die "fstab not found: $FSTAB"
fi

# Escape spaces for fstab, which uses \040 rather than shell quoting.
escape_fstab() {
  local value="$1"
  value="${value//\\/\\\\}"
  value="${value// /\\040}"
  value="${value//$'\t'/\\011}"
  printf '%s' "$value"
}

MOUNT_ESCAPED="$(escape_fstab "$MOUNTPOINT")"
CREDENTIALS_ESCAPED="$(escape_fstab "$CREDENTIALS")"
ENTRY="//$SERVER/$SHARE $MOUNT_ESCAPED cifs credentials=$CREDENTIALS_ESCAPED,uid=$OWNER_UID,gid=$OWNER_GID,file_mode=0644,dir_mode=0755,iocharset=utf8,vers=3.1.1,nofail 0 0"
MARKER="# audiosync-managed-smb-mount"

fstab_has_entry() {
  [ -f "$FSTAB" ] || return 1
  awk -v target="$MOUNT_ESCAPED" '
    /^[[:space:]]*#/ { next }
    NF >= 4 && $2 == target && ($3 == "cifs" || $3 == "smb2" || $3 == "smb3") {
      found = 1
    }
    END { exit(found ? 0 : 1) }
  ' "$FSTAB"
}

credentials_exist=0
[ -f "$CREDENTIALS" ] && credentials_exist=1

if [ "$DRY_RUN" -eq 1 ]; then
  say "dry run: no files will be written"
  say "fstab: $FSTAB"
  if fstab_has_entry; then
    say "would keep the existing CIFS entry for $MOUNTPOINT"
  else
    say "would add:"
    printf '  %s\n  %s\n' "$MARKER" "$ENTRY"
  fi
  if [ "$credentials_exist" -eq 1 ] && [ "$FORCE" -eq 0 ]; then
    say "would keep existing credentials: $CREDENTIALS"
  else
    say "would write root-only credentials: $CREDENTIALS"
  fi
  say "would create mountpoint if needed: $MOUNTPOINT"
  if mountpoint -q "$MOUNTPOINT" 2>/dev/null; then
    say "already mounted: $MOUNTPOINT"
  else
    say "would mount //$SERVER/$SHARE at $MOUNTPOINT"
  fi
  exit 0
fi

if [ "$ASSUME_YES" -ne 1 ]; then
  printf 'Add the CIFS mount for //%s/%s at %s? [y/N] ' "$SERVER" "$SHARE" "$MOUNTPOINT"
  read -r answer
  case "$answer" in
    y|Y|yes|YES) ;;
    *) die "aborted by user" ;;
  esac
fi

say "ensuring mountpoint exists: $MOUNTPOINT"
if [ -e "$MOUNTPOINT" ] && [ ! -d "$MOUNTPOINT" ]; then
  die "mountpoint exists but is not a directory: $MOUNTPOINT"
fi
mkdir -p -- "$MOUNTPOINT"

if [ "$credentials_exist" -eq 1 ] && [ "$FORCE" -eq 0 ]; then
  say "keeping existing credentials: $CREDENTIALS"
else
  if [ -z "$SMB_USER" ]; then
    read -r -p "SMB username: " SMB_USER
    [ -n "$SMB_USER" ] || die "SMB username cannot be empty"
  fi
  if [ -z "$SMB_PASSWORD" ]; then
    if [ "$PASSWORD_STDIN" -eq 1 ]; then
      IFS= read -r SMB_PASSWORD || true
    else
      read -rsp "SMB password for $SMB_USER: " SMB_PASSWORD
      printf '\n'
    fi
  fi
  [ -n "$SMB_PASSWORD" ] || die "SMB password cannot be empty"
  install -d -m 0755 -- "$(dirname -- "$CREDENTIALS")"
  if [ "$credentials_exist" -eq 1 ]; then
    cp --preserve=mode -- "$CREDENTIALS" "${CREDENTIALS}.audiosync.bak"
    chmod 0600 "${CREDENTIALS}.audiosync.bak"
  fi
  umask 077
  temporary="$(mktemp "${CREDENTIALS}.XXXXXX")"
  trap 'rm -f -- "$temporary"' EXIT
  printf 'username=%s\npassword=%s\n' "$SMB_USER" "$SMB_PASSWORD" >"$temporary"
  chmod 0600 "$temporary"
  mv -- "$temporary" "$CREDENTIALS"
  trap - EXIT
  unset SMB_PASSWORD
  say "wrote root-only credentials: $CREDENTIALS"
fi

if fstab_has_entry; then
  say "fstab already has a CIFS entry for $MOUNTPOINT"
else
  cp --preserve=mode -- "$FSTAB" "${FSTAB}.audiosync.bak"
  say "backed up fstab to ${FSTAB}.audiosync.bak"
  {
    printf '\n%s\n%s\n' "$MARKER" "$ENTRY"
  } >>"$FSTAB"
  say "added CIFS entry for //$SERVER/$SHARE"
fi

if mountpoint -q "$MOUNTPOINT" 2>/dev/null; then
  say "already mounted: $MOUNTPOINT"
else
  say "mounting $MOUNTPOINT"
  if ! mount "$MOUNTPOINT"; then
    die "fstab is configured but the mount failed; check the share, credentials, and network"
  fi
  say "mounted //$SERVER/$SHARE at $MOUNTPOINT"
fi

say "done. Verify with: findmnt -T $MOUNTPOINT"
say "remove later with: $PWD/install.sh --uninstall (it will ask first)"
