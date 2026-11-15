"""Runtime application of acoustic measurements to Snapcast clients.

This module is deliberately independent of Qt.  The GUI and the end-to-end
tests use the same applier so calibration, consistency checks, headroom and
Snapcast writes cannot silently diverge.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .backend import Backend, BackendError
from .config import Config
from .latency import AcousticOffsetTracker, LatencyController

#: Keep this much shared buffer above the largest per-client offset.  Snapcast
#: latency advances a client's playout timestamp; it must not consume the
#: buffer the group needs for network jitter.
HEADROOM_MS = 120


@dataclass
class ApplyReport:
    """What one result changed, for UI text and test assertions."""

    offsets: dict[str, int] = field(default_factory=dict)
    writes: dict[str, int] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    note: str = ""
    buffer_raised: int | None = None


class AcousticLatencyApplier:
    """Apply measured offsets to live clients through ``Client.SetLatency``.

    A new non-zero value needs two consistent measurements.  The reference
    client's zero value is written once so the server has an explicit baseline
    even when its default was already zero.  Values below the tracker's
    deadband produce no write.  Reapplying uses the persisted values after a
    server restart or a client reconnect.
    """

    def __init__(
        self,
        cfg: Config,
        backend: Backend,
        controller: LatencyController,
        host_ip: str = "127.0.0.1",
    ) -> None:
        self.cfg = cfg
        self.backend = backend
        self.controller = controller
        self.host_ip = host_ip
        self.applied_latency: dict[str, int] = {}
        self.tracker = AcousticOffsetTracker(cfg.client_latency_ms)
        self.last_report = ApplyReport()

    def reset(self, controller: LatencyController | None = None, host_ip: str | None = None) -> None:
        """Reset live tracking after a new host session or controller."""
        if controller is not None:
            self.controller = controller
        if host_ip is not None:
            self.host_ip = host_ip
        self.applied_latency.clear()
        self.tracker.reset(self.cfg.client_latency_ms)
        self.last_report = ApplyReport()

    def _rows_by_name(self, rows: list[dict]) -> dict[str, dict]:
        result: dict[str, dict] = {}
        for row in rows:
            if not row.get("connected"):
                continue
            name = str(row.get("name") or "").strip()
            if name:
                result[name] = row
        return result

    def _ensure_headroom(
        self,
        max_offset_ms: int,
        now: float,
        apply_buffer: Callable[[int], None] | None,
        persist_floor: Callable[[], None] | None,
    ) -> tuple[bool, int | None, str]:
        if max_offset_ms <= 0:
            return True, None, ""
        needed = int(max_offset_ms) + HEADROOM_MS
        if needed <= self.controller.buffer_ms:
            return True, None, ""
        if needed > self.controller.cfg.ceiling_ms:
            return (
                False,
                None,
                f"measured offset needs ~{needed} ms, above the "
                f"{self.controller.cfg.ceiling_ms} ms Auto maximum",
            )
        raised = self.controller.ensure_buffer(needed, now)
        if raised is None:
            return (
                False,
                None,
                f"needs ~{needed} ms of buffer, but the current "
                f"{self.controller.buffer_ms} ms buffer cannot be raised in this preset",
            )
        if apply_buffer is not None:
            apply_buffer(raised)
        if persist_floor is not None:
            persist_floor()
        return True, raised, ""

    def _write(
        self,
        name: str,
        value: int,
        rows: dict[str, dict],
        writes: dict[str, int],
        errors: list[str],
    ) -> bool:
        row = rows.get(name)
        if not row:
            errors.append(f"{name}: no connected Snapcast client")
            return False
        client_id = str(row.get("id") or "")
        if not client_id:
            errors.append(f"{name}: Snapcast client has no id")
            return False
        value = max(0, int(value))
        try:
            self.backend.set_client_latency(self.host_ip, client_id, value)
        except BackendError as exc:
            errors.append(f"{name}: latency update failed: {exc}")
            return False
        self.applied_latency[client_id] = value
        self.cfg.client_latency_ms[name] = value
        writes[name] = value
        return True

    def apply_result(
        self,
        result: dict,
        rows: list[dict],
        now: float,
        *,
        apply_buffer: Callable[[int], None] | None = None,
        persist_floor: Callable[[], None] | None = None,
    ) -> ApplyReport:
        """Apply one real calibration result; never write without a valid row."""
        offsets = {str(name): max(0, int(value)) for name, value in (result.get("offsets_ms") or {}).items()}
        skipped = [str(name) for name in (result.get("skipped") or [])]
        errors = [str(item) for item in (result.get("errors") or [])]
        capture_errors = [str(name) for name in (result.get("capture_errors") or [])]
        if capture_errors:
            detail = f"capture/SSH failure on {', '.join(capture_errors)}; existing latencies left unchanged"
            report = ApplyReport(
                offsets=offsets,
                skipped=skipped,
                errors=[*errors, detail],
                note=detail,
            )
            self.last_report = report
            return report
        writes: dict[str, int] = {}
        if not offsets:
            note = "measurement failed" if not skipped else f"probe answered on no device ({len(skipped)} skipped)"
            report = ApplyReport(offsets=offsets, skipped=skipped, errors=errors, note=note)
            self.last_report = report
            return report

        rows_by_name = self._rows_by_name(rows)
        eligible = {name: value for name, value in offsets.items() if name in rows_by_name}
        if not eligible:
            errors.append("no measured device has a connected Snapcast client")
            report = ApplyReport(offsets=offsets, skipped=skipped, errors=errors, note="measurement could not be applied")
            self.last_report = report
            return report

        if skipped:
            # A skip is evidence that the measurement is incomplete.  Keep the
            # existing per-client values and let group Auto absorb the gap,
            # rather than half-applying a sample from a partial round.
            raised = None
            for name in skipped:
                raised = self.controller.on_client_underrun(name, now)
                if raised is not None:
                    if apply_buffer is not None:
                        apply_buffer(raised)
                    if persist_floor is not None:
                        persist_floor()
            note = f"skipping: {', '.join(skipped)}; existing latencies left unchanged"
            if raised is not None:
                note += f"; shared buffer raised to {raised} ms"
            report = ApplyReport(
                offsets=offsets,
                skipped=skipped,
                errors=errors,
                note=note,
                buffer_raised=raised,
            )
            self.last_report = report
            return report

        okay, raised, headroom_error = self._ensure_headroom(
            max(eligible.values(), default=0), now, apply_buffer, persist_floor
        )
        if not okay:
            errors.append(headroom_error)
            note = headroom_error
            report = ApplyReport(offsets=offsets, skipped=skipped, errors=errors, note=note)
            self.last_report = report
            return report

        previous = dict(self.tracker.applied)
        updates = self.tracker.observe(eligible)
        # Explicitly establish the reference at zero once.  This is a baseline,
        # not a new non-zero correction, so it does not need two samples.
        for name, value in eligible.items():
            if value != 0 or name in self.cfg.client_latency_ms:
                continue
            row = rows_by_name[name]
            if int(row.get("latency") or 0) != 0:
                continue
            updates.setdefault(name, 0)

        for name, value in sorted(updates.items()):
            if not self._write(name, value, rows_by_name, writes, errors):
                # Do not let a failed RPC trick the tracker into thinking the
                # value is already live; retry after the next measurement.
                if name in previous:
                    self.tracker.applied[name] = previous[name]
                else:
                    self.tracker.applied.pop(name, None)

        if updates:
            self.cfg.save()

        if offsets:
            note = f"{len(offsets)} device(s) measured, max offset {max(offsets.values())} ms"
            if writes:
                note += f", applied {len(writes)}"
            else:
                note += ", waiting for a second consistent measurement"
        else:
            note = "measurement failed"
        if skipped:
            note += f"; skipping: {', '.join(skipped)}"
        if errors:
            note += f"; {errors[0]}"
        report = ApplyReport(
            offsets=offsets,
            writes=writes,
            skipped=skipped,
            errors=errors,
            note=note,
            buffer_raised=raised,
        )
        self.last_report = report
        return report

    def reapply(
        self,
        rows: list[dict],
        now: float,
        *,
        apply_buffer: Callable[[int], None] | None = None,
        persist_floor: Callable[[], None] | None = None,
    ) -> dict[str, int]:
        """Re-send persisted values after a restart, reconnect or host start."""
        if not self.cfg.client_latency_ms:
            return {}
        desired = {str(name): max(0, int(value)) for name, value in self.cfg.client_latency_ms.items()}
        okay, raised, headroom_error = self._ensure_headroom(
            max(desired.values(), default=0), now, apply_buffer, persist_floor
        )
        if not okay:
            self.last_report = ApplyReport(offsets=desired, note=headroom_error, errors=[headroom_error])
            return {}
        rows_by_name = self._rows_by_name(rows)
        writes: dict[str, int] = {}
        errors: list[str] = []
        for name, value in sorted(desired.items()):
            row = rows_by_name.get(name)
            if not row:
                continue
            client_id = str(row.get("id") or "")
            if not client_id:
                continue
            if (
                self.applied_latency.get(client_id) == value
                and int(row.get("latency") or 0) == value
            ):
                continue
            try:
                self.backend.set_client_latency(self.host_ip, client_id, value)
            except BackendError as exc:
                errors.append(f"{name}: reapply failed: {exc}")
                continue
            self.applied_latency[client_id] = value
            writes[name] = value
        self.last_report = ApplyReport(offsets=desired, writes=writes, errors=errors)
        return writes
