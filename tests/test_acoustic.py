"""Synthetic acoustic signal tests; no microphone or live audio is required."""

from __future__ import annotations

import unittest
from unittest import mock

from audiosync import acoustic


def _delayed(pcm, delay_s: float) -> list[int]:
    return [0] * int(round(acoustic.SAMPLE_RATE * delay_s)) + list(pcm)


class ProbeDetectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pcm = acoustic.probe_samples(20)

    def test_generated_probe_finds_all_three_pulses(self) -> None:
        detection = acoustic.detect_probe(self.pcm)
        self.assertTrue(detection.valid)
        self.assertFalse(detection.skipped)
        self.assertEqual(len(detection.onsets_s), 3)
        for expected, actual in zip(acoustic.pulse_onsets_s(), detection.onsets_s):
            self.assertAlmostEqual(expected, actual, delta=0.005)
        self.assertGreater(detection.confidence, 0.5)

    def test_known_delay_is_preserved(self) -> None:
        detection = acoustic.detect_probe(_delayed(self.pcm, 0.137))
        self.assertTrue(detection.valid)
        for expected, actual in zip(acoustic.pulse_onsets_s(), detection.onsets_s):
            self.assertAlmostEqual(expected + 0.137, actual, delta=0.005)

    def test_silence_is_rejected(self) -> None:
        detection = acoustic.detect_probe([0] * (acoustic.SAMPLE_RATE * 2))
        self.assertFalse(detection.valid)
        self.assertTrue(detection.skipped)
        self.assertEqual(detection.onsets_s, [])

    def test_missing_middle_pulse_is_a_skip(self) -> None:
        samples = list(self.pcm)
        start = int(acoustic.pulse_onsets_s()[1] * acoustic.SAMPLE_RATE)
        end = start + int(0.06 * acoustic.SAMPLE_RATE)
        samples[start:end] = [0] * (end - start)
        detection = acoustic.detect_probe(samples)
        self.assertFalse(detection.valid)
        self.assertTrue(detection.skipped)
        self.assertLess(len(detection.onsets_s), 3)

    def test_extra_early_click_does_not_hide_a_matching_triplet(self) -> None:
        samples = list(self.pcm)
        first = int(acoustic.pulse_onsets_s()[0] * acoustic.SAMPLE_RATE)
        pulse_length = int(acoustic.PULSE_MS * acoustic.SAMPLE_RATE / 1000)
        click = int(0.40 * acoustic.SAMPLE_RATE)
        samples[click : click + pulse_length] = samples[first : first + pulse_length]
        detection = acoustic.detect_probe(samples)
        self.assertTrue(detection.valid)
        self.assertEqual(len(detection.onsets_s), acoustic.PULSE_COUNT)
        self.assertAlmostEqual(detection.onsets_s[0], acoustic.pulse_onsets_s()[0], delta=0.005)

    def test_physical_sources_exclude_monitors(self) -> None:
        text = "0\taudiosync_sink.monitor\tPipeWire\tfloat32le\n1\tusb-mic\tPipeWire\ts16le\n2\tmic2\tPipeWire\ts16le\n"
        self.assertEqual(acoustic.physical_sources(text), ["usb-mic", "mic2"])

    def test_verbose_source_metadata_rejects_monitor_and_virtual_entries(self) -> None:
        text = """Source #1
\tName: alsa_output.speakers.monitor
\tMonitor of Sink: alsa_output.speakers
\tProperties:
\t\tdevice.class = "monitor"
Source #2
\tName: alsa_input.usb-mic
\tMonitor of Sink: n/a
\tProperties:
\t\tnode.virtual = "false"
Source #3
\tName: fake-virtual-input
\tProperties:
\t\tnode.virtual = "true"
"""
        self.assertEqual(acoustic.physical_sources(text), ["alsa_input.usb-mic"])

    def test_capture_agent_refuses_an_unverified_source_before_recording(self) -> None:
        with mock.patch.object(acoustic, "source_info", return_value=None), \
             mock.patch.object(acoustic.subprocess, "Popen") as popen:
            result = acoustic.capture_and_detect("alsa_input.usb-mic", duration_s=0.1)
        self.assertTrue(result["ok"] is False)
        self.assertTrue(result["capture_error"])
        self.assertIn("cannot verify", result["error"])
        popen.assert_not_called()

    def test_offsets_use_earliest_valid_arrival(self) -> None:
        results = {
            "a": {"ok": True, "started_ns": 1_000_000_000, "onsets": [0.250, 0.500, 0.750], "skipped": False},
            "b": {"ok": True, "started_ns": 1_000_000_000, "onsets": [0.283, 0.533, 0.783], "skipped": False},
        }
        summary = acoustic.measure_arrivals(results)
        self.assertEqual(summary["reference"], "a")
        self.assertEqual(summary["offsets_ms"], {"a": 0, "b": 33})

    def test_skipped_device_is_not_given_an_offset(self) -> None:
        results = {
            "a": {"ok": True, "started_ns": 1_000_000_000, "onsets": [0.250, 0.500, 0.750], "skipped": False},
            "b": {"ok": False, "started_ns": 0, "onsets": [], "skipped": True, "error": "no pulse"},
        }
        summary = acoustic.measure_arrivals(results)
        self.assertEqual(summary["offsets_ms"], {"a": 0})
        self.assertEqual(summary["skipped"], ["b"])
        self.assertTrue(any("b:" in error for error in summary["errors"]))


if __name__ == "__main__":
    unittest.main()
