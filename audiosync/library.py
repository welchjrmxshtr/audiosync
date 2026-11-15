"""SMB-aware preload libraries.

A directory is synced once to each device under
``~/.cache/audiosync/library/<identity-hash>/``.  The same share keeps the
same identity whether it is read through the Samba server's local path or
through a CIFS/GVFS mount on another device.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable

from .preload import (
    SSH_OPTIONS,
    SSH_SETUP_TIMEOUT_S,
    Target,
    is_local_host,
    probe_targets,
)

LIBRARY_ROOT = ".cache/audiosync/library"
MARKER_NAME = ".audiosync-library.json"



class LibraryError(RuntimeError):
    """A source, path, or folder sync is invalid."""


class LibrarySyncCancelled(LibraryError):
    """The user cancelled a folder sync."""


class LibraryUnavailable(LibraryError):
    """A root is absent right now: an unmounted share or a detached disk.

    This is expected rather than a failure.  Network roots are mounted with
    ``nofail``, so they are simply missing until the server answers; callers
    skip that root and keep going instead of reporting an error.
    """


@dataclass(frozen=True)
class Library:
    """One registered media root and its share-relative identity."""

    path: str
    identity: str
    label: str

    def relative_path(self, media: str | Path) -> str:
        return relative_media_path(Path(self.path), media)

    def remote_relpath(self, media: str | Path) -> str:
        return library_relpath(self.identity, self.relative_path(media))

    @property
    def available(self) -> bool:
        """True when the root exists here; a nofail mount may be absent."""
        return Path(self.path).expanduser().is_dir()


@dataclass(frozen=True)
class LibraryStats:
    files: int
    bytes: int


def library_id(identity: str) -> str:
    import hashlib

    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]


def _unescape_mount(value: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), value)


def parse_mountinfo_line(line: str) -> dict[str, str] | None:
    """Return mountpoint/fstype/source from one ``/proc/self/mountinfo`` line."""
    if " - " not in line:
        return None
    left, right = line.split(" - ", 1)
    fields = left.split()
    tail = right.split()
    if len(fields) < 5 or len(tail) < 3:
        return None
    return {
        "mountpoint": _unescape_mount(fields[4]),
        "fstype": tail[0],
        "source": _unescape_mount(tail[1]),
    }


def parse_testparm(text: str) -> list[tuple[str, str]]:
    """Return ``(share, path)`` pairs from ``testparm -s`` output."""
    shares: list[tuple[str, str]] = []
    current: str | None = None
    current_path: str | None = None

    def flush() -> None:
        nonlocal current, current_path
        if current and current_path and current.lower() != "global":
            shares.append((current, current_path))
        current_path = None

    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            flush()
            current = line[1:-1].strip()
        elif current and line.startswith("path") and "=" in line:
            _, value = line.split("=", 1)
            current_path = value.strip()
    flush()
    return shares


def _share_from_source(source: str) -> str:
    text = source.rstrip("/")
    if text.startswith("//"):
        text = text[2:]
    return text.split("/")[-1]


def _identity_from_mount(path: Path, mountinfo: str | None = None) -> str | None:
    """Use the longest CIFS mount containing ``path``."""
    best: tuple[int, str] | None = None
    if mountinfo is None:
        try:
            mountinfo = Path("/proc/self/mountinfo").read_text(encoding="utf-8")
        except OSError:
            return None
    lines = mountinfo.splitlines()
    for line in lines:
        mount = parse_mountinfo_line(line)
        if not mount or mount["fstype"].lower() not in ("cifs", "smb3", "smb2"):
            continue
        mountpoint = Path(mount["mountpoint"]).resolve()
        try:
            relative = path.relative_to(mountpoint)
        except ValueError:
            continue
        share = _share_from_source(mount["source"])
        if not share:
            continue
        identity = "smb:" + share
        if relative.as_posix() != ".":
            identity += "/" + relative.as_posix()
        if best is None or len(str(mountpoint)) > best[0]:
            best = (len(str(mountpoint)), identity)
    return best[1] if best else None


def _identity_from_gvfs(path: Path) -> str | None:
    text = str(path)
    marker = "/gvfs/smb-share:"
    if marker not in text:
        return None
    rest = text.split(marker, 1)[1]
    params, sep, relative = rest.partition("/")
    match = re.search(r"(?:^|,)share=([^,/]+)", params)
    if not match:
        return None
    identity = "smb:" + match.group(1)
    if sep and relative:
        identity += "/" + relative
    return identity


def _identity_from_samba(path: Path, output: str | None = None) -> str | None:
    """On the server, map a local share path to its Samba share name."""
    if output is None:
        if shutil.which("testparm") is None:
            return None
        try:
            proc = subprocess.run(
                ["testparm", "-s"], capture_output=True, text=True, timeout=10
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            return None
        output = proc.stdout
    best: tuple[int, str] | None = None
    for share, share_path in parse_testparm(output):
        root = Path(share_path).expanduser().resolve()
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        identity = "smb:" + share
        if relative.as_posix() != ".":
            identity += "/" + relative.as_posix()
        if best is None or len(str(root)) > best[0]:
            best = (len(str(root)), identity)
    return best[1] if best else None


def label_for(identity: str) -> str:
    if identity.startswith("smb:"):
        rest = identity[4:]
        share, _, relative = rest.partition("/")
        return "SMB " + share + ((" / " + relative) if relative else "")
    if identity.startswith("local:"):
        return "Local " + identity[6:]
    return identity


def unavailable_message(root: Path, identity: str = "") -> str:
    """Explain an absent root without calling an offline mount a failure."""
    detail = f"missing directory: {root}" if identity else "missing directory"
    label = label_for(identity) if identity else str(root)
    return (
        f"{label} is not available right now ({detail}). "
        "A network share mounted with nofail stays absent until the server "
        "answers; reconnect it and retry."
    )


def resolve_source(path: str | Path, identity: str = "") -> Library:
    """Resolve a directory to a stable share-relative library identity.

    ``identity`` is only a hint for the message when the root is absent, so a
    caller that already knows the library can name it in the text.
    """
    root = Path(path).expanduser().resolve()
    if not root.is_dir():
        raise LibraryUnavailable(unavailable_message(root, identity))
    identity = (
        _identity_from_mount(root)
        or _identity_from_gvfs(root)
        or _identity_from_samba(root)
        or "local:" + str(root)
    )
    return Library(path=str(root), identity=identity, label=label_for(identity))


def safe_relative(relative: str) -> str:
    """Reject absolute and parent-traversing relative paths."""
    candidate = PurePosixPath(relative)
    parts = candidate.parts
    if not parts or candidate.is_absolute() or any(part in ("..", ".") for part in parts):
        raise LibraryError(f"unsafe relative path: {relative}")
    return candidate.as_posix()


def relative_media_path(root: Path, media: str | Path) -> str:
    root = root.expanduser().resolve()
    item = Path(media).expanduser().resolve()
    try:
        relative = item.relative_to(root)
    except ValueError as exc:
        raise LibraryError(f"{item} is outside {root}") from exc
    return safe_relative(relative.as_posix())


def library_root(identity: str) -> str:
    """Cache directory (home-relative) that holds one library."""
    return f"{LIBRARY_ROOT}/{library_id(identity)}"


def library_relpath(identity: str, relative: str) -> str:
    return f"{library_root(identity)}/{safe_relative(relative)}"


def find_library(media: str | Path, roots: Iterable[dict[str, str]]) -> Library | None:
    """Find the longest registered root containing ``media``."""
    item = Path(media).expanduser().resolve()
    best: tuple[int, Library] | None = None
    for record in roots or []:
        if isinstance(record, str):  # older configs stored plain paths
            record = {"path": record}
        if not isinstance(record, dict):
            continue
        raw = str(record.get("path") or "")
        if not raw:
            continue
        root = Path(raw).expanduser().resolve()
        try:
            item.relative_to(root)
        except ValueError:
            continue
        identity = str(record.get("identity") or "")
        if not identity:
            try:
                identity = resolve_source(root).identity
            except LibraryError:
                continue
        library = Library(path=str(root), identity=identity, label=label_for(identity))
        if best is None or len(str(root)) > best[0]:
            best = (len(str(root)), library)
    return best[1] if best else None


def _skip_file(path: Path) -> bool:
    name = path.name
    return (
        name.startswith("._")
        or name.startswith(".Trash-")
        or name in (".DS_Store", "Thumbs.db")
        or name.endswith(".part")
    )


def scan_directory(
    source: str | Path, identity: str = ""
) -> tuple[list[str], LibraryStats]:
    root = Path(source).expanduser().resolve()
    if not root.is_dir():
        raise LibraryUnavailable(unavailable_message(root, identity))
    if root == Path(root.anchor):
        raise LibraryError("refusing to sync a filesystem root")
    if not os.access(root, os.R_OK | os.X_OK):
        raise LibraryError(f"not readable: {root}")
    files: list[str] = []
    total = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [name for name in dirnames if not name.startswith(".Trash")]
        for name in filenames:
            path = Path(dirpath) / name
            if _skip_file(path):
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            files.append(path.relative_to(root).as_posix())
            total += size
    if not files:
        raise LibraryError(f"no files found under {root}")
    return sorted(files), LibraryStats(len(files), total)


def check_host_dependencies() -> None:
    """Folder sync runs rsync locally and drives it over ssh."""
    missing = [tool for tool in ("rsync", "ssh") if shutil.which(tool) is None]
    if missing:
        raise LibraryError(
            f"{' and '.join(missing)} not installed on this device; folder sync needs it"
        )


def rsync_argv(source: str | Path, target: Target, destination: str) -> list[str]:
    ssh_command = "ssh " + " ".join(shlex.quote(item) for item in SSH_OPTIONS)
    return [
        "rsync",
        "-rlt",
        "--partial",
        "--safe-links",
        "--delete",
        "--protect-args",
        "--exclude=.Trash-*",
        "--exclude=._*",
        "--exclude=*.part",
        "-e",
        ssh_command,
        str(Path(source).expanduser().resolve()) + os.sep,
        f"{target.host}:{destination}",
    ]


class FolderSyncJob:
    """Copy one library directory to every remote target."""

    def __init__(
        self, source: str | Path, targets: list[Target], identity: str = ""
    ) -> None:
        self.library = resolve_source(source, identity)
        self.targets = list(targets)
        self._cancelled = threading.Event()
        self.files: list[str] = []
        self.stats: LibraryStats | None = None
        #: Devices that could not be reached; the rest of the sync continues.
        self.failures: list[str] = []

    def cancel(self) -> None:
        self._cancelled.set()

    def scan(self) -> LibraryStats:
        self.files, self.stats = scan_directory(self.library.path, self.library.identity)
        return self.stats

    def _run_remote(self, target: Target, progress) -> None:
        if target.local or is_local_host(target.host):
            if progress:
                progress(f"{target.name}: local source, no copy needed")
            return
        assert target.host
        destination = library_root(self.library.identity)
        quoted_dest = shlex.quote(destination)
        try:
            mkdir = subprocess.run(
                [
                    "ssh", *SSH_OPTIONS, target.host,
                    f'mkdir -p "$HOME"/{quoted_dest} && '
                    "if ! command -v rsync >/dev/null 2>&1; then "
                    'echo "rsync is not installed on this device" >&2; exit 127; fi',
                ],
                capture_output=True,
                text=True,
                timeout=SSH_SETUP_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired as exc:
            raise LibraryError(
                f"SSH to {target.host} did not answer within {SSH_SETUP_TIMEOUT_S:.0f}s; "
                "keyless SSH must work from this device (a Tailscale SSH check or a "
                "name that only resolves off-LAN will hang)"
            ) from exc
        if mkdir.returncode != 0:
            detail = (mkdir.stderr or mkdir.stdout).strip()
            if "Tailscale SSH" in detail:
                detail += " (Tailscale SSH intercepted the connection; use the device's LAN address)"
            raise LibraryError(detail or f"{target.name}: mkdir failed")
        if progress:
            progress(f"{target.name}: syncing {self.stats.files if self.stats else 0} file(s)")
        argv = rsync_argv(self.library.path, target, destination)
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        while proc.poll() is None:
            if self._cancelled.is_set():
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except OSError:
                    pass
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                raise LibrarySyncCancelled("folder sync cancelled")
            time.sleep(0.2)
        if proc.returncode != 0:
            error = (proc.stderr.read() if proc.stderr else "").strip()
            raise LibraryError(error or f"{target.name}: rsync failed ({proc.returncode})")
        if progress:
            progress(f"{target.name}: library ready")

    def run(self, progress=None) -> int:
        check_host_dependencies()
        stats = self.scan()
        if progress:
            progress(f"{self.library.label}: {stats.files} file(s), {stats.bytes / 1048576:.1f} MiB")
        copied = 0
        self.failures = []
        reachable, failures = probe_targets(self.targets)
        # One unreachable device must not throw away the copies that already
        # landed, nor stall every other device.
        for target, error in failures:
            self.failures.append(f"{target.name}: {error}")
            if progress:
                progress(f"{target.name}: skipped ({error})")
        reachable_ids = {id(target) for target in reachable}
        probed_any = bool(reachable or failures)
        for target in self.targets:
            if self._cancelled.is_set():
                raise LibrarySyncCancelled("folder sync cancelled")
            remote = not (target.local or is_local_host(target.host))
            if remote and id(target) not in reachable_ids:
                continue
            try:
                self._run_remote(target, progress)
            except LibraryError as exc:
                # One unreachable device must not throw away the copies that
                # already landed; preload falls back to a single-file copy there.
                self.failures.append(f"{target.name}: {exc}")
                if progress:
                    progress(f"{target.name}: skipped ({exc})")
                continue
            if not (target.local or is_local_host(target.host)):
                try:
                    write_remote_marker(target, self.library, self.stats or LibraryStats(0, 0))
                except LibraryError as exc:
                    if progress:
                        progress(f"{target.name}: synced, marker not written ({exc})")
                copied += 1
        if self._cancelled.is_set():
            raise LibrarySyncCancelled("folder sync cancelled")
        if probed_any and not copied and self.failures:
            raise LibraryError("; ".join(self.failures))
        return copied


def write_remote_marker(target: Target, library: Library, stats: LibraryStats) -> None:
    """Write the diagnostic marker after a successful transfer."""
    if target.local or not target.host:
        return
    destination = f"{library_root(library.identity)}/{MARKER_NAME}"
    payload = json.dumps(
        {
            "identity": library.identity,
            "source": library.path,
            "files": stats.files,
            "bytes": stats.bytes,
            "synced_at": time.time(),
        },
        indent=2,
    )
    encoded = base64.b64encode(payload.encode()).decode()
    command = (
        f'printf %s {shlex.quote(encoded)} | base64 -d > "$HOME"/{shlex.quote(destination)}'
    )
    proc = subprocess.run(
        ["ssh", *SSH_OPTIONS, target.host, command], capture_output=True, text=True, timeout=20
    )
    if proc.returncode != 0:
        raise LibraryError((proc.stderr or proc.stdout).strip() or "could not write library marker")