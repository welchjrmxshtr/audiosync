"""Calibration orchestration tests with fake captures and no network."""

from __future__ import annotations

import json
import subprocess
import unittest
from unittest import mock

from audiosync import calibration, safety
from audiosync.preload import Target


class SourceTests(unittest.TestCase):
    def test_remote_source_listing_filters_verbose_monitor_entries(self) -> None:
        text = """Source #1
\tName: audiosync_sink.monitor
\tMonitor of Sink: audiosync_sink
\tProperties:
\t\tdevice.class = "monitor"
Source #2
\tName: alsa_input.usb-mic
"""
        proc = mock.Mock(stdout=text, returncode=0)
        with mock.patch.object(calibration, "_run", return_value=proc):
            sources = calibration.list_physical_sources(Target("remote", "remote-host"))
        self.assertEqual(sources, ["alsa_input.usb-mic"])

    def test_timeout_returns_no_sources(self) -> None:
        with mock.patch.object(calibration, "_run", side_effect=subprocess.TimeoutExpired("ssh", 1)):
            self.assertEqual(calibration.list_physical_sources(Target("remote", "remote-host")), [])

    def test_choose_source_prefers_configured_then_default(self) -> None:
        target = Target("a")
        with mock.patch.object(calibration, "default_source", return_value="default-mic"):
            self.assertEqual(calibration.choose_source(target, "configured", ["default-mic", "configured"]), "configured")
            self.assertEqual(calibration.choose_source(target, "missing", ["default-mic", "other"]), "default-mic")
            self.assertEqual(calibration.choose_source(target, None, ["other"]), "other")
            self.assertIsNone(calibration.choose_source(target, None, []))
            self.assertIsNone(
                calibration.choose_source(target, "audiosync_sink.monitor", ["audiosync_sink.monitor"])
            )

    def test_a_muted_configured_source_falls_back_to_an_unmuted_one(self) -> None:
        target = Target("a")
        with mock.patch.object(calibration, "list_physical_sources", return_value=["muted", "usable"]), \
             mock.patch.object(calibration, "default_source", return_value="muted"), \
             mock.patch.object(calibration, "source_muted", side_effect=lambda _target, name: name == "muted"):
            job = calibration.AcousticCalibration([target], {"a": "muted"})
            problems = job.resolve_sources()
        self.assertEqual(job.sources["a"], "usable")
        self.assertEqual(problems, [])

    def test_all_muted_sources_are_removed_before_capture(self) -> None:
        target = Target("a")
        with mock.patch.object(calibration, "list_physical_sources", return_value=["mic"]), \
             mock.patch.object(calibration, "default_source", return_value="mic"), \
             mock.patch.object(calibration, "source_muted", return_value=True):
            job = calibration.AcousticCalibration([target], {"a": "mic"}, play_func=lambda _level: None)
            result = job.run()
        self.assertNotIn("a", job.sources)
        self.assertFalse(result["probe_played"])
        self.assertTrue(any("muted" in error for error in result["errors"]))


class AgentOutputTests(unittest.TestCase):
    def test_last_json_object_wins(self) -> None:
        payload = {"ok": True, "onsets": [0.25], "error": ""}
        parsed = calibration._parse_agent_output("noise\n" + json.dumps(payload) + "\n", "")
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["onsets"], [0.25])

    def test_plain_error_is_reported(self) -> None:
        parsed = calibration._parse_agent_output("", "python3: command not found")
        self.assertFalse(parsed["ok"])
        self.assertIn("python3", parsed["error"])

    def test_remote_capture_timeout_is_a_clean_failure(self) -> None:
        proc = mock.Mock()
        proc.communicate.side_effect = [subprocess.TimeoutExpired("ssh", 1), ("", "")]
        job = calibration.AcousticCalibration([Target("remote", "remote-host")])
        result = job._finish_remote(Target("remote", "remote-host"), proc, 0.0)
        self.assertFalse(result["ok"])
        self.assertIn("timed out", result["error"])


class CalibrationTests(unittest.TestCase):
    def _capture(self, target: Target, source: str, duration: float) -> dict:
        del source, duration
        started = 1_000_000_000 + (33_000_000 if target.name == "b" else 0)
        return {
            "ok": True,
            "started_ns": started,
            "source": "mic",
            "onsets": [0.250, 0.500, 0.750],
            "scores": [1.0, 1.0, 1.0],
            "confidence": 1.0,
            "noise": 0.0,
            "skipped": False,
            "error": "",
        }

    def test_one_round_measures_and_plays_once(self) -> None:
        played: list[int] = []
        targets = [Target("a"), Target("b", "remote-host")]
        with mock.patch.object(calibration, "list_physical_sources", return_value=["mic"]), \
             mock.patch.object(calibration, "default_source", return_value="mic"), \
             mock.patch.object(calibration, "clock_synchronized", return_value=True), \
             mock.patch.object(calibration, "source_muted", return_value=False), \
             mock.patch.object(calibration, "PROBE_LEAD_S", 0.0):
            job = calibration.AcousticCalibration(
                targets,
                {"a": "mic", "b": "mic"},
                capture_func=self._capture,
                play_func=played.append,
            )
            result = job.run()
        self.assertEqual(played, [20])
        self.assertTrue(result["probe_played"])
        self.assertEqual(result["offsets_ms"]["a"], 0)
        self.assertEqual(result["offsets_ms"]["b"], 33)
        self.assertEqual(result["reference"], "a")

    def test_one_unsafe_target_prevents_the_probe(self) -> None:
        played: list[int] = []
        targets = [Target("a"), Target("b", "remote-host")]
        with mock.patch.object(calibration, "list_physical_sources", side_effect=[["mic"], []]), \
             mock.patch.object(calibration, "default_source", side_effect=["mic", None]), \
             mock.patch.object(calibration, "source_muted", return_value=False), \
             mock.patch.object(calibration, "clock_synchronized", return_value=True):
            job = calibration.AcousticCalibration(
                targets,
                capture_func=self._capture,
                play_func=played.append,
            )
            result = job.run()
        self.assertFalse(result["probe_played"])
        self.assertFalse(played)
        self.assertTrue(any("b" in error for error in result["errors"]))

    def test_duplicate_targets_are_rejected(self) -> None:
        with self.assertRaises(safety.RoutingError):
            calibration.AcousticCalibration([Target("a"), Target("a", "remote-host")])

    def test_no_physical_microphone_does_not_play(self) -> None:
        played: list[int] = []
        target = Target("a")
        with mock.patch.object(calibration, "list_physical_sources", return_value=[]), \
             mock.patch.object(calibration, "default_source", return_value=None):
            job = calibration.AcousticCalibration([target], capture_func=self._capture, play_func=played.append)
            result = job.run()
        self.assertFalse(result["probe_played"])
        self.assertFalse(played)
        self.assertTrue(any("no physical microphone" in error for error in result["errors"]))

    def test_unsynchronized_clock_blocks_measurement(self) -> None:
        played: list[int] = []
        target = Target("a")
        with mock.patch.object(calibration, "list_physical_sources", return_value=["mic"]), \
             mock.patch.object(calibration, "default_source", return_value="mic"), \
             mock.patch.object(calibration, "clock_synchronized", return_value=False):
            job = calibration.AcousticCalibration([target], capture_func=self._capture, play_func=played.append)
            result = job.run()
        self.assertFalse(played)
        self.assertTrue(any("NTP-synchronized" in error for error in result["errors"]))

    def test_muted_microphone_is_reported(self) -> None:
        target = Target("a")
        with mock.patch.object(calibration, "list_physical_sources", return_value=["mic"]), \
             mock.patch.object(calibration, "default_source", return_value="mic"), \
             mock.patch.object(calibration, "source_muted", return_value=True):
            job = calibration.AcousticCalibration([target], play_func=lambda _level: None)
            result = job.run()
        self.assertTrue(any("muted" in error for error in result["errors"]))

    def test_cancel_before_capture_returns_failure(self) -> None:
        job = calibration.AcousticCalibration([Target("a")], play_func=lambda _level: None)
        job.cancel()
        with mock.patch.object(calibration, "list_physical_sources", return_value=["mic"]), \
             mock.patch.object(calibration, "default_source", return_value="mic"), \
             mock.patch.object(calibration, "clock_synchronized", return_value=True), \
             mock.patch.object(calibration, "source_muted", return_value=False):
            result = job.run()
        self.assertFalse(result["ok"])
        self.assertTrue(any("cancelled" in error for error in result["errors"]))


if __name__ == "__main__":
    unittest.main()
