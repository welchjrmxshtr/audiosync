"""Configuration, paths and persisted state.

The application's own state lives under ``~/.config/audiosync`` and its
state directory.  The optional SMB media mount is separate system state:
``scripts/setup-smb-mount.sh`` owns its fstab entry and root-only credentials,
and ``install.sh --uninstall`` detects and offers to remove them.  Secrets:
the app itself stores none; the Snapcast control plane is LAN-local by design.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import safety

APP_NAME = "audiosync"
VERSION = "1.0.0"

#: Snapcast defaults (see plan's wire-protocol table).
SNAPCAST_STREAM_PORT = 1704
SNAPCAST_CONTROL_PORT = 1705
SNAPCAST_HTTP_PORT = 1780

#: mDNS service this app announces; snapserver announces _snapcast._tcp itself.
DISCOVERY_TYPE = "_audiosync._tcp.local."
DISCOVERY_NAME = "_audiosync._tcp"

#: PipeWire objects we create.
SINK_NAME = safety.SINK_NAME
SINK_DESCRIPTION = "Audiosync"
STREAM_NAME = "Audiosync"

PRESETS = ("auto", "deep", "manual")
PLAYBACK_MODES = safety.PLAYBACK_MODES


class ConfigError(ValueError):
    """Persisted settings cannot be safely used for a routing operation."""


def normalize_library_roots(roots: Any) -> list[dict[str, str]]:
    """Accept plain-string roots from older configs and return records."""
    records: list[dict[str, str]] = []
    for item in roots if isinstance(roots, list) else []:
        if isinstance(item, str):
            item = {"path": item}
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "").strip()
        if not path:
            continue
        records.append({"path": path, "identity": str(item.get("identity") or "").strip()})
    return records


def config_dir() -> Path:
    return Path(os.environ.get("AUDIOSYNC_CONFIG_DIR") or Path.home() / ".config" / APP_NAME)


def state_dir() -> Path:
    path = Path(os.environ.get("AUDIOSYNC_STATE_DIR") or Path.home() / ".local" / "share" / APP_NAME)
    return path


def fifo_path() -> Path:
    return Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp") / f"{APP_NAME}.fifo"


def config_file() -> Path:
    return config_dir() / "config.json"


def snapserver_conf_path() -> Path:
    return config_dir() / "snapserver.conf"


def capture_env_path() -> Path:
    return config_dir() / "capture.env"


def client_env_path() -> Path:
    return config_dir() / "client.env"


def snapserver_state_path() -> Path:
    """Where snapserver persists its groups and client history."""
    override = os.environ.get("AUDIOSYNC_SNAPSERVER_STATE")
    if override:
        return Path(override)
    return Path.home() / ".config" / "snapserver" / "server.json"


def machine_id() -> str:
    """Stable short id for discovery, independent of the hostname."""
    for candidate in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            raw = Path(candidate).read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if raw:
            return hashlib.sha256(raw.encode()).hexdigest()[:12]
    return hashlib.sha256(socket.gethostname().encode()).hexdigest()[:12]


def device_name() -> str:
    return os.environ.get("AUDIOSYNC_DEVICE_NAME") or socket.gethostname()


@dataclass
class Config:
    """User-visible settings plus what the latency controller learned."""

    playback: str = "synced"  # synced | direct | off
    preset: str = "auto"  # auto | deep | manual
    manual_buffer_ms: int = 250
    preferred_host: str | None = None  # peer hostname; None means "this device"
    codec: str = "pcm"  # effective codec chosen by codec.choose_codec()
    hw_sink: str = ""  # PipeWire sink name of the real hardware output
    hw_card: str = ""  # ALSA card *id* (not index, not card_name) for the alsa fallback
    player: str = "pulse"  # pulse | alsa, probed at install time
    output_device: str = ""  # hw:<card>,0 when player == alsa
    recorder: str = "parec"  # parec | pw-cat, whichever install.sh found
    learned_floor_ms: dict[str, int] = field(default_factory=dict)
    #: Physical microphone source per stable device name; monitor sources are never stored.
    mic_sources: dict[str, str] = field(default_factory=dict)
    #: Last acoustic per-client latency offset applied by Snapcast.
    client_latency_ms: dict[str, int] = field(default_factory=dict)
    #: Audible probe during playback; off unless the user explicitly opts in.
    acoustic_monitor: bool = False
    acoustic_interval_s: int = 30
    acoustic_volume: int = 20
    extra_hosts: list[str] = field(default_factory=list)  # manual IPs, mDNS gaps
    #: Synced media roots: {"path": "/mnt/storage/Media", "identity": "smb:storage/Media"}
    library_roots: list[dict[str, str]] = field(default_factory=list)

    # --- persistence -------------------------------------------------
    @classmethod
    def load(cls, *, strict: bool = False) -> "Config":
        try:
            raw = json.loads(config_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        if not isinstance(raw, dict):
            return cls()
        if strict:
            cls._validate_strict(raw)
        known = {f for f in cls.__dataclass_fields__}
        values = {k: v for k, v in raw.items() if k in known}
        try:
            values["playback"] = safety.validate_playback(values.get("playback", "synced"))
        except safety.RoutingError:
            values["playback"] = "synced"
        if safety.is_virtual_sink(values.get("hw_sink")):
            values["hw_sink"] = ""
        values["library_roots"] = normalize_library_roots(values.get("library_roots"))
        if not isinstance(values.get("mic_sources"), dict):
            values["mic_sources"] = {}
        else:
            values["mic_sources"] = {
                str(name): str(source)
                for name, source in values["mic_sources"].items()
                if str(name).strip()
                and str(source).strip()
                and not safety.is_loopback_source_name(source)
            }
        if not isinstance(values.get("client_latency_ms"), dict):
            values["client_latency_ms"] = {}
        else:
            latencies = {}
            for name, value in values["client_latency_ms"].items():
                if not str(name).strip():
                    continue
                try:
                    latencies[str(name)] = max(0, int(value))
                except (TypeError, ValueError):
                    continue
            values["client_latency_ms"] = latencies
        try:
            values["acoustic_interval_s"] = max(10, min(300, int(values.get("acoustic_interval_s", 30))))
            values["acoustic_volume"] = max(1, min(100, int(values.get("acoustic_volume", 20))))
        except (TypeError, ValueError):
            values["acoustic_interval_s"] = 30
            values["acoustic_volume"] = 20
        if not isinstance(values.get("acoustic_monitor"), bool):
            values["acoustic_monitor"] = False
        return cls(**values)

    @staticmethod
    def _validate_strict(raw: dict[str, Any]) -> None:
        """Reject unsafe persisted values instead of silently repairing them.

        The normal ``load`` path repairs old/partial settings for the GUI.  The
        CLI uses this strict view before host/join so a hostile value cannot be
        normalized, saved, and then silently used.
        """
        if "playback" in raw:
            try:
                safety.validate_playback(raw["playback"])
            except safety.RoutingError as exc:
                raise ConfigError(
                    f"invalid persisted playback {raw['playback']!r}: {exc}"
                ) from exc
        if "hw_sink" in raw:
            persisted_sink = raw.get("hw_sink")
            if not str(persisted_sink or "").strip():
                raise ConfigError(
                    "empty persisted hw_sink: refusing to fall back to a virtual output"
                )
            if safety.is_virtual_sink(persisted_sink):
                raise ConfigError(
                    f"unsafe persisted hw_sink {persisted_sink!r}: "
                    "virtual/loopback sinks cannot be used as a hardware output"
                )
        sources = raw.get("mic_sources")
        if isinstance(sources, dict):
            for name, source in sources.items():
                if safety.is_loopback_source_name(source):
                    raise ConfigError(
                        f"unsafe persisted microphone for {name!r}: "
                        f"{source!r} is a monitor/virtual loopback"
                    )

    def save(self) -> None:
        try:
            self.playback = safety.validate_playback(self.playback)
        except safety.RoutingError:
            self.playback = "synced"
        if safety.is_virtual_sink(self.hw_sink):
            self.hw_sink = ""
        write_atomic(config_file(), json.dumps(asdict(self), indent=2) + "\n")

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    # --- small helpers ----------------------------------------------
    def effective_buffer_ms(self) -> int:
        if self.preset == "deep":
            return 60_000
        if self.preset == "manual":
            return max(40, min(5_000, int(self.manual_buffer_ms)))
        return 250  # auto starts here; the controller converges downwards

    def learned_floor(self, host: str | None) -> int | None:
        if not host:
            return None
        return self.learned_floor_ms.get(host)


def write_atomic(path: Path, text: str) -> None:
    """Write via a temp file in the same directory, then rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_env_file(path: Path) -> dict[str, str]:
    """Parse the simple KEY=value files we hand to systemd."""
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def write_env_file(path: Path, values: dict[str, str]) -> None:
    body = ["# generated by audiosync; rewritten whenever settings change"]
    body += [f"{key}={value}" for key, value in values.items()]
    write_atomic(path, "\n".join(body) + "\n")


def prune_pairing_history(path: Path, device_name: str, ips: set[str] | None = None) -> int:
    """Drop this device's persisted Snapcast clients from ``server.json``.

    Snapserver keeps every client it has ever seen.  A reconnect after a
    name/MAC change leaves a ghost row (``#2``) that the UI keeps rendering,
    and stale ``connected`` flags mislead the host.  Every record whose
    ``host.name`` is this device - or whose ``host.ip`` is one of ``ips`` -
    is removed, connected or not: the caller only does this while this
    device's own client is stopped (or the server itself is stopped).

    Other devices and all groups are left untouched.  A malformed or missing
    file is a no-op; the original is copied to ``server.json.bak`` once.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    if not isinstance(raw, dict):
        return 0
    groups = raw.get("Groups")
    if not isinstance(groups, list):
        return 0

    target = (device_name or "").strip().casefold()
    addresses = {str(ip) for ip in (ips or set()) if ip}
    removed = 0
    for group in groups:
        if not isinstance(group, dict):
            continue
        clients = group.get("clients")
        if not isinstance(clients, list):
            continue
        kept = []
        for client in clients:
            if not isinstance(client, dict):
                kept.append(client)
                continue
            host = client.get("host") if isinstance(client.get("host"), dict) else {}
            name = str(host.get("name") or "").strip().casefold()
            ip = str(host.get("ip") or "")
            if (target and name == target) or (ip and ip in addresses):
                removed += 1
            else:
                kept.append(client)
        group["clients"] = kept

    if not removed:
        return 0
    try:
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    except OSError:
        pass
    write_atomic(path, json.dumps(raw, indent=2) + "\n")
    return removed
