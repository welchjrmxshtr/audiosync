"""Fail-closed routing policy tests; no audio or systemd access."""

from __future__ import annotations

import unittest

from audiosync import acoustic, safety
from audiosync.preload import Target


class PlaybackPolicyTests(unittest.TestCase):
    def test_supported_modes_round_trip(self) -> None:
        for mode in safety.PLAYBACK_MODES:
            self.assertEqual(safety.validate_playback(f" {mode.upper()} "), mode)

    def test_unknown_playback_never_falls_through(self) -> None:
        for value in (None, "", "monitor", "default", "synced "):
            if value == "synced ":
                self.assertEqual(safety.validate_playback(value), "synced")
                continue
            with self.assertRaises(safety.RoutingError):
                safety.validate_playback(value)

    def test_client_sink_must_be_real_hardware(self) -> None:
        for sink in ("alsa_output.pci-0000_00_1f.3.analog-stereo", "bluez_output.00", "hw:PCH,0"):
            self.assertEqual(safety.safe_client_sink(sink), sink)
        with self.assertRaises(safety.RoutingError):
            safety.safe_client_sink("hw:PCH,0", allow_alsa_device=False)
        for sink in (
            "",
            "audiosync_sink",
            "audiosync_sink.monitor",
            "auto_null",
            "easyeffects_sink",
            "alsa_output.virtual",
            "alsa_output.loopback",
            "alsa_output.audiosync_sink",
        ):
            with self.assertRaises(safety.RoutingError):
                safety.safe_client_sink(sink)

    def test_direct_capture_never_uses_the_virtual_sink(self) -> None:
        self.assertEqual(safety.safe_direct_capture_sink("alsa_output.usb"), "alsa_output.usb")
        for sink in ("", "audiosync_sink", "auto_null", "alsa_output.virtual", "alsa_output.audiosync_sink"):
            with self.assertRaises(safety.RoutingError):
                safety.safe_direct_capture_sink(sink)


class SourcePolicyTests(unittest.TestCase):
    def test_physical_source_is_allowed(self) -> None:
        info = acoustic.SourceInfo("alsa_input.usb-mic", metadata=True)
        self.assertEqual(safety.safe_capture_source("alsa_input.usb-mic", info), "alsa_input.usb-mic")

    def test_monitor_and_virtual_metadata_are_rejected(self) -> None:
        cases = [
            acoustic.SourceInfo("monitor-name", monitor_of="alsa_output.speakers", metadata=True),
            acoustic.SourceInfo("class-monitor", device_class="monitor", metadata=True),
            acoustic.SourceInfo("virtual-name", virtual=True, metadata=True),
        ]
        for info in cases:
            with self.assertRaises(safety.RoutingError):
                safety.safe_capture_source(info.name, info)

    def test_obvious_loopback_names_are_rejected_without_metadata(self) -> None:
        for name in ("alsa_output.device.monitor", "audiosync_sink.monitor", "auto_null.monitor"):
            self.assertTrue(safety.is_loopback_source_name(name))
            with self.assertRaises(safety.RoutingError):
                safety.safe_capture_source(name, require_metadata=False)
        self.assertFalse(safety.is_loopback_source_name("alsa_input.usb-mic"))

    def test_unknown_metadata_fails_closed(self) -> None:
        with self.assertRaises(safety.RoutingError):
            safety.safe_capture_source("alsa_input.usb-mic")
        with self.assertRaises(safety.RoutingError):
            safety.safe_capture_source("alsa_input.usb-mic", acoustic.SourceInfo("alsa_input.usb-mic"))


class TargetPolicyTests(unittest.TestCase):
    def test_unique_targets_are_returned(self) -> None:
        targets = [Target("a"), Target("b", "b-host")]
        self.assertEqual(safety.unique_targets(targets, {"this-host"}), targets)

    def test_duplicate_names_or_hosts_are_rejected(self) -> None:
        with self.assertRaises(safety.RoutingError):
            safety.unique_targets([Target("a"), Target("a", "a-host")])
        with self.assertRaises(safety.RoutingError):
            safety.unique_targets([Target("a", "same-host"), Target("b", "same-host")])

    def test_two_local_views_are_rejected(self) -> None:
        with self.assertRaises(safety.RoutingError):
            safety.unique_targets(
                [Target("a"), Target("b", "localhost")],
                {"localhost", "127.0.0.1"},
            )


if __name__ == "__main__":
    unittest.main()
