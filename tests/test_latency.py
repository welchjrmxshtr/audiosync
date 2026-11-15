"""Synthetic timelines for the latency controller and the pre-roll gate.

Real Wi-Fi cannot be unit-tested; these pin the decision rules instead.
Run with:  python3 -m unittest discover -s tests -t .
"""

from __future__ import annotations

import unittest

from audiosync.latency import AcousticOffsetTracker, ControllerConfig, LatencyController, PreRollGate


class ControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = ControllerConfig(
            start_ms=250, step_ms=20, floor_ms=100, ceiling_ms=400,
            settle_s=30.0, underrun_step_ms=40, disconnect_step_ms=80,
            cooldown_s=60.0, disconnect_cooldown_s=120.0, promote_after=2,
        )

    def test_starts_at_250_and_waits_for_settle(self) -> None:
        ctrl = LatencyController(self.cfg)
        self.assertEqual(ctrl.buffer_ms, 250)
        ctrl.start(0.0)
        self.assertIsNone(ctrl.tick(10.0))
        self.assertEqual(ctrl.buffer_ms, 250)

    def test_converges_down_to_floor_not_below(self) -> None:
        ctrl = LatencyController(self.cfg)
        now = ctrl.start(0.0)
        seen = []
        for step in range(1, 40):
            now = step * 30.0
            changed = ctrl.tick(now)
            if changed is not None:
                seen.append(changed)
        self.assertEqual(seen[:3], [230, 210, 190])
        self.assertEqual(ctrl.buffer_ms, 100, "must stop exactly at the floor")
        self.assertEqual(seen[-1], 100)
        self.assertGreater(ctrl.steps_down, 0)

    def test_underrun_raises_and_cools_down(self) -> None:
        ctrl = LatencyController(self.cfg)
        ctrl.start(0.0)
        ctrl.tick(30.0)  # 230
        ctrl.tick(60.0)  # 210
        self.assertEqual(ctrl.buffer_ms, 210)
        self.assertEqual(ctrl.on_underrun(61.0), 250)
        self.assertEqual(ctrl.underruns, 1)
        # inside the 60 s cooldown: no step down even after a full settle
        self.assertIsNone(ctrl.tick(120.0))
        self.assertEqual(ctrl.buffer_ms, 250)
        # after cooldown and a settle window it resumes
        self.assertEqual(ctrl.tick(200.0), 230)

    def test_disconnect_raises_more_and_holds_longer(self) -> None:
        ctrl = LatencyController(self.cfg)
        ctrl.start(0.0)
        self.assertEqual(ctrl.on_disconnect(10.0), 330)
        self.assertIsNone(ctrl.tick(69.0))
        self.assertEqual(ctrl.buffer_ms, 330)
        self.assertEqual(ctrl.tick(131.0), 310)

    def test_never_exceeds_ceiling(self) -> None:
        ctrl = LatencyController(self.cfg)
        ctrl.start(0.0)
        for i in range(10):
            ctrl.on_disconnect(i * 500.0)
        self.assertEqual(ctrl.buffer_ms, 400)

    def test_two_backoffs_promote_learned_floor(self) -> None:
        ctrl = LatencyController(self.cfg)
        ctrl.start(0.0)
        ctrl.buffer_ms = 180
        ctrl.on_underrun(1.0)  # 220
        ctrl.on_underrun(2.0)  # 260
        self.assertIsNone(ctrl.learned_floor)
        ctrl.buffer_ms = 180
        ctrl.on_underrun(3.0)  # 220 again
        ctrl.buffer_ms = 180
        ctrl.on_underrun(4.0)  # 220 -> second time
        self.assertEqual(ctrl.learned_floor, 220)
        self.assertEqual(ctrl.floor, 220)

    def test_learned_floor_is_honoured_on_construction(self) -> None:
        ctrl = LatencyController(self.cfg, learned_floor_ms=220)
        self.assertEqual(ctrl.floor, 220)
        self.assertEqual(ctrl.buffer_ms, 250)
        ctrl.start(0.0)
        for step in range(1, 20):
            ctrl.tick(step * 30.0)
        self.assertEqual(ctrl.buffer_ms, 220)

    def test_deep_preset_disables_convergence(self) -> None:
        ctrl = LatencyController(self.cfg)
        self.assertEqual(ctrl.set_preset("deep", 0.0), 60_000)
        for step in range(1, 20):
            self.assertIsNone(ctrl.tick(step * 30.0))
        self.assertEqual(ctrl.buffer_ms, 60_000)
        self.assertEqual(ctrl.set_preset("auto", 1000.0), 250)
        self.assertTrue(ctrl.enabled)

    def test_manual_preset_keeps_large_buffer(self) -> None:
        ctrl = LatencyController(self.cfg)
        self.assertEqual(ctrl.set_preset("manual", 0.0, 3000), 3000)
        self.assertFalse(ctrl.enabled)
        self.assertEqual(ctrl.set_preset("manual", 1.0, 320), 320)
        for step in range(1, 10):
            self.assertIsNone(ctrl.tick(step * 30.0))

    def test_not_playing_freezes_convergence(self) -> None:
        ctrl = LatencyController(self.cfg)
        ctrl.start(0.0)
        self.assertIsNone(ctrl.tick(300.0, playing=False))

    def test_default_auto_max_is_one_thousand(self) -> None:
        self.assertEqual(ControllerConfig().ceiling_ms, 1000)

    def test_default_auto_never_exceeds_one_thousand(self) -> None:
        ctrl = LatencyController(ControllerConfig())
        ctrl.start(0.0)
        for index in range(30):
            ctrl.on_underrun(index * 100.0)
        self.assertEqual(ctrl.buffer_ms, 1000)

    def test_client_hold_blocks_step_down(self) -> None:
        ctrl = LatencyController(self.cfg)
        ctrl.start(0.0)
        self.assertEqual(ctrl.on_client_underrun("remote", 10.0), 290)
        self.assertIn("remote", ctrl.status()["held_by"])
        self.assertIsNone(ctrl.tick(69.0))
        self.assertEqual(ctrl.tick(200.0), 270)

    def test_ensure_buffer_raises_without_settling(self) -> None:
        ctrl = LatencyController(self.cfg, buffer_ms=250)
        self.assertEqual(ctrl.ensure_buffer(320, 1.0), 320)
        self.assertIsNone(ctrl.ensure_buffer(300, 2.0))
        self.assertEqual(ctrl.buffer_ms, 320)


class AcousticOffsetTrackerTests(unittest.TestCase):
    def test_requires_two_consistent_measurements_and_slews(self) -> None:
        tracker = AcousticOffsetTracker(deadband_ms=4, required=2, max_step_ms=5)
        self.assertEqual(tracker.observe({"a": 20}), {})
        self.assertEqual(tracker.observe({"a": 20}), {"a": 5})
        self.assertEqual(tracker.observe({"a": 20}), {})
        self.assertEqual(tracker.observe({"a": 20}), {"a": 10})

    def test_deadband_clears_a_pending_measurement(self) -> None:
        tracker = AcousticOffsetTracker(applied={"a": 10}, deadband_ms=4, required=2)
        self.assertEqual(tracker.observe({"a": 20}), {})
        self.assertEqual(tracker.observe({"a": 12}), {})
        self.assertEqual(tracker.applied["a"], 10)

    def test_tracker_can_reduce_a_value(self) -> None:
        tracker = AcousticOffsetTracker(applied={"a": 20}, deadband_ms=2, required=1, max_step_ms=5)
        self.assertEqual(tracker.observe({"a": 0}), {"a": 15})


class PreRollTests(unittest.TestCase):
    def test_gate_waits_for_buffer_plus_margin(self) -> None:
        gate = PreRollGate(margin_ms=250, ramp_ms=500)
        gate.begin(100.0, buffer_ms=250)
        self.assertFalse(gate.ready(100.4, connected=True, playing=True))
        self.assertEqual(gate.volume(100.4, 100), 0)
        self.assertTrue(gate.ready(100.5, connected=True, playing=True))

    def test_gate_requires_connection_and_playing(self) -> None:
        gate = PreRollGate()
        gate.begin(0.0, buffer_ms=0)
        self.assertFalse(gate.ready(10.0, connected=False, playing=True))
        self.assertFalse(gate.ready(10.0, connected=True, playing=False))
        self.assertTrue(gate.ready(10.0, connected=True, playing=True))

    def test_volume_ramps_then_reaches_target(self) -> None:
        gate = PreRollGate(margin_ms=0, ramp_ms=500)
        gate.begin(0.0, buffer_ms=0)
        self.assertTrue(gate.ready(0.0, True, True))
        self.assertEqual(gate.volume(0.0, 100), 0)
        self.assertEqual(gate.volume(0.25, 100), 50)
        self.assertEqual(gate.volume(0.5, 100), 100)
        self.assertEqual(gate.volume(9.0, 100), 100)

    def test_never_ramps_above_target(self) -> None:
        gate = PreRollGate(margin_ms=0, ramp_ms=1)
        gate.begin(0.0, buffer_ms=0)
        gate.ready(0.0, True, True)
        self.assertLessEqual(gate.volume(5.0, 80), 80)

    def test_deep_mode_gate_waits_a_minute(self) -> None:
        gate = PreRollGate(margin_ms=250)
        gate.begin(0.0, buffer_ms=60_000)
        self.assertFalse(gate.ready(59.0, True, True))
        self.assertTrue(gate.ready(60.3, True, True))


if __name__ == "__main__":
    unittest.main()
