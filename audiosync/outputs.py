"""Hardware output discovery.

PipeWire advertises HDMI/DisplayPort sinks even when the monitor has no
speakers, and the installer used to pair the first ALSA card with the first
real sink - on this desktop that mixed the NVidia HDMI card with the Ryzen
analog output.  Everything here is pure (text in, decision out) so the
installer, the GUI and the tests share one policy:

1. analog / built-in speakers,
2. any other non-HDMI output (USB, Bluetooth),
3. HDMI only when nothing else exists, with a warning.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import safety

HDMI_MARKERS = ("hdmi", "displayport")
BUILTIN_MARKERS = ("analog-stereo", "analog", "speaker", "built-in", "builtin")
REAL_PREFIXES = ("alsa_output", "bluez_output")


@dataclass(frozen=True)
class Sink:
    """One entry from ``pactl list sinks``."""

    name: str
    description: str = ""
    card_index: int | None = None
    device: int | None = None
    card_name: str = ""
    virtual: bool = False


@dataclass(frozen=True)
class Choice:
    """The sink we will play through, plus its ALSA identity."""

    sink: str = ""
    card: str = ""
    device: int | None = None
    card_name: str = ""
    reason: str = ""
    warning: str = ""


def parse_pactl_sinks(text: str) -> list[Sink]:
    """Parse the verbose ``pactl list sinks`` output.

    Only the fields we rank on are kept: name, description and the ALSA
    card/device properties.  Unknown sections are ignored so a PipeWire
    upgrade adding properties cannot break detection.
    """
    sinks: list[Sink] = []
    fields: dict[str, object] | None = None

    def flush() -> None:
        nonlocal fields
        if fields and fields.get("name"):
            sinks.append(
                Sink(
                    name=str(fields["name"]),
                    description=str(fields.get("description") or ""),
                    card_index=fields.get("card_index"),  # type: ignore[arg-type]
                    device=fields.get("device"),  # type: ignore[arg-type]
                    card_name=str(fields.get("card_name") or ""),
                    virtual=bool(fields.get("virtual")),
                )
            )
        fields = None

    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("Sink #"):
            flush()
            fields = {
                "name": "",
                "description": "",
                "card_index": None,
                "device": None,
                "card_name": "",
                "virtual": False,
            }
            continue
        if fields is None:
            continue
        if line.startswith("Name:"):
            fields["name"] = line.split(":", 1)[1].strip()
            continue
        if line.startswith("Description:"):
            fields["description"] = line.split(":", 1)[1].strip()
            continue
        match = re.match(r'alsa\.card\s*=\s*"([^"]+)"', line)
        if match:
            try:
                fields["card_index"] = int(match.group(1))
            except ValueError:
                pass
            continue
        match = re.match(r'alsa\.device\s*=\s*"([^"]+)"', line)
        if match:
            try:
                fields["device"] = int(match.group(1))
            except ValueError:
                pass
            continue
        match = re.match(r'alsa\.card_name\s*=\s*"([^"]+)"', line)
        if match:
            fields["card_name"] = match.group(1)
            continue
        match = re.match(r'node\.virtual\s*=\s*"([^"]*)"', line)
        if match:
            fields["virtual"] = match.group(1).strip().casefold() in ("true", "yes", "1")
            continue
        match = re.match(r'device\.class\s*=\s*"([^"]*)"', line)
        if match and match.group(1).strip().casefold() == "monitor":
            fields["virtual"] = True
    flush()
    return sinks


def hardware_sink_names(text: str) -> set[str]:
    """Names of current sinks that are not known virtual/loopback routes."""
    return {
        sink.name
        for sink in parse_pactl_sinks(text)
        if not sink.virtual and not safety.is_virtual_sink(sink.name)
    }


def parse_asound_cards(text: str) -> dict[int, str]:
    """Map ALSA card index to its stable id from ``/proc/asound/cards``.

    ``hw:`` device strings use the id in brackets (``Generic``,
    ``sofhdadsp``), never ``alsa.card_name`` (``HD-Audio Generic``).
    """
    cards: dict[int, str] = {}
    for line in text.splitlines():
        match = re.match(r"^\s*(\d+)\s+\[([^\]]+)\]\s*:", line)
        if match:
            cards[int(match.group(1))] = match.group(2).strip()
    return cards


def _haystack(sink: Sink) -> str:
    return f"{sink.name} {sink.description}".lower()


def is_hdmi(sink: Sink) -> bool:
    hay = _haystack(sink)
    return any(marker in hay for marker in HDMI_MARKERS)


def is_builtin(sink: Sink) -> bool:
    hay = _haystack(sink)
    return any(marker in hay for marker in BUILTIN_MARKERS)


def _score(sink: Sink) -> int:
    if is_hdmi(sink):
        return 10
    if is_builtin(sink):
        return 100
    if sink.name.startswith("bluez_output"):
        return 60
    return 50


def choose_output(sinks: list[Sink], cards: dict[int, str] | None = None) -> Choice:
    """Pick the speakers, never a speakerless HDMI monitor."""
    candidates = [
        sink
        for sink in sinks
        if (
            sink.name.startswith(REAL_PREFIXES)
            and not sink.virtual
            and not safety.is_virtual_sink(sink.name)
        )
    ]
    if not candidates:
        return Choice(reason="no hardware output found")

    best = candidates[0]
    for sink in candidates[1:]:
        if _score(sink) > _score(best):
            best = sink

    card = ""
    if best.card_index is not None and cards:
        card = cards.get(best.card_index, "")

    warning = ""
    if is_hdmi(best):
        reason = "HDMI (no speakers detected)"
        warning = "only HDMI outputs found - no speakers detected"
    elif is_builtin(best):
        reason = "built-in speakers" if "speaker" in _haystack(best) else "analog output"
    elif best.name.startswith("bluez_output"):
        reason = "Bluetooth output"
    else:
        reason = "external output"

    return Choice(
        sink=best.name,
        card=card,
        device=best.device,
        card_name=best.card_name,
        reason=reason,
        warning=warning,
    )


def output_device(choice: Choice) -> str:
    """``hw:<card-id>,<device>`` for the ALSA fallback, or empty."""
    if choice.card and choice.device is not None:
        return f"hw:{choice.card},{choice.device}"
    return ""


def label(choice: Choice) -> str:
    """Short human-readable description for the status line."""
    if not choice.sink:
        return choice.reason or "no output"
    name = choice.card_name or choice.card or choice.sink
    return f"{name} ({choice.reason})" if choice.reason else name
