"""Persistence tests for the acoustic settings."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from audiosync import config


class AcousticConfigTests(unittest.TestCase):
    def test_new_settings_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            with mock.patch.dict(os.environ, {"AUDIOSYNC_CONFIG_DIR": root}):
                cfg = config.Config(
                    mic_sources={"laptop": "alsa_input.usb-mic"},
                    client_latency_ms={"laptop": 37},
                    acoustic_monitor=True,
                    acoustic_interval_s=45,
                    acoustic_volume=31,
                )
                cfg.save()
                loaded = config.Config.load()
        self.assertEqual(loaded.mic_sources, {"laptop": "alsa_input.usb-mic"})
        self.assertEqual(loaded.client_latency_ms, {"laptop": 37})
        self.assertTrue(loaded.acoustic_monitor)
        self.assertEqual(loaded.acoustic_interval_s, 45)
        self.assertEqual(loaded.acoustic_volume, 31)

    def test_bad_acoustic_values_are_sanitized(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "config.json"
            path.write_text(
                '{"mic_sources": [], "client_latency_ms": {"a": "bad", "b": -2}, '
                '"acoustic_interval_s": 1, "acoustic_volume": 900}',
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"AUDIOSYNC_CONFIG_DIR": root}):
                loaded = config.Config.load()
        self.assertEqual(loaded.mic_sources, {})
        self.assertEqual(loaded.client_latency_ms, {"b": 0})
        self.assertEqual(loaded.acoustic_interval_s, 10)
        self.assertEqual(loaded.acoustic_volume, 100)

    def test_a_non_boolean_monitor_value_is_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "config.json"
            path.write_text('{"acoustic_monitor": "true"}', encoding="utf-8")
            with mock.patch.dict(os.environ, {"AUDIOSYNC_CONFIG_DIR": root}):
                loaded = config.Config.load()
        self.assertFalse(loaded.acoustic_monitor)

    def test_monitor_sources_are_never_loaded_as_microphones(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "config.json"
            path.write_text('{"mic_sources": {"a": "alsa_output.device.monitor", "b": "alsa_input.mic"}}', encoding="utf-8")
            with mock.patch.dict(os.environ, {"AUDIOSYNC_CONFIG_DIR": root}):
                loaded = config.Config.load()
        self.assertEqual(loaded.mic_sources, {"b": "alsa_input.mic"})

    def test_invalid_playback_and_virtual_sink_are_sanitized(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "config.json"
            path.write_text(
                '{"playback": "monitor", "hw_sink": "audiosync_sink"}',
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"AUDIOSYNC_CONFIG_DIR": root}):
                loaded = config.Config.load()
        self.assertEqual(loaded.playback, "synced")
        self.assertEqual(loaded.hw_sink, "")


if __name__ == "__main__":
    unittest.main()
