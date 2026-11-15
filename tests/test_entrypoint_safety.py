"""End-to-end refusal tests through the real CLI and backend entry points.

Every subprocess uses a throwaway HOME, config/state directories and a PATH
containing only logging fake control binaries.  A refusal therefore cannot
touch the live Snapcast units, PipeWire routing or the user's config.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from audiosync import backend, calibration, config, safety
from audiosync.preload import Target

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class EntrypointSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.config_dir = self.root / "config"
        self.state_dir = self.root / "state"
        self.runtime_dir = self.root / "runtime"
        self.bin_dir = self.root / "bin"
        self.log = self.root / "control.log"
        for directory in (self.config_dir, self.state_dir, self.runtime_dir, self.bin_dir):
            directory.mkdir(parents=True)
        self._write_fake_binaries()
        self.env = os.environ.copy()
        self.env.update(
            {
                "HOME": str(self.root),
                "AUDIOSYNC_CONFIG_DIR": str(self.config_dir),
                "AUDIOSYNC_STATE_DIR": str(self.state_dir),
                "AUDIOSYNC_SNAPSERVER_STATE": str(self.root / "server.json"),
                "AUDIOSYNC_DEVICE_NAME": "safety-test-host",
                "AUDIOSYNC_FAKE_LOG": str(self.log),
                "XDG_RUNTIME_DIR": str(self.runtime_dir),
                "PATH": str(self.bin_dir),
                "PYTHONPATH": str(PROJECT_ROOT)
                + (os.pathsep + self.env["PYTHONPATH"] if self.env.get("PYTHONPATH") else ""),
            }
        )

    def _write_fake_binaries(self) -> None:
        pactl = """#!/bin/sh
printf 'pactl %s\n' "$*" >> "$AUDIOSYNC_FAKE_LOG"
case "$1" in
  get-default-sink) printf '%s\n' "${AUDIOSYNC_FAKE_DEFAULT_SINK:-}" ;;
  list) printf '%s\n' "${AUDIOSYNC_FAKE_SINKS:-}" ;;
esac
exit 0
"""
        systemctl = """#!/bin/sh
printf 'systemctl %s\n' "$*" >> "$AUDIOSYNC_FAKE_LOG"
exit 1
"""
        journalctl = """#!/bin/sh
exit 0
"""
        for name, body in (("pactl", pactl), ("systemctl", systemctl), ("journalctl", journalctl)):
            path = self.bin_dir / name
            path.write_text(body, encoding="utf-8")
            path.chmod(0o755)

    def _write_config(self, values: dict) -> bytes:
        path = self.config_dir / "config.json"
        path.write_text(json.dumps(values) + "\n", encoding="utf-8")
        return path.read_bytes()

    def _run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        self.log.write_text("", encoding="utf-8")
        return subprocess.run(
            [sys.executable, "-m", "audiosync", *args],
            cwd=PROJECT_ROOT,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=20,
        )

    def _assert_refused(self, values: dict, expected: str, *args: str) -> None:
        original = self._write_config(values)
        result = self._run_cli(*args)
        output = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0, output)
        self.assertIn("error:", output.lower())
        self.assertIn(expected, output)
        self.assertEqual((self.config_dir / "config.json").read_bytes(), original)
        for generated in ("snapserver.conf", "capture.env", "client.env"):
            self.assertFalse((self.config_dir / generated).exists(), generated)
        control = self.log.read_text(encoding="utf-8")
        self.assertNotIn("restart", control)
        self.assertNotIn("start", control)

    def test_invalid_persisted_playback_is_refused(self) -> None:
        self._assert_refused({"playback": "monitor"}, "invalid persisted playback", "--host")

    def test_loopback_microphone_is_refused(self) -> None:
        self._assert_refused(
            {"mic_sources": {"desk": "audiosync_sink.monitor"}},
            "unsafe persisted microphone",
            "--join",
            "host.example",
        )

    def test_virtual_hw_sink_is_refused(self) -> None:
        self._assert_refused(
            {"playback": "synced", "hw_sink": "audiosync_sink"},
            "unsafe persisted hw_sink",
            "--host",
        )

    def test_direct_capture_on_virtual_sink_is_refused(self) -> None:
        self._assert_refused(
            {"playback": "direct", "hw_sink": config.SINK_NAME},
            "unsafe persisted hw_sink",
            "--host",
            "--playback",
            "direct",
        )
        cfg = config.Config(playback="direct", hw_sink=config.SINK_NAME)
        with self.assertRaises(backend.BackendError) as caught:
            backend.Backend(cfg).capture_target("direct")
        self.assertIn("virtual", str(caught.exception).lower())

    def test_empty_hw_sink_is_refused_before_units(self) -> None:
        self._assert_refused(
            {"playback": "direct", "hw_sink": ""},
            "empty persisted hw_sink",
            "--host",
            "--playback",
            "direct",
        )

    def test_duplicate_local_and_ssh_targets_are_refused(self) -> None:
        targets = [Target("local-device"), Target("remote-view", "localhost")]
        with self.assertRaises(safety.RoutingError) as caught:
            safety.unique_targets(targets, {"localhost"})
        self.assertIn("more than one local", str(caught.exception))
        with self.assertRaises(safety.RoutingError):
            calibration.AcousticCalibration(targets)

    def test_safe_modes_and_real_hardware_are_accepted(self) -> None:
        hardware = "alsa_output.pci-0000_00_1f.3.analog-stereo"
        for playback in config.PLAYBACK_MODES:
            self._write_config({"playback": playback, "hw_sink": hardware})
            with mock.patch.dict(os.environ, {"AUDIOSYNC_CONFIG_DIR": str(self.config_dir)}):
                cfg = config.Config.load(strict=True)
                transport = backend.Backend(cfg)
                with mock.patch.object(
                    transport,
                    "_sinks_text",
                    return_value=f"Sink #1\n\tName: {hardware}\n",
                ):
                    target = transport.capture_target(playback)
                    transport.write_client_env("host.example")
                values = config.read_env_file(config.client_env_path())
            self.assertEqual(cfg.playback, playback)
            self.assertEqual(target, hardware if playback == "direct" else config.SINK_NAME)
            self.assertIn("--soundcard", values["AUDIOSYNC_ARGS"])
            self.assertIn(hardware, values["AUDIOSYNC_ARGS"])


if __name__ == "__main__":
    unittest.main()