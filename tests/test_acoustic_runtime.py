"""End-to-end acoustic correction tests through the runtime applier.

The calibration orchestrator and the applier are the production classes the
Qt app uses.  Only the SSH/audio capture and the Snapcast HTTP transport are
injected, so these tests exercise the path from probe measurements to
``Client.SetLatency`` without touching hardware.
"""

from __future__ import annotations

import unittest
from unittest import mock

from audiosync import app as app_mod
from audiosync import backend as backend_mod
from audiosync import calibration, config
from audiosync.acoustic_sync import AcousticLatencyApplier
from audiosync.latency import ControllerConfig, LatencyController
from audiosync.preload import Target


class FakeSnapcast:
    """Real Backend transport stub with a Snapcast-shaped status document."""

    def __init__(self) -> None:
        self.clients = [
            {"id": "a-id", "name": "a", "host": "a-host", "connected": True, "latency": 0},
            {"id": "b-id", "name": "b", "host": "b-host", "connected": True, "latency": 0},
            {"id": "c-id", "name": "c", "host": "c-host", "connected": True, "latency": 0},
        ]
        self.latency_calls: list[tuple[str, int]] = []
        self.buffer_calls: list[tuple[str, int]] = []
        self.fail_latency = False

    def snapshot(self) -> dict:
        clients = []
        for client in self.clients:
            clients.append(
                {
                    "id": client["id"],
                    "connected": client["connected"],
                    "host": {"name": client["host"]},
                    "config": {
                        "name": client["name"],
                        "latency": client["latency"],
                        "volume": {"percent": 100, "muted": False},
                    },
                }
            )
        return {"server": {"groups": [{"clients": clients}]}}

    def rpc(self, host: str, method: str, params: dict | None = None, **kwargs) -> dict:
        del host, kwargs
        if method == "Server.GetStatus":
            return self.snapshot()
        if method == "Client.SetLatency":
            assert params is not None
            if self.fail_latency:
                raise backend_mod.BackendError("fake latency failure")
            client_id = str(params["id"])
            value = int(params["latency"])
            self.latency_calls.append((client_id, value))
            for client in self.clients:
                if client["id"] == client_id:
                    client["latency"] = value
                    break
            return {}
        if method in ("Stream.SetBuffer", "Server.SetBuffer"):
            assert params is not None
            self.buffer_calls.append((method, int(params["buffer"])))
            return {}
        raise backend_mod.BackendError(f"unexpected fake RPC {method}")

    def reset_latencies(self) -> None:
        for client in self.clients:
            client["latency"] = 0


class AcousticRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = config.Config(playback="synced", codec="pcm")
        self.transport = FakeSnapcast()
        self.rpc_patch = mock.patch("audiosync.backend.rpc", side_effect=self.transport.rpc)
        self.rpc_patch.start()
        self.addCleanup(self.rpc_patch.stop)
        self.save_patch = mock.patch.object(self.cfg, "save")
        self.save_patch.start()
        self.addCleanup(self.save_patch.stop)
        self.backend = backend_mod.Backend(self.cfg)
        self.controller = LatencyController(
            ControllerConfig(floor_ms=100, ceiling_ms=1000),
            buffer_ms=400,
        )
        self.applier = AcousticLatencyApplier(self.cfg, self.backend, self.controller)
        # Call the real MainWindow acoustic wrappers without constructing the
        # whole Qt window.  This keeps the test on the same application methods
        # that the GUI invokes after a calibration thread finishes.
        self.window = app_mod.MainWindow.__new__(app_mod.MainWindow)
        self.window.state = app_mod.HOSTING
        self.window.cfg = self.cfg
        self.window._latency_applier = self.applier
        self.window._last_rows = []
        self.window._acoustic_note = ""
        self.window._apply_buffer = self.apply_buffer
        self.window._persist_learned_floor = self.persist_floor
        self.window._refresh_acoustic_label = lambda: None
        self.window._now = lambda: self.now
        self.now = 10.0
        self.targets = [Target("a"), Target("b", "b-host"), Target("c", "c-host")]
        self.buffer_applies: list[int] = []
        self.floor_persists = 0
        self.played: list[int] = []

    def apply_buffer(self, value: int) -> None:
        self.buffer_applies.append(value)
        self.backend.apply_buffer("127.0.0.1", value, self.cfg.codec, self.cfg.playback)

    def persist_floor(self) -> None:
        self.floor_persists += 1

    def rows(self) -> list[dict]:
        return self.backend.clients("127.0.0.1")

    def run_calibration(self, offsets: dict[str, int], failed: set[str] | None = None) -> dict:
        failed = failed or set()

        def capture(target: Target, source: str, duration: float) -> dict:
            del duration
            if target.name in failed:
                raise RuntimeError(f"{target.name}: fake SSH capture failed")
            delay = offsets.get(target.name, 0) / 1000.0
            return {
                "ok": True,
                "started_ns": 1_000_000_000,
                "source": source,
                "onsets": [0.250 + delay, 0.500 + delay, 0.750 + delay],
                "scores": [1.0, 1.0, 1.0],
                "confidence": 1.0,
                "noise": 0.0,
                "skipped": False,
                "error": "",
            }

        with mock.patch.object(calibration, "list_physical_sources", return_value=["mic"]), \
             mock.patch.object(calibration, "default_source", return_value="mic"), \
             mock.patch.object(calibration, "source_muted", return_value=False), \
             mock.patch.object(calibration, "clock_synchronized", return_value=True), \
             mock.patch.object(calibration, "PROBE_LEAD_S", 0.0):
            job = calibration.AcousticCalibration(
                self.targets,
                {"a": "mic", "b": "mic", "c": "mic"},
                capture_func=capture,
                play_func=self.played.append,
            )
            return job.run()

    def apply(self, result: dict, now: float = 10.0):
        self.now = now
        self.window._last_rows = self.rows()
        self.window._apply_acoustic_result(result)
        return self.applier.last_report

    def reapply(self, now: float = 30.0) -> dict[str, int]:
        self.now = now
        self.window._apply_saved_latencies(self.rows())
        return self.applier.last_report.writes

    def prime(self) -> None:
        first = self.apply(self.run_calibration({"a": 0, "b": 33, "c": 20}), 10.0)
        self.assertEqual(first.writes, {"a": 0})
        second = self.apply(self.run_calibration({"a": 0, "b": 33, "c": 20}), 20.0)
        self.assertEqual(second.writes, {"b": 5, "c": 5})
        self.assertEqual(self.transport.latency_calls, [("a-id", 0), ("b-id", 5), ("c-id", 5)])

    def test_real_round_writes_zero_then_requires_consistency(self) -> None:
        result = self.run_calibration({"a": 0, "b": 33, "c": 20})
        self.assertEqual(result["offsets_ms"], {"a": 0, "b": 33, "c": 20})
        self.assertEqual(result["reference"], "a")
        self.assertTrue(result["probe_played"])

        first = self.apply(result, 10.0)
        self.assertEqual(first.writes, {"a": 0})
        self.assertEqual(self.transport.latency_calls, [("a-id", 0)])
        self.assertEqual(self.cfg.client_latency_ms, {"a": 0})
        self.assertTrue(all(value >= 0 for _client, value in self.transport.latency_calls))

        second = self.apply(self.run_calibration({"a": 0, "b": 33, "c": 20}), 20.0)
        self.assertEqual(second.writes, {"b": 5, "c": 5})
        self.assertEqual(self.transport.latency_calls, [("a-id", 0), ("b-id", 5), ("c-id", 5)])
        self.assertEqual(self.cfg.client_latency_ms, {"a": 0, "b": 5, "c": 5})
        self.assertFalse(any(value < 0 for _client, value in self.transport.latency_calls))

    def test_deadband_suppresses_writes_and_slew_limit_is_honoured(self) -> None:
        self.prime()
        self.transport.latency_calls.clear()

        # 3 ms is inside the 4 ms deadband: no write, even after two rounds.
        for now in (30.0, 40.0):
            report = self.apply(self.run_calibration({"a": 0, "b": 8, "c": 5}), now)
            self.assertEqual(report.writes, {})
        self.assertEqual(self.transport.latency_calls, [])

        # A change still requires a second consistent sample; each write is
        # capped at five milliseconds.
        first = self.apply(self.run_calibration({"a": 0, "b": 20, "c": 20}), 50.0)
        self.assertEqual(first.writes, {})
        second = self.apply(self.run_calibration({"a": 0, "b": 20, "c": 20}), 60.0)
        self.assertEqual(second.writes, {"b": 10, "c": 10})
        self.assertEqual(self.transport.latency_calls, [("b-id", 10), ("c-id", 10)])
        self.assertTrue(all(value >= 0 for _client, value in self.transport.latency_calls))

    def test_headroom_raises_shared_buffer_before_the_offset(self) -> None:
        result = self.run_calibration({"a": 0, "b": 600, "c": 20})
        report = self.apply(result, 10.0)
        self.assertEqual(report.buffer_raised, 720)
        self.assertEqual(self.controller.buffer_ms, 720)
        self.assertIn(720, self.buffer_applies)
        self.assertEqual(self.transport.buffer_calls, [("Stream.SetBuffer", 720)])
        self.assertEqual(report.writes, {"a": 0})
        self.assertEqual(self.floor_persists, 1)

        second = self.apply(self.run_calibration({"a": 0, "b": 600, "c": 20}), 20.0)
        self.assertEqual(second.writes, {"b": 5, "c": 5})

    def test_persisted_offsets_reapply_after_restart_and_reconnect(self) -> None:
        self.prime()
        self.transport.reset_latencies()
        self.transport.latency_calls.clear()
        self.applier.reset(self.controller)
        writes = self.reapply(30.0)
        self.assertEqual(writes, {"a": 0, "b": 5, "c": 5})
        self.assertEqual(self.transport.latency_calls, [("a-id", 0), ("b-id", 5), ("c-id", 5)])

        # A reconnect gets a new Snapcast id and loses the server-side value.
        self.transport.clients[1]["id"] = "b-new"
        self.transport.clients[1]["latency"] = 0
        writes = self.reapply(40.0)
        self.assertEqual(writes, {"b": 5})
        self.assertEqual(self.transport.latency_calls[-1], ("b-new", 5))

    def test_low_confidence_and_skip_apply_nothing(self) -> None:
        self.transport.latency_calls.clear()
        low = self.apply(
            {"offsets_ms": {}, "skipped": ["b"], "errors": ["b: confidence below threshold"]},
            10.0,
        )
        self.assertEqual(low.writes, {})
        self.assertIn("skipped", low.note)
        self.assertEqual(self.transport.latency_calls, [])

        partial = self.apply(
            {"offsets_ms": {"a": 0, "b": 33}, "skipped": ["b"], "errors": []},
            20.0,
        )
        self.assertEqual(partial.writes, {})
        self.assertEqual(self.transport.latency_calls, [])
        self.assertEqual(self.controller.buffer_ms, 440)
        self.assertEqual(self.transport.buffer_calls, [("Stream.SetBuffer", 440)])

    def test_capture_failure_mid_round_leaves_latencies_untouched(self) -> None:
        self.prime()
        self.transport.latency_calls.clear()
        before = dict(self.cfg.client_latency_ms)

        result = self.run_calibration({"a": 0, "b": 33, "c": 20}, failed={"b"})
        self.assertIn("b", result["capture_errors"])
        report = self.apply(result, 30.0)
        self.assertEqual(report.writes, {})
        self.assertIn("existing latencies left unchanged", report.note)
        self.assertEqual(self.transport.latency_calls, [])
        self.assertEqual(self.cfg.client_latency_ms, before)


if __name__ == "__main__":
    unittest.main()
