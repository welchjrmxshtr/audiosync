"""Backend route enforcement tests; no units or audio devices are started."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from audiosync import backend, config

HARDWARE_SINK = "alsa_output.pci-0000_00_1f.3.analog-stereo"


def sink_text(*names: str) -> str:
    return "\n".join(
        f"Sink #{index}\n\tName: {name}"
        for index, name in enumerate(names, start=1)
    )


class BackendRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = config.Config(hw_sink=HARDWARE_SINK)
        self.transport = backend.Backend(self.cfg)

    def test_invalid_playback_is_rejected_by_capture_target(self) -> None:
        with self.assertRaises(backend.BackendError):
            self.transport.capture_target("monitor")

    def test_synced_and_off_capture_the_virtual_sink(self) -> None:
        for mode in ("synced", "off"):
            self.assertEqual(self.transport.capture_target(mode), config.SINK_NAME)

    def test_direct_capture_requires_a_real_sink(self) -> None:
        with mock.patch.object(self.transport, "_sinks_text", return_value=sink_text(HARDWARE_SINK)):
            self.assertEqual(self.transport.capture_target("direct"), self.cfg.hw_sink)
        self.cfg.hw_sink = config.SINK_NAME
        with self.assertRaises(backend.BackendError):
            self.transport.capture_target("direct")

    def test_direct_capture_refuses_a_stale_hardware_looking_sink(self) -> None:
        self.cfg.hw_sink = "alsa_output.stale-device"
        with mock.patch.object(self.transport, "_sinks_text", return_value=""):
            with self.assertRaises(backend.BackendError):
                self.transport.capture_target("direct")

    def test_client_env_always_contains_an_explicit_hardware_sink(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            with mock.patch.dict(os.environ, {"AUDIOSYNC_CONFIG_DIR": root}):
                with mock.patch.object(
                    self.transport, "_sinks_text", return_value=sink_text(HARDWARE_SINK)
                ):
                    self.transport.write_client_env("host.example")
                values = config.read_env_file(config.client_env_path())
        self.assertEqual(values["AUDIOSYNC_HOST"], "host.example")
        self.assertIn("--soundcard", values["AUDIOSYNC_ARGS"])
        self.assertIn(self.cfg.hw_sink, values["AUDIOSYNC_ARGS"])

    def test_client_env_refuses_to_fall_back_to_the_virtual_sink(self) -> None:
        self.cfg.hw_sink = ""
        with tempfile.TemporaryDirectory() as root:
            with mock.patch.dict(os.environ, {"AUDIOSYNC_CONFIG_DIR": root}):
                with self.assertRaises(backend.BackendError):
                    self.transport.write_client_env("host.example")
                self.assertFalse(config.client_env_path().exists())

    def test_client_env_refuses_a_stale_hardware_looking_sink(self) -> None:
        self.cfg.hw_sink = "alsa_output.stale-device"
        with tempfile.TemporaryDirectory() as root:
            with mock.patch.dict(os.environ, {"AUDIOSYNC_CONFIG_DIR": root}):
                with mock.patch.object(
                    self.transport, "_sinks_text", return_value=sink_text(HARDWARE_SINK)
                ):
                    with self.assertRaises(backend.BackendError):
                        self.transport.write_client_env("host.example")
                self.assertFalse(config.client_env_path().exists())

    def test_alsa_player_uses_and_accepts_its_hardware_device(self) -> None:
        self.cfg.player = " ALSA "
        self.cfg.output_device = "hw:PCH,0"
        with tempfile.TemporaryDirectory() as root:
            with mock.patch.dict(os.environ, {"AUDIOSYNC_CONFIG_DIR": root}):
                self.transport.write_client_env("host.example")
                values = config.read_env_file(config.client_env_path())
        self.assertIn("--player alsa", values["AUDIOSYNC_ARGS"])
        self.assertIn("hw:PCH,0", values["AUDIOSYNC_ARGS"])

    def test_host_refuses_an_unsafe_route_before_touching_units(self) -> None:
        self.cfg.hw_sink = ""
        with mock.patch.object(self.transport, "detect_hw_sink", return_value=""), \
             mock.patch.object(backend, "systemctl") as systemctl:
            with self.assertRaises(backend.BackendError):
                self.transport.host(250, "pcm", "synced")
        systemctl.assert_not_called()


if __name__ == "__main__":
    unittest.main()
