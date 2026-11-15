"""Detect and remove the optional SMB mount outside ``$HOME``.

The application itself writes only under the user's config/state directories.
``scripts/setup-smb-mount.sh`` adds an fstab entry and a root-only credentials
file for the media share; ``install.sh --uninstall`` calls this module after
the user confirms removal.  Detection is deliberately narrow: only CIFS
entries for the configured mountpoint, only credentials under ``/etc/samba``,
and only an empty, unmounted mountpoint can be removed.  Media data is never
recursively deleted.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_FSTAB = Path("/etc/fstab")
DEFAULT_CREDENTIALS = Path("/etc/samba/credentials-storage")
DEFAULT_MOUNTPOINT = Path("/mnt/storage")
SMB_FSTYPES = frozenset({"cifs", "smb2", "smb3"})
SAMBA_DIR = Path("/etc/samba")


@dataclass(frozen=True)
class FstabEntry:
    """One CIFS line in fstab and the credential files it references."""

    line_number: int
    text: str
    credentials: tuple[Path, ...]
    marker_line: int | None = None


@dataclass(frozen=True)
class SmbArtifacts:
    """Everything uninstall may need to report or remove."""

    fstab: Path
    entries: tuple[FstabEntry, ...]
    credentials: tuple[Path, ...]
    mountpoint: Path
    mountpoint_exists: bool
    mountpoint_empty: bool
    mounted: bool

    @property
    def existing_credentials(self) -> tuple[Path, ...]:
        return tuple(path for path in self.credentials if path.is_file())

    @property
    def has_artifacts(self) -> bool:
        """True when there is something specific to audiosync to act on."""
        return bool(
            self.entries
            or self.existing_credentials
            or (self.mountpoint_exists and self.mountpoint_empty and not self.mounted)
        )

    @property
    def can_remove_mountpoint(self) -> bool:
        return self.mountpoint_exists and self.mountpoint_empty and not self.mounted


@dataclass(frozen=True)
class RemovalResult:
    removed_fstab: bool
    removed_credentials: tuple[Path, ...]
    removed_mountpoint: bool
    skipped: tuple[str, ...]
    fstab_backup: Path | None


def _unescape_fstab(value: str) -> str:
    return (
        value.replace("\\040", " ")
        .replace("\\011", "\t")
        .replace("\\134", "\\")
    )


def _is_samba_credentials(path: Path) -> bool:
    """Only ever consider root-owned Samba credential locations."""
    try:
        return path.is_absolute() and path.resolve().is_relative_to(SAMBA_DIR.resolve())
    except (OSError, RuntimeError):
        return False


def _entry_credentials(fields: list[str]) -> tuple[Path, ...]:
    if len(fields) < 4:
        return ()
    paths: list[Path] = []
    for option in fields[3].split(","):
        key, separator, value = option.partition("=")
        if key != "credentials" or not separator:
            continue
        path = Path(value)
        if _is_samba_credentials(path):
            paths.append(path)
            backup = path.with_name(path.name + ".audiosync.bak")
            if _is_samba_credentials(backup):
                paths.append(backup)
    return tuple(paths)


def detect(
    fstab: str | Path = DEFAULT_FSTAB,
    credentials: str | Path = DEFAULT_CREDENTIALS,
    mountpoint: str | Path = DEFAULT_MOUNTPOINT,
) -> SmbArtifacts:
    """Inspect the configured system paths without changing anything."""
    fstab_path = Path(fstab).expanduser()
    credentials_path = Path(credentials).expanduser()
    mount_path = Path(mountpoint).expanduser()
    entries: list[FstabEntry] = []
    credentials_paths: list[Path] = []
    try:
        lines = fstab_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line_number, raw in enumerate(lines, start=1):
        text = raw.strip()
        if not text or text.startswith("#"):
            continue
        fields = text.split()
        if len(fields) < 4:
            continue
        target = Path(_unescape_fstab(fields[1]))
        if target != mount_path:
            continue
        if fields[2].lower() not in SMB_FSTYPES:
            continue
        referenced = _entry_credentials(fields)
        marker_line = None
        if line_number > 1 and lines[line_number - 2].strip() == "# audiosync-managed-smb-mount":
            marker_line = line_number - 1
        entries.append(FstabEntry(line_number, raw, referenced, marker_line))
        credentials_paths.extend(referenced)

    managed_credentials = _is_samba_credentials(credentials_path)
    if managed_credentials:
        credential_candidates = (
            credentials_path,
            credentials_path.with_name(credentials_path.name + ".audiosync.bak"),
        )
        for candidate in credential_candidates:
            if candidate.is_file():
                credentials_paths.append(candidate)
        # Keep the default path visible in the report even when it is absent.
        if not any(path == credentials_path for path in credentials_paths):
            credentials_paths.append(credentials_path)
    deduped = tuple(dict.fromkeys(credentials_paths))

    exists = mount_path.is_dir() and not mount_path.is_symlink()
    empty = False
    if exists:
        try:
            empty = not any(mount_path.iterdir())
        except OSError:
            empty = False
    try:
        mounted = os.path.ismount(mount_path)
    except OSError:
        mounted = False
    return SmbArtifacts(
        fstab=fstab_path,
        entries=tuple(entries),
        credentials=deduped,
        mountpoint=mount_path,
        mountpoint_exists=exists,
        mountpoint_empty=empty,
        mounted=mounted,
    )


def _write_fstab(path: Path, text: str) -> None:
    """Replace fstab atomically while preserving its mode."""
    original_mode = stat.S_IMODE(path.stat().st_mode)
    fd, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(temporary, original_mode)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def remove(artifacts: SmbArtifacts) -> RemovalResult:
    """Remove only the detected artifacts, preserving non-empty mountpoints."""
    backup: Path | None = None
    removed_fstab = False
    if artifacts.entries:
        lines = artifacts.fstab.read_text(encoding="utf-8").splitlines(keepends=True)
        remove_lines = {entry.line_number for entry in artifacts.entries}
        remove_lines.update(
            entry.marker_line for entry in artifacts.entries if entry.marker_line is not None
        )
        kept = [
            line for number, line in enumerate(lines, start=1) if number not in remove_lines
        ]
        backup = artifacts.fstab.with_name(artifacts.fstab.name + ".audiosync.bak")
        shutil.copy2(artifacts.fstab, backup)
        _write_fstab(artifacts.fstab, "".join(kept))
        removed_fstab = True

    removed_credentials: list[Path] = []
    skipped: list[str] = []
    for path in artifacts.existing_credentials:
        try:
            path.unlink()
        except OSError as exc:
            skipped.append(f"{path}: {exc}")
        else:
            removed_credentials.append(path)

    removed_mountpoint = False
    if artifacts.mountpoint_exists:
        if artifacts.mounted:
            skipped.append(f"{artifacts.mountpoint}: still mounted; not removing")
        elif not artifacts.mountpoint_empty:
            skipped.append(
                f"{artifacts.mountpoint}: not empty; media preserved, not removing"
            )
        else:
            try:
                artifacts.mountpoint.rmdir()
            except OSError as exc:
                skipped.append(f"{artifacts.mountpoint}: {exc}")
            else:
                removed_mountpoint = True

    return RemovalResult(
        removed_fstab=removed_fstab,
        removed_credentials=tuple(removed_credentials),
        removed_mountpoint=removed_mountpoint,
        skipped=tuple(skipped),
        fstab_backup=backup,
    )


def _credentials_mode(path: Path) -> str:
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return "missing"
    return f"{mode:04o}"


def report_lines(artifacts: SmbArtifacts) -> list[str]:
    lines = ["SMB artifacts:"]
    if artifacts.entries:
        lines.append(
            f"  fstab: {artifacts.fstab} ({len(artifacts.entries)} CIFS entr"
            f"{'y' if len(artifacts.entries) == 1 else 'ies'} for {artifacts.mountpoint})"
        )
        for entry in artifacts.entries:
            lines.append(f"    {entry.line_number}: {entry.text.strip()}")
    else:
        lines.append(f"  fstab: no CIFS entry for {artifacts.mountpoint} in {artifacts.fstab}")
    for path in artifacts.credentials:
        lines.append(
            f"  credentials: {path} "
            f"({'present' if path.is_file() else 'missing'}, mode {_credentials_mode(path)})"
        )
    if not artifacts.mountpoint_exists:
        lines.append(f"  mountpoint: {artifacts.mountpoint} (missing)")
    elif artifacts.mounted:
        lines.append(f"  mountpoint: {artifacts.mountpoint} (mounted; not removable)")
    elif not artifacts.mountpoint_empty:
        lines.append(
            f"  mountpoint: {artifacts.mountpoint} "
            "(not empty; media preserved, never deleted)"
        )
    else:
        lines.append(f"  mountpoint: {artifacts.mountpoint} (empty; removable)")
    return lines


def _as_dict(artifacts: SmbArtifacts) -> dict[str, Any]:
    return {
        "fstab": str(artifacts.fstab),
        "entries": [
            {
                "line": entry.line_number,
                "text": entry.text,
                "credentials": [str(path) for path in entry.credentials],
                "marker_line": entry.marker_line,
            }
            for entry in artifacts.entries
        ],
        "credentials": [
            {
                "path": str(path),
                "present": path.is_file(),
                "mode": _credentials_mode(path),
            }
            for path in artifacts.credentials
        ],
        "mountpoint": {
            "path": str(artifacts.mountpoint),
            "exists": artifacts.mountpoint_exists,
            "empty": artifacts.mountpoint_empty,
            "mounted": artifacts.mounted,
        },
        "has_artifacts": artifacts.has_artifacts,
        "can_remove_mountpoint": artifacts.can_remove_mountpoint,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="audiosync.system_files",
        description="Report or remove the optional SMB mount owned by audiosync.",
    )
    parser.add_argument(
        "--fstab",
        default=os.environ.get("AUDIOSYNC_FSTAB", str(DEFAULT_FSTAB)),
        help=f"fstab path (default: {DEFAULT_FSTAB})",
    )
    parser.add_argument(
        "--credentials",
        default=os.environ.get("AUDIOSYNC_SMB_CREDENTIALS", str(DEFAULT_CREDENTIALS)),
        help=f"SMB credentials path (default: {DEFAULT_CREDENTIALS})",
    )
    parser.add_argument(
        "--mountpoint",
        default=os.environ.get("AUDIOSYNC_SMB_MOUNT", str(DEFAULT_MOUNTPOINT)),
        help=f"media mountpoint (default: {DEFAULT_MOUNTPOINT})",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="print nothing and exit 0 only when artifacts were detected",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    parser.add_argument(
        "--remove",
        action="store_true",
        help="remove detected fstab lines, credentials, and an empty mountpoint",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="with --remove, report what would happen without writing",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    artifacts = detect(args.fstab, args.credentials, args.mountpoint)
    if args.check:
        return 0 if artifacts.has_artifacts else 1
    if args.remove and args.dry_run:
        print("dry-run: no files would be changed")
        print("\n".join(report_lines(artifacts)))
        return 0
    if args.remove:
        if os.geteuid() != 0:
            print(
                "error: removing /etc files requires root; run this command with sudo",
                file=sys.stderr,
            )
            return 2
        result = remove(artifacts)
        if args.json:
            print(
                json.dumps(
                    {
                        "removed_fstab": result.removed_fstab,
                        "removed_credentials": [
                            str(path) for path in result.removed_credentials
                        ],
                        "removed_mountpoint": result.removed_mountpoint,
                        "skipped": list(result.skipped),
                        "fstab_backup": str(result.fstab_backup)
                        if result.fstab_backup
                        else None,
                    },
                    indent=2,
                )
            )
        else:
            print("SMB cleanup:")
            print(f"  fstab: {'removed' if result.removed_fstab else 'no matching entry'}")
            if result.fstab_backup:
                print(f"  fstab backup: {result.fstab_backup}")
            print(
                "  credentials: "
                + (
                    ", ".join(str(path) for path in result.removed_credentials)
                    if result.removed_credentials
                    else "none removed"
                )
            )
            print(
                f"  mountpoint: {'removed' if result.removed_mountpoint else 'kept'}"
            )
            for item in result.skipped:
                print(f"  skipped: {item}")
        return 0
    if args.json:
        print(json.dumps(_as_dict(artifacts), indent=2))
    else:
        print("\n".join(report_lines(artifacts)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
