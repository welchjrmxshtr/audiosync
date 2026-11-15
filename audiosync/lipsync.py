"""Video compensation for a delayed audio path.

The group's audio is N ms behind the app's timeline, so the *player* has to
emit audio N ms early - which is the same thing as showing the picture N ms
late.  For mpv/VLC/Kodi the number is exact, not approximate: a constant
offset cancels a constant delay.

Browsers have no such control, which is why the Auto preset exists.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
from pathlib import Path

DEFAULT_MPV_SOCKET = Path("/tmp/audiosync-mpv.sock")


def offset_seconds(buffer_ms: int) -> float:
    """Negative = audio emitted earlier, i.e. video effectively delayed."""
    return -abs(buffer_ms) / 1000.0


def offset_label(buffer_ms: int) -> str:
    return f"\u2212{abs(buffer_ms)} ms"


# --------------------------------------------------------------------------
# mpv
# --------------------------------------------------------------------------
def mpv_args(buffer_ms: int, ipc_socket: Path | str | None = None) -> list[str]:
    args = [f"--audio-delay={offset_seconds(buffer_ms):.3f}"]
    sock = Path(ipc_socket) if ipc_socket else DEFAULT_MPV_SOCKET
    args.append(f"--input-ipc-server={sock}")
    return args


def mpv_available() -> bool:
    return shutil.which("mpv") is not None


def launch_mpv(buffer_ms: int, media: list[str] | None = None, ipc_socket: Path | str | None = None) -> subprocess.Popen:
    """Start mpv already compensated, so sync is right from the first frame."""
    binary = shutil.which("mpv")
    if binary is None:
        raise FileNotFoundError("mpv is not installed")
    cmd = [binary, *mpv_args(buffer_ms, ipc_socket), *(media or [])]
    return subprocess.Popen(cmd, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def mpv_set_offset(ipc_socket: Path | str | None, buffer_ms: int) -> bool:
    """Live-update a running mpv over its IPC socket.

    Called when the controller settles on a new buffer so the compensation
    never goes stale mid-film.
    """
    sock_path = str(ipc_socket or DEFAULT_MPV_SOCKET)
    payload = json.dumps(
        {"command": ["set_property", "audio-delay", offset_seconds(buffer_ms)]}
    ).encode() + b"\n"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(0.5)
            client.connect(sock_path)
            client.sendall(payload)
        return True
    except OSError:
        return False


# --------------------------------------------------------------------------
# VLC / Kodi
# --------------------------------------------------------------------------
def vlc_args(buffer_ms: int) -> list[str]:
    return [f"--audio-desync={int(offset_seconds(buffer_ms) * 1000)}"]


def vlc_command(buffer_ms: int, media: str = "FILE") -> str:
    return f"vlc {' '.join(vlc_args(buffer_ms))} {media}"


def kodi_hint(buffer_ms: int) -> str:
    ms = abs(buffer_ms)
    return (
        "Kodi: Settings \u2192 Player \u2192 Videos \u2192 Playback \u2192 "
        f"'Audio offset' = \u2212{ms} ms"
    )


def browser_hint(buffer_ms: int) -> str:
    return (
        "Browsers cannot be offset. Use the Auto preset "
        f"(currently {abs(buffer_ms)} ms) when watching video in a browser."
    )


def guidance(buffer_ms: int) -> dict[str, str]:
    """Everything the UI needs for the lip-sync row."""
    return {
        "offset": offset_label(buffer_ms),
        "mpv": "mpv is launched already compensated" if mpv_available() else "mpv not installed",
        "vlc": vlc_command(buffer_ms),
        "kodi": kodi_hint(buffer_ms),
        "browser": browser_hint(buffer_ms),
    }


def mpv_socket_in_use(ipc_socket: Path | str | None = None) -> bool:
    path = str(ipc_socket or DEFAULT_MPV_SOCKET)
    return os.path.exists(path)
