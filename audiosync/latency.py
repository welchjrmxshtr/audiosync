"""Latency control: the adaptive buffer plus the pre-roll gate.

Both classes are deliberately free of I/O so they can be exercised with
synthetic timelines in ``tests/test_latency.py`` - real Wi-Fi is too noisy
to unit-test against.
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import median


@dataclass
class ControllerConfig:
    """Knobs from the approved plan; changing these changes observed behaviour."""

    start_ms: int = 250
    step_ms: int = 20
    floor_ms: int = 100
    #: Auto's upper bound.  Still named ``ceiling_ms`` for older configs/tests;
    #: this is the named Auto maximum in the UI and status output.
    ceiling_ms: int = 1000
    settle_s: float = 30.0
    underrun_step_ms: int = 40
    disconnect_step_ms: int = 80
    cooldown_s: float = 60.0
    disconnect_cooldown_s: float = 120.0
    promote_after: int = 2


class LatencyController:
    """Converges the Snapcast buffer down to what the link actually sustains.

    Step down only after ``settle_s`` of clean playback; jump back up on an
    underrun or a disconnect and then hold off.  A value that forces two
    back-offs gets promoted to a learned floor and persisted by the caller.
    """

    def __init__(
        self,
        config: ControllerConfig | None = None,
        buffer_ms: int | None = None,
        learned_floor_ms: int | None = None,
    ) -> None:
        self.cfg = config or ControllerConfig()
        self._floor = self.cfg.floor_ms
        if learned_floor_ms is not None:
            self._floor = max(self._floor, min(int(learned_floor_ms), self.cfg.ceiling_ms))
        start = self.cfg.start_ms if buffer_ms is None else int(buffer_ms)
        self.buffer_ms = max(self._floor, min(self.cfg.ceiling_ms, start))
        self.last_change: float | None = None
        self.cooldown_until: float = 0.0
        self.underruns = 0
        self.steps_down = 0
        self._backoffs: dict[int, int] = {}
        self.learned_floor: int | None = learned_floor_ms
        self.enabled = True
        #: Client name -> wall-clock time until which Auto may not step down.
        self._holds: dict[str, float] = {}
        self._last_now = 0.0

    # ------------------------------------------------------------------
    def start(self, now: float, buffer_ms: int | None = None) -> int:
        """(Re)start convergence, e.g. after the server or a client restarts."""
        if buffer_ms is not None:
            self.buffer_ms = max(self._floor, min(self.cfg.ceiling_ms, int(buffer_ms)))
        self.last_change = now
        self.cooldown_until = 0.0
        self.steps_down = 0
        self._holds.clear()
        return self.buffer_ms

    def set_preset(self, preset: str, now: float, buffer_ms: int | None = None) -> int:
        """``deep`` and ``manual`` pin the buffer and disable convergence.

        Pinned values deliberately bypass the ceiling that Auto uses: a
        user asking for 3 s is not a convergence target.
        """
        if preset == "deep":
            self.enabled = False
            self.buffer_ms = 60_000
            self.last_change = now
            return self.buffer_ms
        if preset == "manual":
            self.enabled = False
            value = self.buffer_ms if buffer_ms is None else int(buffer_ms)
            self.buffer_ms = max(40, min(5000, value))
            self.last_change = now
            return self.buffer_ms
        self.enabled = True
        # Leaving Deep must come back to the Auto start point, not to 60 s
        return self.start(now, self.cfg.start_ms if buffer_ms is None else buffer_ms)

    @property
    def floor(self) -> int:
        return self._floor

    def tick(self, now: float, playing: bool = True) -> int | None:
        """Return a new buffer in ms if it is time to step down, else None."""
        self._last_now = now
        if not self.enabled or not playing:
            return None
        if any(until > now for until in self._holds.values()):
            return None
        if self.buffer_ms <= self._floor:
            return None
        if now < self.cooldown_until:
            return None
        if self.last_change is None:
            self.last_change = now
            return None
        if now - self.last_change < self.cfg.settle_s:
            return None
        self.buffer_ms = max(self._floor, self.buffer_ms - self.cfg.step_ms)
        self.last_change = now
        self.steps_down += 1
        return self.buffer_ms

    def on_underrun(self, now: float) -> int | None:
        return self._raise(now, self.cfg.underrun_step_ms, self.cfg.cooldown_s)

    def on_disconnect(self, now: float) -> int | None:
        return self._raise(now, self.cfg.disconnect_step_ms, self.cfg.disconnect_cooldown_s)

    def on_client_underrun(self, client: str, now: float) -> int | None:
        """Raise the shared buffer and remember which client caused the hold."""
        name = (client or "client").strip() or "client"
        self._holds[name] = now + self.cfg.cooldown_s
        return self._raise(now, self.cfg.underrun_step_ms, self.cfg.cooldown_s)

    def ensure_buffer(self, target_ms: int, now: float) -> int | None:
        """Raise the shared buffer to at least ``target_ms`` without settling down.

        Used when an acoustic per-client offset needs more shared headroom.  A
        pinned Manual/Deep preset is left alone; its user chose the buffer.
        """
        if not self.enabled:
            return None
        target = max(self._floor, min(self.cfg.ceiling_ms, int(target_ms)))
        if target <= self.buffer_ms:
            return None
        self.buffer_ms = target
        self.last_change = now
        self.cooldown_until = max(self.cooldown_until, now + self.cfg.cooldown_s)
        return target

    def _raise(self, now: float, step: int, cooldown: float) -> int | None:
        if not self.enabled:
            return None
        self.underruns += 1
        new_value = min(self.cfg.ceiling_ms, self.buffer_ms + step)
        self.buffer_ms = new_value
        self.last_change = now
        self.cooldown_until = now + cooldown
        # Two back-offs landing on the same value mean this link dislikes it.
        self._backoffs[new_value] = self._backoffs.get(new_value, 0) + 1
        if self._backoffs[new_value] >= self.cfg.promote_after and new_value > self._floor:
            self._floor = new_value
            self.learned_floor = new_value
        return new_value

    def status(self) -> dict[str, object]:
        held_by = [name for name, until in self._holds.items() if until > self._last_now]
        return {
            "buffer_ms": self.buffer_ms,
            "floor_ms": self._floor,
            "auto_max_ms": self.cfg.ceiling_ms,
            "steps_down": self.steps_down,
            "underruns": self.underruns,
            "enabled": self.enabled,
            "cooldown_s_left": max(0.0, self.cooldown_until - (self.last_change or 0.0)),
            "held_by": held_by,
        }


class AcousticOffsetTracker:
    """Slew per-client latency corrections toward acoustic measurements.

    Latency is a playout timestamp change, not a smooth volume control.  A
    measurement must persist across ``required`` rounds, only small steps are
    applied, and a deadband prevents convergence from oscillating around room
    noise or microphone jitter.
    """

    def __init__(
        self,
        applied: dict[str, int] | None = None,
        deadband_ms: int = 4,
        required: int = 2,
        max_step_ms: int = 5,
    ) -> None:
        self.applied = {str(name): max(0, int(value)) for name, value in (applied or {}).items()}
        self.deadband_ms = max(0, int(deadband_ms))
        self.required = max(1, int(required))
        self.max_step_ms = max(1, int(max_step_ms))
        self._samples: dict[str, list[int]] = {}

    def observe(self, offsets: dict[str, int]) -> dict[str, int]:
        """Return the client values that should be sent to Snapcast now."""
        updates: dict[str, int] = {}
        for name, value in offsets.items():
            key = str(name)
            target = max(0, int(value))
            current = self.applied.get(key, 0)
            if abs(target - current) <= self.deadband_ms:
                self._samples.pop(key, None)
                continue
            samples = self._samples.setdefault(key, [])
            samples.append(target)
            if len(samples) < self.required:
                continue
            candidate = int(round(median(samples[-self.required :])))
            step = max(-self.max_step_ms, min(self.max_step_ms, candidate - current))
            if step:
                current += step
                self.applied[key] = current
                updates[key] = current
            self._samples.pop(key, None)
        return updates

    def reset(self, applied: dict[str, int] | None = None) -> None:
        self.applied = {str(name): max(0, int(value)) for name, value in (applied or {}).items()}
        self._samples.clear()


class PreRollGate:
    """Keep a joining client muted until the stream is genuinely primed.

    Without this the first second after a join is a coin flip between clean
    audio and a crackle, which is exactly what makes small buffers risky.
    """

    def __init__(self, margin_ms: int = 250, ramp_ms: int = 500) -> None:
        self.margin_ms = margin_ms
        self.ramp_ms = ramp_ms
        self.started_at: float | None = None
        self.buffer_ms = 250
        self.released_at: float | None = None

    def begin(self, now: float, buffer_ms: int) -> None:
        self.started_at = now
        self.buffer_ms = max(0, int(buffer_ms))
        self.released_at = None

    @property
    def required_s(self) -> float:
        return (self.buffer_ms + self.margin_ms) / 1000.0

    def ready(self, now: float, connected: bool, playing: bool) -> bool:
        """True once the client may start making sound."""
        if self.started_at is None or not connected or not playing:
            return False
        if now - self.started_at < self.required_s:
            return False
        if self.released_at is None:
            self.released_at = now
        return True

    def volume(self, now: float, target: int) -> int:
        """0 until released, then a linear ramp up to ``target``."""
        if self.released_at is None:
            return 0
        if self.ramp_ms <= 0:
            return target
        fraction = min(1.0, (now - self.released_at) * 1000.0 / self.ramp_ms)
        return int(round(target * fraction))

    def reset(self) -> None:
        self.started_at = None
        self.released_at = None
