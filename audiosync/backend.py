"""Control plane: systemd user units plus Snapcast's JSON-RPC.

The app never spawns snapserver/snapclient itself.  It writes config and
``EnvironmentFile`` data, then restarts the user units, so a crashed or
closed GUI leaves the audio path exactly as it was.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from . import config, outputs, safety

UNIT_SERVER = "snapserver.service"
UNIT_CAPTURE = "snapcast-capture.service"
UNIT_CLIENT = "snapclient.service"
SAMPLE_FORMAT = "48000:16:2"


class BackendError(RuntimeError):
    """A unit refused to start, or the RPC endpoint said no."""


# --------------------------------------------------------------------------
# process helpers
# --------------------------------------------------------------------------
def _run(cmd: list[str], timeout: float = 20.0) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def systemctl(action: str, *units: str, timeout: float = 20.0) -> tuple[bool, str]:
    if not units:
        return True, ""
    try:
        proc = _run(["systemctl", "--user", action, *units], timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    return proc.returncode == 0, (proc.stdout + proc.stderr).strip()


def unit_active(unit: str) -> bool:
    ok, out = systemctl("is-active", unit)
    return ok and "active" in out


def daemon_reload() -> None:
    systemctl("daemon-reload")


def journal_tail(unit: str, lines: int = 20) -> str:
    try:
        proc = _run(["journalctl", "--user", "-u", unit, "-n", str(lines), "--no-pager", "-o", "cat"], timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout.strip()


def local_ipv4_addresses() -> set[str]:
    """LAN addresses used to match a persisted client whose name changed."""
    addresses: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addresses.add(str(info[4][0]))
    except OSError:
        pass
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("192.0.2.1", 9))
            addresses.add(sock.getsockname()[0])
        finally:
            sock.close()
    except OSError:
        pass
    return {ip for ip in addresses if not ip.startswith("127.")}


# --------------------------------------------------------------------------
# Snapcast JSON-RPC over HTTP
# --------------------------------------------------------------------------
def rpc(host: str, method: str, params: dict | None = None, port: int = config.SNAPCAST_HTTP_PORT, timeout: float = 2.5) -> dict:
    body: dict[str, Any] = {"id": 1, "jsonrpc": "2.0", "method": method}
    if params is not None:
        body["params"] = params
    request = urllib.request.Request(
        f"http://{host}:{port}/jsonrpc",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode())
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise BackendError(f"{method} failed: {exc}") from exc
    if "error" in payload:
        error = payload["error"]
        message = error.get("message") if isinstance(error, dict) else str(error)
        raise BackendError(f"{method} rejected: {message}")
    return payload.get("result", {}) or {}


def reachable(host: str, port: int = config.SNAPCAST_STREAM_PORT, timeout: float = 0.7) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def server_ready(host: str, timeout: float = 6.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            rpc(host, "Server.GetRPCVersion", timeout=0.8)
            return True
        except BackendError:
            time.sleep(0.2)
    return False


# --------------------------------------------------------------------------
# backend
# --------------------------------------------------------------------------
class Backend:
    def __init__(self, cfg: config.Config) -> None:
        self.cfg = cfg

    # -- device detection --------------------------------------------
    @staticmethod
    def list_sinks() -> list[str]:
        try:
            proc = _run(["pactl", "list", "short", "sinks"], timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            return []
        sinks = []
        for line in proc.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                sinks.append(parts[1])
        return sinks

    def _sinks_text(self) -> str:
        try:
            return _run(["pactl", "list", "sinks"], timeout=5).stdout
        except (OSError, subprocess.TimeoutExpired):
            return ""

    def _cards_text(self) -> str:
        try:
            return Path("/proc/asound/cards").read_text(encoding="utf-8")
        except OSError:
            return ""

    def detect_hw_sink(self) -> str:
        """Best real output: analog/built-in speakers before any HDMI."""
        choice = outputs.choose_output(
            outputs.parse_pactl_sinks(self._sinks_text()),
            outputs.parse_asound_cards(self._cards_text()),
        )
        return choice.sink

    def ensure_virtual_sink(self) -> None:
        """The capture bridge needs ``audiosync_sink``; recreate it if absent."""
        if config.SINK_NAME in self.list_sinks():
            return
        systemctl("restart", "pipewire", "wireplumber")
        deadline = time.monotonic() + 6.0
        while time.monotonic() < deadline:
            if config.SINK_NAME in self.list_sinks():
                return
            time.sleep(0.4)
        raise BackendError(
            f"{config.SINK_NAME} did not appear after restarting PipeWire\n{journal_tail('pipewire')}"
        )

    def refresh_outputs(self, hosting: bool = False, restart: bool = True) -> tuple[bool, str, str]:
        """Re-resolve the hardware output and repair the cached tuple.

        Called at launch, on join, on host start and on every reconnect.  A
        cached HDMI sink is replaced when analog/built-in speakers exist;
        ``hw_card``/``output_device`` always come from the chosen sink's own
        ALSA card index, never from ``aplay -l``'s first card.
        """
        if hosting:
            self.ensure_virtual_sink()
        choice = outputs.choose_output(
            outputs.parse_pactl_sinks(self._sinks_text()),
            outputs.parse_asound_cards(self._cards_text()),
        )
        if not choice.sink:
            return False, self.cfg.hw_sink, "no hardware output found"

        desired_device = outputs.output_device(choice)
        changed = (
            self.cfg.hw_sink != choice.sink
            or self.cfg.hw_card != choice.card
            or self.cfg.output_device != desired_device
        )
        if changed:
            self.cfg.hw_sink = choice.sink
            self.cfg.hw_card = choice.card
            self.cfg.output_device = desired_device
            self.cfg.save()
            env = config.read_env_file(config.client_env_path())
            if env.get("AUDIOSYNC_HOST"):
                self.write_client_env(env["AUDIOSYNC_HOST"])
                if restart and unit_active(UNIT_CLIENT):
                    systemctl("restart", UNIT_CLIENT)
            if hosting and self.capture_mode() == "fifo":
                self.write_capture_env(self.cfg.playback)
                if restart and unit_active(UNIT_CAPTURE):
                    systemctl("restart", UNIT_CAPTURE)
        return changed, choice.sink, outputs.label(choice)

    @staticmethod
    def set_default_sink(name: str) -> bool:
        if not name:
            return False
        try:
            proc = _run(["pactl", "set-default-sink", name], timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return proc.returncode == 0

    def detect_player(self) -> tuple[str, str]:
        """Return ``(AUDIOSYNC_ARGS, soundcard)`` for the local client.

        Deliberately no probing: ``install.sh`` records the player once (this
        build offers ``alsa|pipewire`` only).  The app never launches
        snapclient, so it must not interrogate the binary either - it only
        writes ``client.env`` and restarts the unit.
        """
        player = (self.cfg.player or "pipewire").strip().lower()
        if player == "alsa":
            self.cfg.player = "alsa"
            return "--player alsa", self.cfg.output_device
        self.cfg.player = "pipewire"
        return "--player pipewire", self.cfg.hw_sink

    # -- config writing ----------------------------------------------
    def capture_mode(self) -> str:
        """Which capture path to use.

        ``pipewire://`` is tempting (no FIFO, no extra process) but on
        PipeWire 1.6 + snapserver 0.34 it never links to the sink's monitor
        ports, so the stream sits at ``idle``.  The parec/pw-cat bridge is
        deterministic, so it is the default; set ``AUDIOSYNC_CAPTURE=pipewire``
        to try the native path on a machine where it works.
        """
        forced = os.environ.get("AUDIOSYNC_CAPTURE", "").strip().lower()
        if forced in ("pipewire", "fifo"):
            return forced
        return "fifo"

    def capture_target(self, playback: str) -> str:
        """Sink whose output we capture on the host."""
        try:
            mode = safety.validate_playback(playback)
        except safety.RoutingError as exc:
            raise BackendError(str(exc)) from exc
        if mode in ("synced", "off"):
            return config.SINK_NAME
        sink = self.cfg.hw_sink or self.detect_hw_sink()
        try:
            sink = safety.safe_direct_capture_sink(sink)
        except safety.RoutingError as exc:
            raise BackendError(str(exc)) from exc
        if sink not in self._current_hardware_sinks():
            raise BackendError(
                f"Direct playback cannot capture {sink!r}; it is not a current output sink"
            )
        return sink

    def _current_hardware_sinks(self) -> set[str]:
        """Current, non-virtual sinks from full ``pactl`` metadata."""
        return outputs.hardware_sink_names(self._sinks_text())

    def write_server_conf(self, buffer_ms: int, codec: str, playback: str) -> None:
        sink = self.capture_target(playback)
        if self.capture_mode() == "pipewire":
            source = (
                f"pipewire://?name={config.STREAM_NAME}&capture_sink=true"
                f"&target={sink}&send_silence=true&idle_threshold=100"
            )
        else:
            source = f"pipe://{config.fifo_path()}?name={config.STREAM_NAME}&sampleformat={SAMPLE_FORMAT}&mode=read"

        text = (
            "# generated by audiosync - rewritten whenever settings change\n"
            "[stream]\n"
            "bind_to_address = 0.0.0.0\n"
            f"source = {source}\n"
            f"sampleformat = {SAMPLE_FORMAT}\n"
            f"codec = {codec}\n"
            "chunk_ms = 20\n"
            f"buffer = {max(40, int(buffer_ms))}\n"
            "send_to_muted = false\n"
            "\n[http]\n"
            "enabled = true\n"
            "bind_to_address = 0.0.0.0\n"
            f"port = {config.SNAPCAST_HTTP_PORT}\n"
            "\n[tcp-streaming]\n"
            "bind_to_address = 0.0.0.0\n"
            f"port = {config.SNAPCAST_STREAM_PORT}\n"
            "\n[logging]\n"
            "level = info\n"
        )
        config.write_atomic(config.snapserver_conf_path(), text)

    def write_capture_env(self, playback: str) -> None:
        sink = self.capture_target(playback)
        source = f"{sink}.monitor"
        flag = "--device" if self.cfg.recorder == "parec" else "--target"
        config.write_env_file(config.capture_env_path(), {"AUDIOSYNC_SRC_ARG": f"{flag}={source}"})

    def _validated_client_sink(self) -> tuple[str, str]:
        """Return client arguments only for a sink that currently exists."""
        args, soundcard = self.detect_player()
        try:
            soundcard = safety.safe_client_sink(
                soundcard, allow_alsa_device=self.cfg.player == "alsa"
            )
        except safety.RoutingError as exc:
            raise BackendError(str(exc)) from exc
        if self.cfg.player != "alsa" and soundcard not in self._current_hardware_sinks():
            raise BackendError(
                f"{soundcard!r} is not a current output sink; refusing to "
                "start snapclient with a stale or virtual route"
            )
        return args, soundcard

    def write_client_env(self, host: str) -> None:
        args, soundcard = self._validated_client_sink()
        args = f"{args} --soundcard {soundcard}"
        values = {"AUDIOSYNC_HOST": host, "AUDIOSYNC_ARGS": args}
        config.write_env_file(config.client_env_path(), values)

    # -- roles -------------------------------------------------------
    def host(self, buffer_ms: int, codec: str, playback: str) -> None:
        try:
            mode = safety.validate_playback(playback)
        except safety.RoutingError as exc:
            raise BackendError(str(exc)) from exc
        if not self.cfg.hw_sink:
            self.cfg.hw_sink = self.detect_hw_sink()

        # Validate the whole route before touching a unit.  In particular,
        # an empty client sink would make snapclient use the virtual default
        # sink once hosting starts, feeding captured audio back to itself.
        if mode == "direct":
            self.capture_target(mode)
        if mode == "synced":
            self._validated_client_sink()

        # Point the default sink first: applications must render into the
        # right place before the capture graph is rebuilt.
        target_default = config.SINK_NAME if mode in ("synced", "off") else self.cfg.hw_sink
        if target_default:
            self.set_default_sink(target_default)

        self.write_server_conf(buffer_ms, codec, mode)
        if self.capture_mode() == "fifo":
            self.write_capture_env(mode)

        ok, message = systemctl("restart", UNIT_SERVER)
        if not ok:
            raise BackendError(f"snapserver did not start: {message}\n{journal_tail(UNIT_SERVER)}")
        if not server_ready("127.0.0.1"):
            raise BackendError(f"snapserver started but is not answering on :1780\n{journal_tail(UNIT_SERVER)}")

        if self.capture_mode() == "fifo":
            ok, message = systemctl("restart", UNIT_CAPTURE)
            if not ok:
                raise BackendError(f"capture bridge did not start: {message}\n{journal_tail(UNIT_CAPTURE)}")
        else:
            systemctl("stop", UNIT_CAPTURE)

        if mode == "synced":
            self.join("127.0.0.1", host_role=True)
        else:
            systemctl("stop", UNIT_CLIENT)

    def join(self, host: str, host_role: bool = False) -> None:
        self.write_client_env(host)
        ok, message = systemctl("restart", UNIT_CLIENT)
        if not ok:
            raise BackendError(f"snapclient did not start: {message}\n{journal_tail(UNIT_CLIENT)}")

    def stop(self) -> None:
        systemctl("stop", UNIT_CLIENT)
        systemctl("stop", UNIT_CAPTURE)
        systemctl("stop", UNIT_SERVER)
        if self.cfg.hw_sink:
            self.set_default_sink(self.cfg.hw_sink)

    def stop_client(self) -> None:
        systemctl("stop", UNIT_CLIENT)

    def diagnose_peer(self, address: str) -> dict:
        """Test the actual ports instead of blaming "the network".

        Returns ``{address, checks, ok, summary}``; ``summary`` is the line the
        UI shows when a join fails.
        """
        checks = {
            "audio": reachable(address, config.SNAPCAST_STREAM_PORT, timeout=0.8),
            "control": reachable(address, config.SNAPCAST_CONTROL_PORT, timeout=0.8),
            "http": reachable(address, config.SNAPCAST_HTTP_PORT, timeout=0.8),
        }
        if checks["audio"]:
            summary = f"{address}: audio port {config.SNAPCAST_STREAM_PORT} is reachable"
        else:
            summary = (
                f"{address}: port {config.SNAPCAST_STREAM_PORT} is not reachable. "
                "Either snapserver is not running there, or that machine's firewall is closed. "
                "On that machine: sudo firewall-cmd --permanent "
                f"--add-port={config.SNAPCAST_STREAM_PORT}/tcp "
                f"--add-port={config.SNAPCAST_CONTROL_PORT}/tcp "
                f"--add-port={config.SNAPCAST_HTTP_PORT}/tcp && "
                "sudo firewall-cmd --permanent --add-service=mdns && sudo firewall-cmd --reload"
            )
        return {"address": address, "checks": checks, "ok": checks["audio"], "summary": summary}

    # -- live control ------------------------------------------------
    def clients(self, host: str) -> list[dict]:
        """Flatten Server.GetStatus into rows the UI can render."""
        result = rpc(host, "Server.GetStatus")
        rows: list[dict] = []
        for group in result.get("server", {}).get("groups", []):
            for client in group.get("clients", []):
                rows.append(
                    {
                        "id": client.get("id", ""),
                        "name": client.get("config", {}).get("name") or client.get("host", {}).get("name", "?"),
                        "host": client.get("host", {}).get("name", "?"),
                        "connected": bool(client.get("connected")),
                        "volume": client.get("config", {}).get("volume", {}),
                        "latency": int(client.get("config", {}).get("latency", 0) or 0),
                    }
                )
        return rows

    def forget_pairings(
        self,
        host: str,
        device_name: str,
        local: bool = False,
        include_connected: bool = True,
    ) -> int:
        """Delete this device's Snapcast history, live or from server.json.

        ``Server.DeleteClient`` removes both connected and disconnected
        records; callers stop this device's client first (or restart the
        server) so deleting a connected record cannot drop another speaker.
        When the local server is down, prune its persisted file instead.  A
        closed control port is not an error: clearing history is best-effort.
        """
        try:
            result = rpc(host, "Server.GetStatus", timeout=1.5)
        except BackendError:
            if local:
                return config.prune_pairing_history(
                    config.snapserver_state_path(), device_name, local_ipv4_addresses()
                )
            return 0

        target = (device_name or "").strip().casefold()
        addresses = local_ipv4_addresses()
        removed = 0
        for group in result.get("server", {}).get("groups", []):
            for client in group.get("clients", []):
                host_info = client.get("host") if isinstance(client.get("host"), dict) else {}
                name = str(host_info.get("name") or "").strip().casefold()
                ip = str(host_info.get("ip") or "")
                if not ((target and name == target) or (ip and ip in addresses)):
                    continue
                if not include_connected and client.get("connected"):
                    continue
                client_id = client.get("id")
                if not client_id:
                    continue
                try:
                    rpc(host, "Server.DeleteClient", {"id": client_id}, timeout=1.5)
                    removed += 1
                except BackendError:
                    continue
        return removed

    def set_client_volume(self, host: str, client_id: str, percent: int, muted: bool = False) -> None:
        rpc(host, "Client.SetVolume", {"id": client_id, "volume": {"percent": max(0, min(100, int(percent))), "muted": muted}})

    def set_client_latency(self, host: str, client_id: str, latency_ms: int) -> None:
        rpc(host, "Client.SetLatency", {"id": client_id, "latency": int(latency_ms)})

    def try_set_buffer(self, host: str, buffer_ms: int) -> bool:
        """0.34 has no Stream.SetBuffer; probe rather than assume."""
        for method in ("Stream.SetBuffer", "Server.SetBuffer"):
            try:
                rpc(host, method, {"buffer": int(buffer_ms)}, timeout=1.2)
                return True
            except BackendError:
                continue
        return False

    def apply_buffer(self, host: str, buffer_ms: int, codec: str, playback: str) -> bool:
        """Live-update if the server supports it, else restart (brief gap)."""
        if self.try_set_buffer(host, buffer_ms):
            return True
        self.write_server_conf(buffer_ms, codec, playback)
        ok, _ = systemctl("restart", UNIT_SERVER)
        return ok

    def stream_playing(self, host: str) -> bool:
        try:
            result = rpc(host, "Server.GetStatus", timeout=1.5)
        except BackendError:
            return False
        for stream in result.get("server", {}).get("streams", []):
            if stream.get("status") == "playing":
                return True
        return False

    def status_line(self, host: str) -> str:
        parts = []
        for unit, label in ((UNIT_SERVER, "server"), (UNIT_CLIENT, "client"), (UNIT_CAPTURE, "capture")):
            parts.append(f"{label}:{'up' if unit_active(unit) else 'down'}")
        return " ".join(parts)
