"""Codec choice and a cheap link probe.

PCM is the default because it has *zero* encoder latency, which leaves the
buffer as the only thing between a sample and your ear.  When the link
looks crowded we spend bitrate to buy buffering headroom instead.
"""

from __future__ import annotations

import socket
import statistics
import time
from dataclasses import dataclass

#: Bits per second needed for 48 kHz / 16-bit / stereo.
CODEC_BITRATE = {
    "pcm": 1_536_000,
    "flac": 700_000,
    "opus": 128_000,
}
CODEC_ORDER = ("pcm", "flac", "opus")


@dataclass
class LinkProbe:
    ok: bool
    rtt_ms: float | None = None
    loss: float = 0.0
    samples: int = 0

    def describe(self) -> str:
        if not self.ok:
            return "host unreachable"
        rtt = f"{self.rtt_ms:.0f} ms" if self.rtt_ms is not None else "?"
        return f"{rtt} RTT, {self.loss:.0%} loss"


def probe_link(host: str, port: int = 1704, samples: int = 3, timeout: float = 0.6) -> LinkProbe:
    """TCP connect RTT as a proxy for how jittery this link is.

    We deliberately do not try to measure throughput: a burst test on a
    Wi-Fi link that is also carrying the audio would perturb the very thing
    we are trying to measure.
    """
    times: list[float] = []
    failures = 0
    for _ in range(max(1, samples)):
        started = time.monotonic()
        try:
            with socket.create_connection((host, port), timeout=timeout):
                times.append((time.monotonic() - started) * 1000.0)
        except OSError:
            failures += 1
    if not times:
        return LinkProbe(ok=False, loss=1.0, samples=samples)
    return LinkProbe(
        ok=True,
        rtt_ms=statistics.median(times),
        loss=failures / max(1, samples),
        samples=samples,
    )


def choose_codec(probe: LinkProbe | None, preferred: str = "pcm") -> tuple[str, str]:
    """Pick the cheapest-to-decode codec the link can plausibly sustain.

    Returns ``(codec, reason)``; the reason is surfaced in the UI so a
    downgrade is never silent.
    """
    if probe is None or not probe.ok:
        return preferred, "no probe"
    if probe.loss >= 0.34 or (probe.rtt_ms or 0) >= 60:
        return "opus", f"congested link ({probe.describe()})"
    if probe.loss > 0 or (probe.rtt_ms or 0) >= 20:
        return "flac", f"jittery link ({probe.describe()})"
    return preferred, f"clean link ({probe.describe()})"


def buffer_floor_for(codec: str) -> int:
    """Smallest buffer worth attempting for a codec, in ms."""
    return {"pcm": 100, "flac": 120, "opus": 150}.get(codec, 100)
