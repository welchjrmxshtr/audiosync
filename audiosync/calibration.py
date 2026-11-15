"""Acoustic calibration orchestration.

The host starts a microphone capture on every device, waits for them to come
up, plays one probe into ``audiosync_sink``, then collects JSON measurements.
Remote devices receive :mod:`audiosync.acoustic` over SSH's stdin, so there is
no daemon or second package install to manage and no audio file transfer.
"""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from . import acoustic, safety
from .preload import SSH_OPTIONS, Target, is_local_host, local_host_names

SOURCE_LIST_TIMEOUT_S = 10.0
CAPTURE_SSH_TIMEOUT_S = 15.0
PROBE_LEAD_S = acoustic.CAPTURE_LEAD_S


def _run(command: list[str], timeout: float = SOURCE_LIST_TIMEOUT_S) -> subprocess.CompletedProcess:
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout)


def _ssh_command(target: Target, command: str) -> list[str]:
    assert target.host
    return ["ssh", *SSH_OPTIONS, target.host, command]


def list_physical_sources(target: Target) -> list[str]:
    """Return physical input names for one device, never monitor sources."""
    try:
        if target.local or is_local_host(target.host):
            proc = _run(["pactl", "list", "sources"])
        else:
            proc = _run(_ssh_command(target, "pactl list sources"))
    except (OSError, subprocess.TimeoutExpired):
        return []
    if getattr(proc, "returncode", 0) not in (0, None):
        return []
    return acoustic.physical_sources(proc.stdout)


def default_source(target: Target) -> str | None:
    """Return the device's current default input when it is physical."""
    try:
        if target.local or is_local_host(target.host):
            proc = _run(["pactl", "get-default-source"])
        else:
            proc = _run(_ssh_command(target, "pactl get-default-source"))
    except (OSError, subprocess.TimeoutExpired):
        return None
    if getattr(proc, "returncode", 0) not in (0, None):
        return None
    name = proc.stdout.strip().splitlines()[0].strip() if proc.stdout.strip() else ""
    if not name or acoustic.unsafe_source_reason(name, require_metadata=False) is not None:
        return None
    return name


def clock_synchronized(target: Target) -> bool:
    """Check the NTP state required for cross-device acoustic timestamps."""
    try:
        if target.local or is_local_host(target.host):
            proc = _run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"])
        else:
            proc = _run(_ssh_command(target, "timedatectl show -p NTPSynchronized --value"))
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.stdout.strip().lower() in ("yes", "true", "1")


def source_muted(target: Target, source: str) -> bool:
    """True when the selected physical microphone is muted at the source."""
    try:
        if target.local or is_local_host(target.host):
            proc = _run(["pactl", "get-source-mute", source])
        else:
            proc = _run(_ssh_command(target, f"pactl get-source-mute {shlex.quote(source)}"))
    except (OSError, subprocess.TimeoutExpired):
        return False
    return "yes" in proc.stdout.lower()


def choose_source(target: Target, configured: str | None, available: list[str] | None = None) -> str | None:
    """Prefer a persisted source, then the system default, then the first input."""
    names = list(available) if available is not None else list_physical_sources(target)
    names = [
        name
        for name in names
        if acoustic.unsafe_source_reason(name, require_metadata=False) is None
    ]
    if configured in names:
        return configured
    fallback = default_source(target)
    if fallback in names:
        return fallback
    return names[0] if names else None


def _agent_source() -> str:
    """The acoustic module's source text, streamed to a remote interpreter."""
    path = Path(acoustic.__file__)
    return path.read_text(encoding="utf-8")


def _parse_agent_output(stdout: str, stderr: str = "") -> dict:
    """Extract the last JSON object from a remote agent's output."""
    for line in reversed(stdout.splitlines()):
        text = line.strip()
        if not text.startswith("{"):
            continue
        try:
            payload = json.loads(text)
        except ValueError:
            continue
        if isinstance(payload, dict):
            return payload
    detail = (stderr or stdout).strip().splitlines()
    message = detail[-1] if detail else "remote capture produced no measurement"
    return {
        "ok": False,
        "started_ns": 0,
        "source": "",
        "onsets": [],
        "scores": [],
        "confidence": 0.0,
        "noise": 0.0,
        "skipped": True,
        "capture_error": True,
        "error": message,
    }


class AcousticCalibration:
    """One probe round across a set of devices.

    ``capture_func`` and ``play_func`` are injectable for tests; production uses
    the local capture path and one SSH process per remote device.
    """

    def __init__(
        self,
        targets: list[Target],
        sources: dict[str, str] | None = None,
        volume: int = 20,
        duration_s: float = acoustic.CAPTURE_DURATION_S,
        capture_func: Callable[[Target, str, float], dict] | None = None,
        play_func: Callable[[int], None] | None = None,
    ) -> None:
        self.targets = safety.unique_targets(targets, local_host_names())
        self.sources = dict(sources or {})
        self.volume = max(0, min(100, int(volume)))
        self.duration_s = max(1.0, float(duration_s))
        self.capture_func = capture_func
        self.play_func = play_func or (lambda level: acoustic.play_probe(level))
        self._agent = _agent_source()
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        self._cancelled.set()

    def resolve_sources(self, progress: Callable[[str], None] | None = None) -> list[str]:
        """Discover and persist a physical input for every target that has one."""
        problems: list[str] = []
        for target in self.targets:
            available = list_physical_sources(target)
            source = choose_source(target, self.sources.get(target.name), available)
            if source and source_muted(target, source):
                alternatives = [item for item in available if not source_muted(target, item)]
                if alternatives:
                    source = alternatives[0]
                else:
                    problems.append(f"{target.name}: all physical microphones are muted")
                    self.sources.pop(target.name, None)
                    if progress:
                        progress(f"{target.name}: microphone is muted")
                    continue
            if source:
                self.sources[target.name] = source
                if progress:
                    progress(f"{target.name}: microphone {source}")
            else:
                self.sources.pop(target.name, None)
                problems.append(f"{target.name}: no physical microphone source")
                if progress:
                    progress(f"{target.name}: no physical microphone")
        return problems

    def _local_capture(self, source: str) -> dict:
        return acoustic.capture_and_detect(source, self.duration_s)

    def _remote_capture(self, target: Target, source: str) -> tuple[subprocess.Popen, float]:
        assert target.host
        remote = f"python3 - {shlex.quote(source)} {shlex.quote(str(self.duration_s))}"
        command = ["ssh", *SSH_OPTIONS, target.host, remote]
        started = time.monotonic()
        proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert proc.stdin is not None
        proc.stdin.write(self._agent)
        proc.stdin.close()
        return proc, started

    def _finish_remote(self, target: Target, proc: subprocess.Popen, started: float) -> dict:
        try:
            stdout, stderr = proc.communicate(timeout=CAPTURE_SSH_TIMEOUT_S + self.duration_s)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            return {
                "ok": False,
                "started_ns": 0,
                "source": "",
                "onsets": [],
                "scores": [],
                "confidence": 0.0,
                "noise": 0.0,
                "skipped": True,
                "capture_error": True,
                "error": f"{target.name}: SSH capture timed out",
            }
        result = _parse_agent_output(stdout or "", stderr or "")
        if proc.returncode != 0 and result.get("ok"):
            result["ok"] = False
            result["error"] = (stderr or "").strip() or "remote capture exited early"
        if not result.get("error") and proc.returncode != 0:
            result["error"] = (stderr or "").strip() or "remote capture exited early"
        return result

    def run(self, progress: Callable[[str], None] | None = None) -> dict:
        """Run one calibration round and return JSON-safe measurements."""
        if not self.targets:
            return {
                "ok": False,
                "measurements": [],
                "offsets_ms": {},
                "reference": None,
                "skipped": [],
                "capture_errors": [],
                "errors": ["no connected devices"],
                "sources": dict(self.sources),
                "probe_played": False,
            }
        source_problems = self.resolve_sources(progress)
        captures: dict[str, Future | subprocess.Popen] = {}
        errors = list(source_problems)
        capture_errors: list[str] = []
        blocked: set[str] = set()
        if source_problems:
            # Do not emit an audible probe when any target lacks a verified
            # physical input.  A partial round must not create corrections or
            # touch persisted latencies.
            return {
                "ok": False,
                "measurements": [],
                "offsets_ms": {},
                "reference": None,
                "skipped": [],
                "capture_errors": capture_errors,
                "errors": errors,
                "sources": dict(self.sources),
                "probe_played": False,
            }
        local_executor = ThreadPoolExecutor(max_workers=max(1, len(self.targets)))
        for target in self.targets:
            if target.name in blocked or not self.sources.get(target.name):
                continue
            if not clock_synchronized(target):
                errors.append(f"{target.name}: clocks are not NTP-synchronized; measurement skipped")
                blocked.add(target.name)
                if progress:
                    progress(f"{target.name}: clock is not synchronized")
        try:
            for target in self.targets:
                if self._cancelled.is_set():
                    errors.append("calibration cancelled")
                    break
                source = self.sources.get(target.name)
                if not source or target.name in blocked:
                    continue
                if progress:
                    progress(f"{target.name}: microphone active (listening)")
                if self.capture_func is not None:
                    captures[target.name] = local_executor.submit(self.capture_func, target, source, self.duration_s)
                elif target.local or is_local_host(target.host):
                    captures[target.name] = local_executor.submit(self._local_capture, source)
                else:
                    try:
                        captures[target.name] = self._remote_capture(target, source)[0]
                    except (OSError, subprocess.SubprocessError) as exc:
                        capture_errors.append(target.name)
                        errors.append(f"{target.name}: {exc}")
            if captures:
                time.sleep(PROBE_LEAD_S)
                if self._cancelled.is_set():
                    errors.append("calibration cancelled")
                    return {
                        "ok": False,
                        "measurements": [],
                        "offsets_ms": {},
                        "reference": None,
                        "skipped": [],
                        "capture_errors": capture_errors,
                        "errors": errors,
                        "sources": dict(self.sources),
                        "probe_played": False,
                    }
                if progress:
                    progress("playing acoustic probe")
                try:
                    self.play_func(self.volume)
                    probe_played = True
                except Exception as exc:  # noqa: BLE001 - reported as a measurement failure
                    probe_played = False
                    capture_errors.append("probe playback")
                    errors.append(f"probe playback failed: {exc}")
                results: dict[str, dict] = {}
                for target in self.targets:
                    handle = captures.get(target.name)
                    if handle is None:
                        continue
                    if isinstance(handle, Future):
                        try:
                            results[target.name] = handle.result(timeout=self.duration_s + 5.0)
                        except Exception as exc:  # noqa: BLE001 - one device must not abort the round
                            capture_errors.append(target.name)
                            results[target.name] = {
                                "ok": False,
                                "started_ns": 0,
                                "source": self.sources.get(target.name, ""),
                                "onsets": [],
                                "scores": [],
                                "confidence": 0.0,
                                "noise": 0.0,
                                "skipped": True,
                                "capture_error": True,
                                "error": str(exc),
                            }
                    else:
                        results[target.name] = self._finish_remote(target, handle, 0.0)
                    if results.get(target.name, {}).get("capture_error"):
                        if target.name not in capture_errors:
                            capture_errors.append(target.name)
                summary = acoustic.measure_arrivals(results)
                summary["sources"] = dict(self.sources)
                summary["probe_played"] = probe_played
                summary["capture_errors"] = capture_errors
                summary["ok"] = bool(summary["offsets_ms"])
                summary["errors"] = [*errors, *summary["errors"]]
                return summary
        finally:
            local_executor.shutdown(wait=False, cancel_futures=True)
        return {
            "ok": False,
            "measurements": [],
            "offsets_ms": {},
            "reference": None,
            "skipped": [],
            "capture_errors": capture_errors,
            "errors": errors or ["no microphone capture started"],
            "sources": dict(self.sources),
            "probe_played": False,
        }


def ssh_available() -> bool:
    return shutil.which("ssh") is not None


def target_label(target: Target) -> str:
    return f"{target.name} ({target.host})" if target.host else f"{target.name} (local)"
