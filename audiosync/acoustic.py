"""Acoustic probe generation, capture and onset detection.

The host plays a known low-level pulse train into ``audiosync_sink``.  Each
device records from a physical microphone and runs :func:`detect_probe` on the
captured PCM.  The module is standard-library only so the same file can be
streamed to a remote device over ``ssh python3 -``; it never imports the GUI or
the package's backend.

Only onset/confidence data leaves a device during normal operation.  Raw PCM is
held in memory for the duration of one capture and is never written to disk.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import wave
from array import array
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Iterable, Sequence

SAMPLE_RATE = 48_000
DETECT_RATE = 8_000
DECIMATE = SAMPLE_RATE // DETECT_RATE
PULSE_COUNT = 3
PULSE_MS = 50
PULSE_GAP_MS = 200
#: The host is allowed to wake an idle Snapcast stream before the first pulse.
#: The floor below keeps the sink monitor flowing during this lead-in.
PROBE_LEAD_MS = 1000
PROBE_TAIL_MS = 120
PROBE_F0 = 1_200.0
PROBE_F1 = 3_600.0
PROBE_AMPLITUDE = 0.22
CAPTURE_DURATION_S = 4.5
#: Give SSH and the remote Python process time to start before the probe.
CAPTURE_LEAD_S = 1.50
MIN_CONFIDENCE = 0.18
MAX_GAP_ERROR_MS = 65.0
DETECT_IGNORE_S = 0.20


class SourceSafetyError(RuntimeError):
    """A requested capture source is a loopback or cannot be verified."""


@dataclass(frozen=True)
class SourceInfo:
    """The parts of one ``pactl list sources`` section we must inspect.

    ``metadata`` is false for the short name-only format.  Production capture
    paths require metadata so a virtual source whose name does not end in
    ``.monitor`` cannot slip through.
    """

    name: str
    monitor_of: str = ""
    virtual: bool = False
    device_class: str = ""
    metadata: bool = False


@dataclass
class ProbeDetection:
    """What one microphone recording found."""

    onsets_s: list[float]
    scores: list[float]
    confidence: float
    noise: float
    skipped: bool
    error: str = ""

    @property
    def valid(self) -> bool:
        return not self.error and not self.skipped and len(self.onsets_s) == PULSE_COUNT


def pulse_onsets_s() -> list[float]:
    """Expected pulse starts relative to the beginning of the probe WAV."""
    step_ms = PULSE_MS + PULSE_GAP_MS
    return [(PROBE_LEAD_MS + index * step_ms) / 1000.0 for index in range(PULSE_COUNT)]


def probe_duration_s() -> float:
    last = pulse_onsets_s()[-1] + PULSE_MS / 1000.0
    return last + PROBE_TAIL_MS / 1000.0


def _chirp(rate: int, duration_s: float, amplitude: float, start_phase: float = 0.0) -> tuple[list[float], float]:
    """A Hann-windowed linear chirp, plus the phase at its end."""
    count = max(1, int(round(rate * duration_s)))
    samples: list[float] = []
    phase = start_phase
    for index in range(count):
        fraction = (index + 0.5) / count
        frequency = PROBE_F0 + (PROBE_F1 - PROBE_F0) * fraction
        phase += 2.0 * math.pi * frequency / rate
        envelope = math.sin(math.pi * fraction) ** 2
        samples.append(amplitude * envelope * math.sin(phase))
    return samples, phase


def _probe_at_rate(rate: int, volume: int = 20) -> list[float]:
    """Generate the probe at an arbitrary rate for playback or matching."""
    amplitude = PROBE_AMPLITUDE * max(0, min(100, int(volume))) / 100.0
    total_s = probe_duration_s()
    # Keep the sink monitor producing non-zero frames between pulses.  Some
    # PipeWire/Pulse paths suppress an all-zero gap; Snapserver then switches
    # to idle and chops the very probe we are trying to measure.  The floor is
    # one LSB and inaudible.
    floor = 1.0 / 32767.0
    samples = [floor if index % 2 == 0 else -floor for index in range(max(1, int(round(total_s * rate))))]
    for onset_s in pulse_onsets_s():
        # Reset phase at each Hann-windowed pulse so the matched template is
        # identical to the generated test signal and correlation stays sharp.
        pulse, _phase = _chirp(rate, PULSE_MS / 1000.0, amplitude)
        start = int(round(onset_s * rate))
        for index, value in enumerate(pulse):
            if start + index < len(samples):
                samples[start + index] += value
    return samples


def probe_samples(volume: int = 20) -> array:
    """Return 48 kHz mono signed-16-bit samples for the test probe."""
    values = _probe_at_rate(SAMPLE_RATE, volume)
    pcm = array("h")
    for value in values:
        pcm.append(max(-32768, min(32767, int(round(value * 32767)))))
    return pcm


def write_probe_wav(path: Path | str, volume: int = 20) -> Path:
    """Write a temporary mono WAV suitable for paplay/pw-play."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(target), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(probe_samples(volume).tobytes())
    return target


def play_probe(volume: int = 20, sink: str = "audiosync_sink", timeout: float = 8.0) -> None:
    """Play the probe into the Audiosync sink on this machine.

    Both paplay and pw-play are accepted because installations use either
    PulseAudio compatibility or native PipeWire tools.
    """
    if shutil.which("paplay") is None and shutil.which("pw-play") is None:
        raise RuntimeError("neither paplay nor pw-play is installed on the host")
    fd, raw_path = tempfile.mkstemp(prefix="audiosync-probe-", suffix=".wav")
    os.close(fd)
    path = Path(raw_path)
    try:
        write_probe_wav(path, volume)
        if shutil.which("paplay"):
            command = ["paplay", f"--device={sink}", str(path)]
        else:
            command = ["pw-play", f"--target={sink}", str(path)]
        proc = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()
            raise RuntimeError(detail or f"probe playback failed ({proc.returncode})")
    finally:
        try:
            path.unlink()
        except OSError:
            pass


def _as_samples(samples: bytes | bytearray | Sequence[int] | array) -> array:
    if isinstance(samples, bytes | bytearray):
        pcm = array("h")
        usable = len(samples) - (len(samples) % 2)
        pcm.frombytes(bytes(samples[:usable]))
        return pcm
    return array("h", samples)


def _decimate(samples: array) -> list[float]:
    source = _as_samples(samples)
    result: list[float] = []
    for start in range(0, len(source) - DECIMATE + 1, DECIMATE):
        block = source[start : start + DECIMATE]
        result.append(sum(block) / (DECIMATE * 32768.0))
    if len(result) < 2:
        return []
    mean = sum(result) / len(result)
    return [value - mean for value in result]


def _template() -> list[float]:
    values, _phase = _chirp(DETECT_RATE, PULSE_MS / 1000.0, PROBE_AMPLITUDE)
    mean = sum(values) / max(1, len(values))
    values = [value - mean for value in values]
    norm = math.sqrt(sum(value * value for value in values))
    return [value / norm for value in values] if norm else values


def _local_maxima(values: Sequence[float], threshold: float) -> list[int]:
    peaks: list[int] = []
    for index in range(1, len(values) - 1):
        value = values[index]
        if value < threshold:
            continue
        if value >= values[index - 1] and value > values[index + 1]:
            if peaks and index - peaks[-1] < int(DETECT_RATE * 0.12):
                if value > values[peaks[-1]]:
                    peaks[-1] = index
                continue
            peaks.append(index)
    return peaks


def detect_probe(samples: bytes | bytearray | Sequence[int] | array) -> ProbeDetection:
    """Find the three probe pulses in one mono 48 kHz recording.

    The detector decimates to 8 kHz, applies a normalised matched filter, and
    merges nearby peaks.  Correlation rather than absolute loudness makes it
    tolerant of room gain and codec/level differences; the noise floor and
    confidence guard against silence and unrelated music.
    """
    source = _as_samples(samples)
    if len(source) < SAMPLE_RATE * 0.35:
        return ProbeDetection([], [], 0.0, 0.0, True, "microphone capture was empty")
    signal = _decimate(source)
    template = _template()
    if len(signal) < len(template) + 2:
        return ProbeDetection([], [], 0.0, 0.0, True, "microphone capture was too short")

    # Sliding energy makes the normalised correlation cheap in pure Python.
    prefix = [0.0]
    for value in signal:
        prefix.append(prefix[-1] + value * value)
    scores: list[float] = []
    for start in range(0, len(signal) - len(template) + 1):
        end = start + len(template)
        energy = prefix[end] - prefix[start]
        if energy <= 1e-12:
            scores.append(0.0)
            continue
        dot = 0.0
        for offset, expected in enumerate(template):
            dot += signal[start + offset] * expected
        scores.append(dot / math.sqrt(energy))

    if not scores:
        return ProbeDetection([], [], 0.0, 0.0, True, "no correlation window")
    for index in range(min(len(scores), int(DETECT_RATE * DETECT_IGNORE_S))):
        scores[index] = 0.0
    ordered = sorted(scores)
    noise = ordered[len(ordered) // 2]
    best = max(scores)
    threshold = max(MIN_CONFIDENCE, best * 0.34)
    peaks = _local_maxima(scores, threshold)
    # A real pulse should be at least a little above the median correlation.
    if best < MIN_CONFIDENCE or not peaks:
        return ProbeDetection([], [], best, noise, True, "probe pulses were not audible")

    all_onsets = [peak / DETECT_RATE for peak in peaks]
    all_scores = [scores[peak] for peak in peaks]
    expected = (PULSE_MS + PULSE_GAP_MS) / 1000.0
    # Pick a triplet with the right spacing instead of blindly taking the
    # first three peaks; capture-start clicks and late room echoes otherwise
    # steal a slot from a real pulse.
    best_triplet: tuple[float, int] | None = None
    for index in range(0, len(all_onsets) - PULSE_COUNT + 1):
        triplet = all_onsets[index : index + PULSE_COUNT]
        gaps = [b - a for a, b in zip(triplet, triplet[1:])]
        if any(abs(gap - expected) > MAX_GAP_ERROR_MS / 1000.0 for gap in gaps):
            continue
        strength = sum(all_scores[index : index + PULSE_COUNT])
        if best_triplet is None or strength > best_triplet[0]:
            best_triplet = (strength, index)
    skipped = best_triplet is None
    if best_triplet is None:
        onsets = all_onsets
        peak_scores = all_scores
        error = "fewer than three probe pulses were detected" if len(onsets) < PULSE_COUNT else "probe pulse spacing was irregular"
    else:
        index = best_triplet[1]
        onsets = all_onsets[index : index + PULSE_COUNT]
        peak_scores = all_scores[index : index + PULSE_COUNT]
        error = ""
    return ProbeDetection(onsets, peak_scores, best, noise, skipped, error)


def capture_raw(source: str, duration_s: float = CAPTURE_DURATION_S, recorder: str | None = None) -> bytes:
    """Capture mono 48 kHz PCM from a physical source into memory."""
    if not source:
        raise RuntimeError("no microphone source selected")
    info = source_info(source)
    reason = unsafe_source_reason(source, info, require_metadata=True)
    if reason:
        raise SourceSafetyError(reason)
    recorder = (recorder or "").strip().lower()
    if not recorder:
        recorder = "parec" if shutil.which("parec") else "pw-record"
    if recorder == "parec":
        command = [
            "parec",
            "--raw",
            "--format=s16le",
            f"--rate={SAMPLE_RATE}",
            "--channels=1",
            f"--device={source}",
            "--latency-msec=20",
        ]
    elif recorder == "pw-record":
        command = [
            "pw-record",
            "--raw",
            "--format=s16",
            f"--rate={SAMPLE_RATE}",
            "--channels=1",
            f"--target={source}",
            "-",
        ]
    else:
        raise RuntimeError(f"unsupported capture recorder: {recorder}")
    try:
        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError as exc:
        raise RuntimeError(f"{recorder} is not installed on this device") from exc
    chunks: list[bytes] = []

    def drain() -> None:
        # A raw capture can exceed the OS pipe buffer.  Drain continuously or
        # the recorder blocks and the later probe never reaches the detector.
        assert proc.stdout is not None
        while True:
            chunk = proc.stdout.read(8192)
            if not chunk:
                break
            chunks.append(chunk)

    reader = threading.Thread(target=drain, name="audiosync-acoustic-reader", daemon=True)
    reader.start()
    try:
        time.sleep(max(0.1, duration_s))
        if proc.poll() is None:
            proc.terminate()
        reader.join(timeout=2.0)
        if reader.is_alive():
            proc.kill()
            reader.join(timeout=1.0)
        stderr = proc.stderr.read() if proc.stderr is not None else b""
    except Exception:
        proc.kill()
        stderr = b""
    stdout = b"".join(chunks)
    if proc.returncode not in (0, -15) and not stdout:
        detail = (stderr or b"").decode("utf-8", "replace").strip()
        raise RuntimeError(detail or f"{recorder} exited with {proc.returncode}")
    return (stdout or b"")[: int(SAMPLE_RATE * duration_s) * 2]


def capture_and_detect(source: str, duration_s: float = CAPTURE_DURATION_S, recorder: str | None = None) -> dict:
    """Capture one microphone window and return the JSON-safe measurement."""
    started_ns = time.time_ns()
    try:
        raw = capture_raw(source, duration_s, recorder)
    except Exception as exc:  # noqa: BLE001 - remote agent reports text, never crashes
        return {
            "ok": False,
            "started_ns": started_ns,
            "source": source,
            "onsets": [],
            "scores": [],
            "confidence": 0.0,
            "noise": 0.0,
            "skipped": True,
            "capture_error": True,
            "error": str(exc),
        }
    detection = detect_probe(raw)
    return {
        "ok": detection.valid,
        "started_ns": started_ns,
        "source": source,
        "onsets": detection.onsets_s,
        "scores": detection.scores,
        "confidence": detection.confidence,
        "noise": detection.noise,
        "skipped": detection.skipped,
        "error": detection.error,
    }


def parse_sources(text: str) -> list[SourceInfo]:
    """Parse either ``pactl list sources`` or ``pactl list short sources``.

    The verbose form is required for safety because a source can be a loopback
    without having a ``.monitor`` suffix.  The short form is kept for callers
    that only need a name list (and for tests); capture paths never treat it as
    proof that a source is physical.
    """
    infos: list[SourceInfo] = []
    current: dict[str, object] | None = None

    def flush() -> None:
        nonlocal current
        if current is not None and str(current.get("name") or "").strip():
            infos.append(
                SourceInfo(
                    name=str(current["name"]).strip(),
                    monitor_of=str(current.get("monitor_of") or "").strip(),
                    virtual=bool(current.get("virtual")),
                    device_class=str(current.get("device_class") or "").strip(),
                    metadata=bool(current.get("metadata")),
                )
            )
        current = None

    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("Source #"):
            flush()
            current = {"name": "", "monitor_of": "", "virtual": False, "device_class": "", "metadata": True}
            continue
        if current is not None:
            if line.startswith("Name:"):
                current["name"] = line.split(":", 1)[1].strip()
                continue
            if line.startswith("Monitor of Sink:"):
                current["monitor_of"] = line.split(":", 1)[1].strip()
                continue
            match = re.match(r'device\.class\s*=\s*"([^"]*)"', line)
            if match:
                current["device_class"] = match.group(1)
                continue
            match = re.match(r'node\.virtual\s*=\s*"([^"]*)"', line)
            if match:
                current["virtual"] = match.group(1).strip().casefold() in ("true", "yes", "1")
                continue
            continue
        if not line:
            continue
        fields = line.split("\t") if "\t" in line else line.split()
        if len(fields) >= 2 and fields[1].strip():
            infos.append(SourceInfo(fields[1].strip()))
    flush()
    return infos


def unsafe_source_reason(
    name: object,
    info: SourceInfo | None = None,
    require_metadata: bool = False,
) -> str | None:
    """Why ``name`` cannot be used as an acoustic microphone, or ``None``.

    Name checks are intentionally conservative.  When ``require_metadata`` is
    set, a short-name-only entry is rejected because a virtual source could be
    hiding behind an innocuous name.
    """
    text = str(name or "").strip()
    if not text:
        return "no microphone source selected"
    lowered = text.casefold()
    if lowered.endswith(".monitor"):
        return f"{text!r} is a monitor/loopback source; it cannot measure the acoustic path"
    if lowered == "audiosync_sink" or lowered.startswith("audiosync_sink."):
        return f"{text!r} is the Audiosync virtual sink; it would feed capture back into itself"
    if lowered == "auto_null" or lowered.startswith("auto_null."):
        return f"{text!r} is a null/virtual source, not a physical microphone"
    if info is None:
        if require_metadata:
            return f"cannot verify that {text!r} is a physical microphone; pactl source metadata is unavailable"
        return None
    if info.metadata and not info.name:
        return f"cannot verify that {text!r} is a physical microphone; source metadata has no name"
    if info.name and info.name != text:
        return f"source metadata belongs to {info.name!r}, not {text!r}"
    if info.monitor_of.strip().casefold() not in ("", "n/a", "none"):
        return f"{text!r} is a monitor of {info.monitor_of!r}; it would create a digital feedback loop"
    if info.device_class.strip().casefold() == "monitor":
        return f"{text!r} is classified as a monitor/loopback source"
    if info.virtual:
        return f"{text!r} is a virtual source, not a physical microphone"
    if require_metadata and not info.metadata:
        return f"cannot verify that {text!r} is a physical microphone; source metadata is incomplete"
    return None


def safe_capture_source(
    name: object,
    info: SourceInfo | None = None,
    require_metadata: bool = True,
) -> str:
    """Return a safe physical source name or raise :class:`SourceSafetyError`."""
    text = str(name or "").strip()
    reason = unsafe_source_reason(text, info, require_metadata=require_metadata)
    if reason:
        raise SourceSafetyError(reason)
    return text


def source_info(name: str) -> SourceInfo | None:
    """Inspect one source using full ``pactl`` metadata, or fail closed."""
    try:
        proc = subprocess.run(
            ["pactl", "list", "sources"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    for info in parse_sources(proc.stdout):
        if info.name == name:
            return info
    return None


def physical_sources(text: str) -> list[str]:
    """Parse source listing text and reject loopback/virtual sources."""
    names: list[str] = []
    for info in parse_sources(text):
        if unsafe_source_reason(info.name, info, require_metadata=False) is not None:
            continue
        if info.name not in names:
            names.append(info.name)
    return names


def measure_arrivals(results: dict[str, dict]) -> dict:
    """Turn per-device detections into non-negative offsets in milliseconds.

    The earliest valid acoustic arrival is the reference.  Devices that missed
    pulses are reported separately and are not given a latency correction.
    """
    measurements: list[dict] = []
    skipped: list[str] = []
    errors: list[str] = []
    arrivals: dict[str, tuple[float, dict]] = {}
    for name, result in results.items():
        if not result.get("ok") or result.get("skipped"):
            reason = str(result.get("error") or "no probe answer")
            if result.get("skipped"):
                skipped.append(name)
            errors.append(f"{name}: {reason}")
            measurements.append({"name": name, **result, "offset_ms": None})
            continue
        onsets = result.get("onsets") or []
        started_ns = int(result.get("started_ns") or 0)
        if len(onsets) < PULSE_COUNT or started_ns <= 0:
            skipped.append(name)
            errors.append(f"{name}: incomplete probe")
            measurements.append({"name": name, **result, "offset_ms": None})
            continue
        # Use every pulse after removing its nominal position.  A single
        # early/late correlation peak should not move the whole offset.
        started = started_ns / 1e9
        expected_gap = (PULSE_MS + PULSE_GAP_MS) / 1000.0
        arrival = median(started + float(onset) - index * expected_gap for index, onset in enumerate(onsets))
        arrivals[name] = (arrival, result)
        measurements.append({"name": name, **result, "offset_ms": None})

    offsets: dict[str, int] = {}
    reference: str | None = None
    if arrivals:
        reference = min(arrivals, key=lambda name: arrivals[name][0])
        zero = arrivals[reference][0]
        for name, (arrival, _result) in arrivals.items():
            offsets[name] = max(0, int(round((arrival - zero) * 1000.0)))
    for item in measurements:
        if item["name"] in offsets:
            item["offset_ms"] = offsets[item["name"]]
    return {
        "measurements": measurements,
        "offsets_ms": offsets,
        "reference": reference,
        "skipped": skipped,
        "errors": errors,
    }


def _agent(argv: Iterable[str]) -> int:
    parser = argparse.ArgumentParser(prog="audiosync-acoustic-agent")
    parser.add_argument("source")
    parser.add_argument("duration", type=float, nargs="?", default=CAPTURE_DURATION_S)
    parser.add_argument("--recorder", choices=("parec", "pw-record"))
    args = parser.parse_args(list(argv))
    result = capture_and_detect(args.source, args.duration, args.recorder)
    print(json.dumps(result), flush=True)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":  # pragma: no cover - exercised through SSH
    raise SystemExit(_agent(sys.argv[1:]))