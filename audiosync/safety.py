"""Fail-closed audio routing policy.

Every user-facing route passes through this module or the equivalent source
checks in :mod:`audiosync.acoustic` (the acoustic agent is streamed to remote
devices without the rest of the package).  The policy deliberately refuses when
it cannot prove a route is safe instead of guessing.

A feedback loop here means any path where captured output can re-enter
capture: a monitor/virtual source used as a microphone, Snapcast output sent
back into ``audiosync_sink`` while it is being captured, or Direct capture
pointing at the virtual sink.
"""

from __future__ import annotations

from typing import Iterable, Protocol

from . import acoustic

SINK_NAME = "audiosync_sink"
VIRTUAL_SINK_MARKERS = (
    "audiosync",
    "auto_null",
    "virtual",
    "loopback",
    "monitor",
    "null",
    "easyeffects",
    "remap",
    "filter",
)
PLAYBACK_MODES = ("synced", "direct", "off")


class RoutingError(ValueError):
    """A routing option would create a feedback loop or cannot be verified."""


class TargetLike(Protocol):
    name: str
    host: str | None


SourceInfo = acoustic.SourceInfo


def _clean(value: object) -> str:
    return str(value or "").strip()


def is_virtual_sink(name: object) -> bool:
    """True for the virtual capture sink and recognisable virtual sinks.

    PipeWire sink names are not proof that a sink is physical: a custom null
    or loopback sink can be named with the ``alsa_output`` prefix.  Reject
    conservative virtual/loopback markers as well as the well-known names.
    """
    text = _clean(name).casefold()
    return bool(text) and any(marker in text for marker in VIRTUAL_SINK_MARKERS)


def validate_playback(mode: object) -> str:
    """Return a supported playback mode or fail closed."""
    text = _clean(mode).casefold()
    if text not in PLAYBACK_MODES:
        raise RoutingError(
            f"playback mode must be one of {', '.join(PLAYBACK_MODES)}; got {mode!r}"
        )
    return text


def safe_client_sink(name: object, allow_alsa_device: bool = True) -> str:
    """Return an explicit real output sink for snapclient.

    An empty value is unsafe because snapclient would use the current default
    sink.  During hosting that default is ``audiosync_sink``, whose monitor is
    captured and streamed back to every client.

    ``allow_alsa_device`` defaults true because an ALSA ``hw:`` device is a
    real hardware output; PipeWire-only paths pass false explicitly.
    """
    text = _clean(name)
    if not text:
        raise RoutingError(
            "no hardware output is configured; refusing to let snapclient use "
            f"the virtual {SINK_NAME} sink"
        )
    lowered = text.casefold()
    if is_virtual_sink(text):
        raise RoutingError(f"{text!r} is a virtual/loopback sink, not a speaker output")
    if lowered.startswith("hw:"):
        if allow_alsa_device:
            return text
        raise RoutingError(
            f"{text!r} is an ALSA device, not a PipeWire sink; refusing to use it as a default sink"
        )
    if not lowered.startswith(("alsa_output", "bluez_output")):
        raise RoutingError(
            f"{text!r} is not a verified hardware output; refusing to route audio through it"
        )
    return text


def safe_direct_capture_sink(name: object) -> str:
    """Return a real hardware sink for Direct capture.

    Direct mode captures a hardware monitor.  Capturing the virtual sink in
    this mode is either wrong or a self-capture loop.
    """
    text = _clean(name)
    if not text:
        raise RoutingError(
            "Direct playback needs a detected hardware output; refusing to capture "
            f"the virtual {SINK_NAME} sink"
        )
    lowered = text.casefold()
    if is_virtual_sink(text):
        raise RoutingError(
            f"Direct playback cannot capture the virtual/loopback sink {text!r}"
        )
    if not lowered.startswith(("alsa_output", "bluez_output")):
        raise RoutingError(
            f"Direct playback cannot capture {text!r}; it is not a verified hardware sink"
        )
    return text


def safe_capture_source(
    name: object,
    info: SourceInfo | None = None,
    require_metadata: bool = True,
) -> str:
    """Return a physical microphone source, or raise.

    ``require_metadata`` is true for production capture paths.  A source may be
    accepted by name only where the caller has already obtained and filtered
    the full ``pactl list sources`` document (for example a GUI dialog).
    """
    try:
        return acoustic.safe_capture_source(name, info, require_metadata=require_metadata)
    except acoustic.SourceSafetyError as exc:
        raise RoutingError(str(exc)) from exc


def is_loopback_source_name(name: object) -> bool:
    """Conservative name-only check used while loading persisted settings."""
    return acoustic.unsafe_source_reason(name, require_metadata=False) is not None


def unique_targets(
    targets: Iterable[TargetLike],
    local_names: Iterable[str] | None = None,
) -> list[TargetLike]:
    """Reject duplicate local/SSH views of the same device.

    A duplicate target would capture one microphone twice and make the
    earliest-arrival reference ambiguous.  Names are always unique; hosts are
    matched case-insensitively after normalising addresses that name this
    machine to the local marker.
    """
    local = {
        _clean(item).rstrip(".").casefold()
        for item in (local_names or ())
        if _clean(item)
    }
    result: list[TargetLike] = []
    seen_names: set[str] = set()
    seen_hosts: set[str] = set()
    seen_local = False
    for target in targets:
        name = _clean(getattr(target, "name", ""))
        if not name:
            raise RoutingError("an audio target has no device name")
        name_key = name.casefold()
        if name_key in seen_names:
            raise RoutingError(f"duplicate audio target name {name!r}")
        host = _clean(getattr(target, "host", "")).rstrip(".")
        host_key = host.casefold()
        if host_key and host_key in local:
            host_key = ""
        is_local = not host_key
        unknown_host = host_key in ("?", "unknown", "n/a")
        if is_local and seen_local:
            raise RoutingError("more than one local target would be captured")
        if is_local:
            seen_local = True
        if host_key and not unknown_host and host_key in seen_hosts:
            raise RoutingError(f"two targets point at the same SSH host {host!r}")
        seen_names.add(name_key)
        if host_key and not unknown_host:
            seen_hosts.add(host_key)
        result.append(target)
    return result
