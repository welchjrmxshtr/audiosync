"""Local-file preload and synchronised start.

Snapcast streams; this mode does not.  A selected file is copied to every
device, its SHA-256 is verified there, and each device prerolls the file in
GStreamer.  All pipelines then change to PLAYING at one shared wall-clock
instant, so Wi-Fi cannot cause dropouts during the media itself.

Browser/system audio is untouched: only the chosen file is played locally.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from . import outputs, safety, transport

SSH_OPTIONS = [
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=8",
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
]
#: Stop must not let one unreachable host hold every window out of idle.
STOP_SSH_OPTIONS = [
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=4",
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
]
CACHE_ROOT = ".cache/audiosync/preload"
VIDEO_SUFFIXES = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".ts", ".mpg", ".mpeg"}
PLAYER_DEADLINE_S = 30
PLAYER_MARKER = "audiosync-preload"

#: ``mkdir`` on a reachable device is instant; a hung SSH must not stall a run.
SSH_SETUP_TIMEOUT_S = 15.0
#: Reachability is probed for every device at once, so this is paid once.
SSH_PROBE_TIMEOUT_S = 10.0


class PreloadError(RuntimeError):
    """Copy, checksum, preroll, or launch failed."""


class PreloadCancelled(PreloadError):
    """The host stopped the job before playback started."""


@dataclass(frozen=True)
class Target:
    """One playback device.  ``host=None`` means this machine."""

    name: str
    host: str | None = None

    @property
    def local(self) -> bool:
        return self.host is None


CAPTURE_UNITS = ("snapserver.service", "snapcast-capture.service")


# --------------------------------------------------------------------------
# which targets can actually be reached
# --------------------------------------------------------------------------
def local_host_names() -> set[str]:
    """Names and addresses that mean "this machine" for SSH targets."""
    names = {"localhost", "127.0.0.1", "::1"}
    for name in (socket.gethostname(), socket.getfqdn()):
        text = (name or "").strip().rstrip(".").lower()
        if text:
            names.add(text)
            names.add(text.split(".")[0])
    try:
        _host, aliases, addresses = socket.gethostbyname_ex(socket.gethostname())
    except OSError:
        aliases, addresses = [], []
    names.update(str(item).strip().rstrip(".").lower() for item in (*aliases, *addresses))
    return {name for name in names if name}


def is_local_host(host: str | None) -> bool:
    """True when an SSH target would just be this machine."""
    text = (host or "").strip().rstrip(".").lower()
    return bool(text) and text in local_host_names()


def is_local_target(target: Target) -> bool:
    """A device that plays from this machine, never over SSH."""
    return target.local or is_local_host(target.host)


def peer_targets(peers, policy: str = "auto") -> list[Target]:
    """Discovered peers as playback targets.

    The peer's best reachable address is the SSH host, chosen with the same
    policy the audio path uses, so transfers keep working when a static
    ``ssh_config`` HostName goes stale after a DHCP change.  The peer's name
    stays the label shown by the app.
    """
    targets: list[Target] = []
    seen: set[str] = set()
    local = [endpoint.address for endpoint in transport.local_endpoints()]
    for peer in peers:
        name = str(getattr(peer, "name", "") or "").strip()
        address = str(getattr(peer, "address", "") or "").strip()
        endpoints = list(getattr(peer, "endpoints", []) or [])
        chosen = transport.choose_endpoint(endpoints, policy, local) if endpoints else None
        if chosen is not None and policy == "btpan":
            address = chosen.address
        elif chosen is not None and chosen.kind != transport.BTPAN:
            address = chosen.address
        display = name or address
        host = address or name
        if not display or not host or display in seen:
            continue
        if is_local_host(host) or is_local_host(display):
            continue
        seen.add(display)
        targets.append(Target(display, host))
    return targets


def probe_remote(target: Target) -> str | None:
    """Return a reason when a device is not reachable over SSH, else ``None``."""
    if is_local_target(target):
        return None
    assert target.host
    try:
        proc = subprocess.run(
            ["ssh", *SSH_OPTIONS, target.host, "true"],
            capture_output=True,
            text=True,
            timeout=SSH_PROBE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return (
            f"SSH to {target.host} did not answer within {SSH_PROBE_TIMEOUT_S:.0f}s; "
            "keyless SSH must work from this device (a Tailscale SSH check or a "
            "name that only resolves off-LAN will hang)"
        )
    except OSError as exc:
        return f"SSH to {target.host} failed: {exc}"
    if proc.returncode == 0:
        return None
    detail = (proc.stderr or proc.stdout).strip() or f"SSH exited {proc.returncode}"
    if "Tailscale SSH" in detail:
        detail += " (Tailscale SSH intercepted the connection; use the device's LAN address)"
    return detail


def probe_targets(
    targets: list[Target],
) -> tuple[list[Target], list[tuple[Target, str]]]:
    """Probe every remote device at once.

    Returns the reachable targets and ``(target, reason)`` pairs for the rest,
    so one unreachable device costs one shared timeout, not one per device.
    """
    remotes = [t for t in targets if not is_local_target(t)]
    if not remotes:
        return [], []
    workers = max(1, min(8, len(remotes)))
    reachable: list[Target] = []
    failures: list[tuple[Target, str]] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for target, error in pool.map(lambda t: (t, probe_remote(t)), remotes):
            if error:
                failures.append((target, error))
            else:
                reachable.append(target)
    return reachable, failures


def _target_control(target: Target, command: list[str], timeout: float = 8.0):
    """Run one local or SSH control command for a preload target."""
    if is_local_target(target):
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    assert target.host
    return subprocess.run(
        ["ssh", *SSH_OPTIONS, target.host, REMOTE_ENV + "exec " + shlex.join(command)],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def playback_sink(target: Target) -> str:
    """Return the target's current default output, if it can be queried."""
    try:
        proc = _target_control(target, ["pactl", "get-default-sink"])
    except (OSError, subprocess.SubprocessError) as exc:
        raise PreloadError(f"{target.name}: could not query the output sink: {exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip() or f"pactl exited {proc.returncode}"
        raise PreloadError(f"{target.name}: could not query the output sink: {detail}")
    sink = (proc.stdout or "").strip().splitlines()[0].strip() if (proc.stdout or "").strip() else ""
    if not sink:
        raise PreloadError(f"{target.name}: no output sink is configured")
    return sink


def active_capture_units(target: Target) -> list[str]:
    """Return Snapcast capture units currently active on a target.

    This is deliberately a preflight for preload, which bypasses Snapcast.
    A player writing to a host's virtual sink while capture is active would
    put the same media back into the stream.
    """
    command = ["systemctl", "--user", "is-active", *CAPTURE_UNITS]
    try:
        proc = _target_control(target, command)
    except (OSError, subprocess.SubprocessError) as exc:
        raise PreloadError(f"{target.name}: could not verify Snapcast capture state: {exc}") from exc
    if proc.returncode not in (0, 3):
        detail = (proc.stderr or proc.stdout).strip() or f"systemctl exited {proc.returncode}"
        raise PreloadError(f"{target.name}: could not verify Snapcast capture state: {detail}")
    lines = [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]
    if len(lines) < len(CAPTURE_UNITS):
        raise PreloadError(
            f"{target.name}: could not verify all Snapcast capture units; refusing preload"
        )
    known_states = {"active", "activating", "reloading", "deactivating", "inactive", "failed"}
    if any(state not in known_states for state in lines[: len(CAPTURE_UNITS)]):
        raise PreloadError(
            f"{target.name}: Snapcast capture unit state is unknown; refusing preload"
        )
    return [
        unit
        for unit, state in zip(CAPTURE_UNITS, lines)
        if state in ("active", "activating", "reloading", "deactivating")
    ]


def current_hardware_sinks(target: Target) -> set[str]:
    """Current non-virtual sinks reported by the target's PipeWire."""
    try:
        proc = _target_control(target, ["pactl", "list", "sinks"])
    except (OSError, subprocess.SubprocessError) as exc:
        raise PreloadError(f"{target.name}: could not verify output sinks: {exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip() or f"pactl exited {proc.returncode}"
        raise PreloadError(f"{target.name}: could not verify output sinks: {detail}")
    return outputs.hardware_sink_names(proc.stdout or "")


def preflight_target(target: Target) -> tuple[str, str]:
    """Return ``(safe sink, error)`` for one preload target.

    Unknown sink or unit state fails closed.  The caller must not launch a
    player for a target whose sink is virtual or whose capture path is live.
    """
    try:
        sink = playback_sink(target)
        sink = safety.safe_client_sink(sink, allow_alsa_device=False)
        if sink not in current_hardware_sinks(target):
            raise PreloadError(
                f"{target.name}: {sink!r} is not a current hardware output; refusing preload"
            )
        active = active_capture_units(target)
    except (PreloadError, safety.RoutingError) as exc:
        return "", str(exc)
    if active:
        return "", (
            f"{target.name}: {', '.join(active)} is active; stop Snapcast hosting "
            "before preload (it bypasses the capture path)"
        )
    return sink, ""


# --------------------------------------------------------------------------
# paths and verification
# --------------------------------------------------------------------------
def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def safe_name(name: str) -> str:
    """A filename that is safe in a remote shell and on every filesystem."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name).name).strip("._")
    return cleaned or "media"


def cache_relpath(digest: str, name: str) -> str:
    """Path relative to the remote user's home."""
    return f"{CACHE_ROOT}/{digest}/{safe_name(name)}"


def _run(cmd: list[str], timeout: float = 1800.0) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PreloadError(f"{cmd[0]} failed: {exc}") from exc
    if proc.returncode != 0:
        message = (proc.stderr or proc.stdout).strip()
        raise PreloadError(message or f"{cmd[0]} exited {proc.returncode}")
    return proc


def remote_relpath(media: str | Path, digest: str, library=None) -> str:
    """Home-relative path a device should hold for ``media``.

    Inside a synced library the share-relative layout is used, so the folder
    sync's copy is reused.  Other files keep the per-digest cache.
    """
    if library is not None:
        return library.remote_relpath(media)
    return cache_relpath(digest, Path(media).name)


def remote_sha256(target: Target, rel: str) -> str | None:
    """Checksum of ``rel`` on a device, or ``None`` when it is not there."""
    if is_local_target(target):
        try:
            return sha256_file(Path(rel).expanduser())
        except OSError:
            return None
    assert target.host
    try:
        proc = subprocess.run(
            ["ssh", *SSH_OPTIONS, target.host, f'sha256sum "$HOME/{rel}"'],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    fields = proc.stdout.split()
    return fields[0].lower() if fields else None


def copy_to(
    target: Target,
    media: str | Path,
    digest: str,
    progress=None,
    rel: str | None = None,
) -> str:
    """Copy ``media`` to a target and verify the complete file.

    ``rel`` overrides the home-relative destination, which lets a synced
    library keep its share-relative layout.  Returns the path used on that
    device (absolute locally, home-relative remotely).
    """
    local_path = str(Path(media).expanduser().resolve())
    if is_local_target(target):
        return local_path

    assert target.host
    rel = rel or cache_relpath(digest, Path(media).name)
    remote_dir = str(Path(rel).parent)
    if progress:
        progress(f"{target.name}: creating cache directory")
    _run(
        ["ssh", *SSH_OPTIONS, target.host, f'mkdir -p "$HOME/{remote_dir}"'],
        timeout=SSH_SETUP_TIMEOUT_S,
    )

    if progress:
        progress(f"{target.name}: copying {Path(media).name}")
    _run(["scp", "-q", *SSH_OPTIONS, local_path, f"{target.host}:{rel}"])

    if progress:
        progress(f"{target.name}: verifying checksum")
    out = _run(["ssh", *SSH_OPTIONS, target.host, f'sha256sum "$HOME/{rel}"']).stdout
    actual = out.split()[0] if out.split() else ""
    if actual.lower() != digest.lower():
        raise PreloadError(f"{target.name}: checksum mismatch (expected {digest[:12]}..., got {actual[:12]}...)")
    return rel


def has_video(path: str | Path) -> bool:
    """Use ffprobe when available; otherwise fall back to the extension."""
    try:
        proc = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(path),
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if proc.returncode == 0:
            return "video" in proc.stdout.lower()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return Path(path).suffix.lower() in VIDEO_SUFFIXES


# --------------------------------------------------------------------------
# player agent (runs on every device)
# --------------------------------------------------------------------------
PLAYER_SCRIPT = r'''
import atexit
import os
import sys
import time

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst

path, start_at, video = sys.argv[1], float(sys.argv[2]), sys.argv[3] == "1"
pidfile = sys.argv[4] if len(sys.argv) > 4 else ""
sink = sys.argv[5] if len(sys.argv) > 5 else ""
if pidfile:
    pidfile = os.path.abspath(os.path.expanduser(pidfile))
    os.makedirs(os.path.dirname(pidfile), exist_ok=True)
    with open(pidfile, "w", encoding="utf-8") as handle:
        handle.write(str(os.getpid()))

    def _remove_pidfile():
        try:
            os.unlink(pidfile)
        except OSError:
            pass

    atexit.register(_remove_pidfile)

Gst.init(None)
uri = Gst.filename_to_uri(os.path.abspath(os.path.expanduser(path)))
player = Gst.ElementFactory.make("playbin", "player")
if player is None:
    sys.exit("playbin is not available")
player.set_property("uri", uri)
player.set_property(
    "video-sink",
    Gst.ElementFactory.make("waylandsink" if video else "fakesink", "video"),
)
if not sink:
    sys.exit("no hardware playback sink selected")
audio = Gst.ElementFactory.make("pulsesink", "audio")
if audio is None:
    sys.exit("pulsesink is not available; refusing to use a virtual default sink")
audio.set_property("device", sink)
player.set_property("audio-sink", audio)
player.set_state(Gst.State.PAUSED)
bus = player.get_bus()

prerolled = False
deadline = time.time() + 30
while time.time() < deadline:
    msg = bus.timed_pop_filtered(
        100 * Gst.MSECOND,
        Gst.MessageType.ASYNC_DONE | Gst.MessageType.ERROR | Gst.MessageType.EOS
        | Gst.MessageType.STATE_CHANGED,
    )
    if msg is None:
        continue
    if msg.type == Gst.MessageType.ERROR:
        err, debug = msg.parse_error()
        sys.stderr.write(f"gst error: {err.message}\n{debug}\n")
        sys.exit(1)
    if msg.type == Gst.MessageType.ASYNC_DONE:
        prerolled = True
        break
    if msg.type == Gst.MessageType.STATE_CHANGED:
        old, new, pending = msg.parse_state_changed()
        if new == Gst.State.PAUSED and pending == Gst.State.VOID_PENDING:
            prerolled = True
            break
if not prerolled:
    sys.exit("preroll timed out")

delay = start_at - time.time()
if delay > 0:
    time.sleep(delay)
player.set_state(Gst.State.PLAYING)


def on_message(_bus, message, loop):
    if message.type == Gst.MessageType.EOS:
        loop.quit()
    elif message.type == Gst.MessageType.ERROR:
        err, debug = message.parse_error()
        sys.stderr.write(f"gst error: {err.message}\n{debug}\n")
        loop.quit()


loop = GLib.MainLoop()
bus.add_signal_watch()
bus.connect("message", on_message, loop)
loop.run()
player.set_state(Gst.State.NULL)
'''


def player_argv(
    path: str,
    start_at: float,
    video: bool,
    pidfile: str = "",
    sink: str = "",
) -> list[str]:
    payload = base64.b64encode(PLAYER_SCRIPT.encode()).decode()
    code = f"import base64;exec(base64.b64decode({payload!r}))"
    return [
        "python3",
        "-c",
        code,
        path,
        f"{start_at:.3f}",
        "1" if video else "0",
        pidfile,
        sink,
        PLAYER_MARKER,
    ]


REMOTE_ENV = (
    "export XDG_RUNTIME_DIR=/run/user/$(id -u); "
    "export DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u)/bus; "
    "export WAYLAND_DISPLAY=${WAYLAND_DISPLAY:-wayland-1}; "
    'cd "$HOME" || exit 1; '
)


def _stderr():
    return None if os.environ.get("AUDIOSYNC_PRELOAD_VERBOSE") else subprocess.DEVNULL


def start(
    targets: list[Target],
    media: str | Path,
    digest: str,
    start_at: float,
    video: bool,
    progress=None,
    remote_rel: str | None = None,
    sinks: dict[str, str] | None = None,
) -> list[subprocess.Popen]:
    """Launch the already-prerolled agents; they sleep until ``start_at``."""
    processes: list[subprocess.Popen] = []
    local_path = str(Path(media).expanduser().resolve())
    remote_path = remote_rel or cache_relpath(digest, Path(media).name)
    local_pidfile = str(Path.home() / CACHE_ROOT / digest / "player.pid")
    remote_pidfile = cache_relpath(digest, "player.pid")
    prepared: list[tuple[Target, str]] = []
    for target in targets:
        sink = (sinks or {}).get(target.name, "")
        try:
            sink = safety.safe_client_sink(sink, allow_alsa_device=False)
        except safety.RoutingError as exc:
            raise PreloadError(f"{target.name}: {exc}") from exc
        prepared.append((target, sink))
    for target, sink in prepared:
        if is_local_target(target):
            argv = player_argv(local_path, start_at, video, local_pidfile, sink)
            process = subprocess.Popen(
                argv,
                stdout=subprocess.DEVNULL,
                stderr=_stderr(),
                start_new_session=True,
            )
        else:
            assert target.host
            argv = player_argv(remote_path, start_at, video, remote_pidfile, sink)
            command = REMOTE_ENV + "exec " + shlex.join(argv)
            process = subprocess.Popen(
                ["ssh", *SSH_OPTIONS, target.host, command],
                stdout=subprocess.DEVNULL,
                stderr=_stderr(),
                start_new_session=True,
            )
        processes.append(process)
        if progress:
            progress(f"{target.name}: player started, waiting for {start_at - time.time():.1f}s")
    return processes


STOP_SCRIPT = r'''
import glob
import json
import os
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor


def run(command):
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=8)
    except (OSError, subprocess.SubprocessError):
        return None


def read_cmdline(pid, settle_s=0.2):
    """argv for a freshly exec'd child can read empty for a moment.

    Returns None when the process is gone (or a zombie), the bytes once they
    are readable, and b"" if a live process still has no readable argv.
    """
    deadline = time.time() + settle_s
    while True:
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as handle:
                data = handle.read()
        except OSError:
            return None
        if data:
            return data
        try:
            with open(f"/proc/{pid}/stat", encoding="utf-8") as handle:
                state = handle.read().rsplit(")", 1)[1].split()[0]
        except (OSError, IndexError):
            return None
        if state == "Z":
            return None
        if time.time() >= deadline:
            return data
        time.sleep(0.01)


pattern = os.path.expanduser(sys.argv[1])
marker = sys.argv[2].encode()
restore = sys.argv[3] == "1"
players = 0


def scan_players():
    """Kill marked players named by PID files; return how many we killed."""
    killed = 0
    for pidfile in glob.glob(pattern):
        try:
            with open(pidfile, encoding="utf-8") as handle:
                pid = int(handle.read().strip())
        except (OSError, ValueError):
            try:
                os.unlink(pidfile)
            except OSError:
                pass
            continue
        cmdline = read_cmdline(pid)
        if cmdline is None:
            try:
                os.unlink(pidfile)
            except OSError:
                pass
            continue
        if marker in cmdline:
            try:
                os.kill(pid, signal.SIGTERM)
                killed += 1
            except OSError:
                pass
            try:
                os.unlink(pidfile)
            except OSError:
                pass
            continue
        if cmdline:
            # Readable argv with no marker: definitely not our player.
            try:
                os.unlink(pidfile)
            except OSError:
                pass
            continue
        # Live process whose argv is still unreadable: keep the PID file for
        # the second scan; Never signal a PID we cannot attribute.
    return killed


players += scan_players()

# mpv launched by lipsync uses this exclusive IPC socket; a user's own mpv
# without it is deliberately left alone.
for cmdpath in glob.glob("/proc/[0-9]*/cmdline"):
    try:
        with open(cmdpath, "rb") as handle:
            cmdline = handle.read()
    except OSError:
        continue
    if b"audiosync-mpv.sock" not in cmdline:
        continue
    try:
        pid = int(cmdpath.split("/")[2])
        os.kill(pid, signal.SIGTERM)
        players += 1
    except (OSError, ValueError):
        pass

units = 0
stopped = []
for unit in ("snapclient.service", "snapcast-capture.service", "snapserver.service"):
    state = run(["systemctl", "--user", "is-active", unit])
    if state is not None and state.stdout.strip() == "active":
        stopped.append(unit)
if stopped:
    units = len(stopped)
    run(["systemctl", "--user", "stop", *stopped])

rtp = 0
listed = run(["pactl", "-f", "json", "list", "modules"])
try:
    modules = json.loads(listed.stdout or "[]") if listed is not None else []
except ValueError:
    modules = []
for module in modules:
    name = str(module.get("name") or "")
    args = str(module.get("argument") or "")
    if name not in ("libpipewire-module-rtp-source", "libpipewire-module-rtp-sink"):
        continue
    if "46000" not in args or not any(mark in args for mark in ("rtp_synced_in", "rtp_out_")):
        continue
    index = (module.get("properties") or {}).get("object.id")
    if index is None:
        continue
    result = run(["pactl", "unload-module", str(index)])
    if result is not None and result.returncode == 0:
        rtp += 1

# Config-loaded PipeWire modules are protected from unload/destroy, but their
# receiving stream node is ours and can be destroyed.  This closes the audio
# without restarting PipeWire and killing unrelated user streams.
streams = run(["pactl", "-f", "json", "list", "sink-inputs"])
try:
    inputs = json.loads(streams.stdout or "[]") if streams is not None else []
except ValueError:
    inputs = []
for item in inputs:
    props = item.get("properties") or {}
    node = str(props.get("node.name") or "")
    if node != "rtp_synced_in":
        continue
    index = item.get("index")
    if index is None:
        continue
    result = run(["pw-cli", "destroy", str(index)])
    if result is not None and result.returncode == 0:
        rtp += 1

# A player launched just before Stop may not have written its PID file yet.
# Re-scan after the units are down so that late PID writes are still caught.
time.sleep(0.25)
players += scan_players()

started = []
if restore and stopped:
    def start_unit(unit):
        result = run(["systemctl", "--user", "start", unit])
        return unit if result is not None and result.returncode == 0 else None

    with ThreadPoolExecutor(max_workers=len(stopped)) as pool:
        started = [unit for unit in pool.map(start_unit, stopped) if unit]

print(json.dumps({"players": players, "rtp": rtp, "units": units, "active": stopped, "started": started}))
'''

def is_audiosync_rtp(module: dict) -> bool:
    """True only for the legacy Audiosync receiver/sender signature."""
    name = str(module.get("name") or "")
    args = str(module.get("argument") or "")
    return (
        name in ("libpipewire-module-rtp-source", "libpipewire-module-rtp-sink")
        and "46000" in args
        and any(mark in args for mark in ("rtp_synced_in", "rtp_out_"))
    )


def _kill_pidfile(path: Path) -> int:
    """Terminate the player named by one PID file, but only our player."""
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        try:
            path.unlink()
        except OSError:
            pass
        return 0
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        cmdline = b""
    killed = 0
    if PLAYER_MARKER.encode() in cmdline:
        try:
            os.kill(pid, signal.SIGTERM)
            killed = 1
        except OSError:
            pass
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
    return killed


def _stop_target(target: Target, pattern: str, restore: bool = True) -> dict:
    """Stop one device and, unless disabled, put its Snapcast units back.

    Everything happens in a single invocation: two PID scans with a short
    settle pause, teardown, and the unit restart.  That keeps Stop to one
    round trip per device instead of one per phase.
    """
    payload = base64.b64encode(STOP_SCRIPT.encode()).decode()
    code = f"import base64;exec(base64.b64decode({payload!r}))"
    argv = [sys.executable, "-c", code, pattern, PLAYER_MARKER, "1" if restore else "0"]
    if is_local_target(target):
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=20)
        out = proc.stdout
    else:
        assert target.host
        out = _run(
            ["ssh", *STOP_SSH_OPTIONS, target.host, REMOTE_ENV + "exec " + shlex.join(argv)],
            timeout=20,
        ).stdout
    try:
        return json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        raise PreloadError(f"stop returned no result: {out.strip()[:200]}") from exc


def stop(
    targets: list[Target],
    digest: str | None = None,
    progress=None,
    restore: bool = True,
) -> int:
    """Close every Audiosync-owned audio source on every target.

    This includes preload players, the legacy ``rtp_synced_in`` receiver, a
    lipsync mpv, and the Snapcast units.  ``digest=None`` means every preload
    session, so a restarted GUI can still stop playback it did not start.
    With ``restore`` the Snapcast units that were running are started again
    afterwards, leaving each device listening instead of silently stopped.
    PID files and RTP modules are checked against Audiosync markers before
    anything is signalled or unloaded; unrelated user audio is untouched.

    Each device is handled by one parallel transaction that tears down,
    re-scans for late PID writes, and restarts its own units, so Stop costs
    one round trip per device instead of one per phase.
    """
    failures: list[str] = []
    affected: set[str] = set()
    local_pattern = str(Path.home() / CACHE_ROOT / "*" / "player.pid")
    remote_pattern = cache_relpath(digest, "player.pid") if digest else f"{CACHE_ROOT}/*/player.pid"
    if progress:
        for target in targets:
            progress(f"{target.name}: stopping playback")

    def stop_target(target):
        try:
            pattern = local_pattern if is_local_target(target) else remote_pattern
            return target, _stop_target(target, pattern, restore), None
        except (PreloadError, ValueError, OSError, subprocess.SubprocessError) as exc:
            return target, None, exc

    # A single dropped SSH connection must not leave audio running or the
    # window stuck.  Retry failed devices once; successful ones never repeat.
    pending = list(targets)
    for attempt in range(2):
        with ThreadPoolExecutor(max_workers=max(1, len(pending))) as pool:
            outcomes = list(pool.map(stop_target, pending))
        retry: list[Target] = []
        for target, result, error in outcomes:
            if error is not None:
                if attempt == 0:
                    retry.append(target)
                else:
                    failures.append(f"{target.name}: {error}")
                continue
            if result.get("players", 0) or result.get("rtp", 0) or result.get("units", 0):
                affected.add(target.name)
            active = result.get("active") or []
            started = result.get("started") or []
            if restore and active:
                if progress:
                    progress(
                        f"{target.name}: Snapcast restored ({', '.join(started)})"
                        if started
                        else f"{target.name}: Snapcast did not restart"
                    )
                if not started:
                    failures.append(f"{target.name}: Snapcast did not restart")
        if not retry:
            break
        if progress:
            for target in retry:
                progress(f"{target.name}: retrying stop")
        pending = retry
        time.sleep(0.2)
    if failures:
        raise PreloadError("; ".join(failures))
    return len(affected)


class Job:
    """Copy, verify, preroll, and start one media file on every target."""

    def __init__(
        self,
        media: str | Path,
        targets: list[Target],
        delay_s: float = 10.0,
        library=None,
    ) -> None:
        self.media = str(media)
        try:
            self.targets = safety.unique_targets(targets, local_host_names())
        except safety.RoutingError as exc:
            raise PreloadError(str(exc)) from exc
        self.delay_s = max(0.0, float(delay_s))
        #: A synced ``library.Library``; its copy is reused instead of a transfer.
        self.library = library
        #: Devices that could not be reached; the rest of the run continues.
        self.failures: list[str] = []
        #: Player (and ssh) processes this job launched, for live state.
        self.processes: list[subprocess.Popen] = []
        self._cancelled = threading.Event()

    def playing(self) -> int:
        """How many players (or their ssh links) this job launched still run."""
        return sum(1 for process in self.processes if process.poll() is None)

    def cancel(self) -> None:
        """Ask run() to stop before it launches any player."""
        self._cancelled.set()

    def _check_cancelled(self) -> None:
        if self._cancelled.is_set():
            raise PreloadCancelled("preload cancelled")

    def run(self, progress=None) -> int:
        def say(message: str) -> None:
            if progress:
                progress(message)

        media = Path(self.media).expanduser().resolve()
        if not media.is_file():
            raise PreloadError(f"media not found: {media}")
        if not self.targets:
            raise PreloadError("no playback devices selected")

        digest = sha256_file(media)
        say(f"{media.name}: {digest[:12]}... ({media.stat().st_size / 1048576:.1f} MiB)")
        remote_rel = None
        if self.library is not None:
            remote_rel = self.library.remote_relpath(media)
            say(f"{self.library.label}: checking synced copy")

        self._check_cancelled()
        reachable, failures = probe_targets(self.targets)
        self.failures = []
        for target, error in failures:
            self.failures.append(f"{target.name}: {error}")
            say(f"{target.name}: skipped ({error})")
        if self.failures and not reachable:
            raise PreloadError("; ".join(self.failures))
        reachable_ids = {id(target) for target in reachable}
        active = [
            target
            for target in self.targets
            if is_local_target(target) or id(target) in reachable_ids
        ]

        sinks: dict[str, str] = {}
        safe_active: list[Target] = []
        for target in active:
            sink, error = preflight_target(target)
            if error:
                self.failures.append(error)
                say(f"{target.name}: refused ({error})")
                continue
            sinks[target.name] = sink
            safe_active.append(target)
        if self.failures and not safe_active:
            raise PreloadError("; ".join(self.failures))
        active = safe_active

        for index, target in enumerate(active, start=1):
            self._check_cancelled()
            say(f"[{index}/{len(active)}] {target.name}: preparing")
            if remote_rel and not is_local_target(target):
                if remote_sha256(target, remote_rel) == digest:
                    say(f"{target.name}: using synced copy")
                    continue
                say(f"{target.name}: synced copy is stale, sending only this file")
            copy_to(target, media, digest, say, rel=remote_rel)

        self._check_cancelled()
        video = has_video(media)
        start_at = time.time() + self.delay_s
        say(f"all {len(active)} device(s) loaded - starting in {self.delay_s:.0f}s")
        self.processes = start(
            active, media, digest, start_at, video, say, remote_rel=remote_rel, sinks=sinks
        )

        while True:
            self._check_cancelled()
            remaining = start_at - time.time()
            if remaining <= 0:
                break
            say(f"playing in {math.ceil(remaining)}s")
            time.sleep(min(1.0, max(0.05, remaining)))
        say(f"playing on {len(active)} device(s)")
        return len(active)
