"""GUI guard tests using the real MainWindow methods without a window."""

from __future__ import annotations

import unittest
from unittest import mock

from PySide6.QtWidgets import QDialog

from audiosync import app as app_mod
from audiosync import config, safety
from audiosync.preload import Target


class FakeRadio:
    def __init__(self, checked: bool = False) -> None:
        self.checked = checked
        self.enabled = True
        self.blocked = False

    def isChecked(self) -> bool:
        return self.checked

    def setChecked(self, checked: bool) -> None:
        self.checked = bool(checked)

    def setEnabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)

    def blockSignals(self, blocked: bool) -> None:
        self.blocked = bool(blocked)


class FakeMonitorCheck:
    def __init__(self, checked: bool = True) -> None:
        self.checked = checked

    def blockSignals(self, _blocked: bool) -> None:
        pass

    def setChecked(self, checked: bool) -> None:
        self.checked = bool(checked)


class GuiRoutingGuardTests(unittest.TestCase):
    def make_window(self) -> app_mod.MainWindow:
        window = app_mod.MainWindow.__new__(app_mod.MainWindow)
        window.cfg = config.Config(playback="synced")
        window.cfg.save = mock.Mock()  # type: ignore[method-assign]
        window.state = app_mod.IDLE
        window._preload_thread = None
        window._preload_stop_thread = None
        window._playing_job = None
        window._calibration_thread = None
        window._calibration_cancelled = False
        window._acoustic_next = 0.0
        window._acoustic_note = ""
        window._notice = ""
        window._set_notice = lambda text, seconds=5.0: setattr(window, "_notice", text)  # type: ignore[method-assign]
        window._refresh_acoustic_label = lambda: None  # type: ignore[method-assign]
        window.monitor_check = FakeMonitorCheck(True)  # type: ignore[assignment]
        window.play_radios = {
            "synced": FakeRadio(True),  # type: ignore[dict-item]
            "direct": FakeRadio(False),  # type: ignore[dict-item]
            "off": FakeRadio(False),  # type: ignore[dict-item]
        }
        return window

    def test_playback_radios_are_reverted_while_hosting(self) -> None:
        window = self.make_window()
        window.state = app_mod.HOSTING
        window.play_radios["synced"].setChecked(False)
        window.play_radios["direct"].setChecked(True)
        window._on_playback_changed()
        self.assertEqual(window.cfg.playback, "synced")
        self.assertTrue(window.play_radios["synced"].isChecked())
        self.assertFalse(window.play_radios["direct"].isChecked())
        self.assertIn("Stop", window._notice)

    def test_preload_is_refused_while_hosting(self) -> None:
        window = self.make_window()
        window.state = app_mod.HOSTING
        window._alert = mock.Mock()  # type: ignore[method-assign]
        window._on_preload()
        window._alert.assert_called_once()
        self.assertIn("idle", window._alert.call_args.args[0].lower())

    def test_preload_is_refused_while_monitoring_is_enabled(self) -> None:
        window = self.make_window()
        window.cfg.acoustic_monitor = True
        window._alert = mock.Mock()  # type: ignore[method-assign]
        window._on_preload()
        window._alert.assert_called_once()
        self.assertIn("monitoring", window._alert.call_args.args[0].lower())

    def test_switching_away_from_synced_stops_acoustic_monitoring(self) -> None:
        window = self.make_window()
        window.cfg.acoustic_monitor = True
        window.monitor_check.checked = True  # type: ignore[attr-defined]
        window.play_radios["synced"].setChecked(False)
        window.play_radios["direct"].setChecked(True)
        window._on_playback_changed()
        self.assertEqual(window.cfg.playback, "direct")
        self.assertFalse(window.cfg.acoustic_monitor)
        self.assertFalse(window.monitor_check.checked)  # type: ignore[attr-defined]
        self.assertIn("Synced", window._notice)
        window.cfg.save.assert_called_once()

    def test_preload_is_refused_during_acoustic_work(self) -> None:
        window = self.make_window()
        window._alert = mock.Mock()  # type: ignore[method-assign]
        window._playing_job = object()
        window._on_preload()
        window._alert.assert_called_once()
        window._playing_job = None
        window._calibration_thread = object()
        window._on_preload()
        self.assertEqual(window._alert.call_count, 2)

    def test_start_is_refused_while_preload_is_active(self) -> None:
        window = self.make_window()
        window._alert = mock.Mock()  # type: ignore[method-assign]
        window._playing_job = object()
        window._start_host()
        window._alert.assert_called_once()
        window._alert.reset_mock()
        window._join("host.example")
        window._alert.assert_called_once()

    def test_calibration_refuses_direct_playback(self) -> None:
        window = self.make_window()
        window.state = app_mod.HOSTING
        window.cfg.playback = "direct"
        window._alert = mock.Mock()  # type: ignore[method-assign]
        window._on_calibrate()
        window._alert.assert_called_once()
        self.assertIn("Synced", window._alert.call_args.args[1])

    def test_gui_calibrate_slot_refuses_duplicate_local_and_ssh_targets(self) -> None:
        window = self.make_window()
        window.state = app_mod.HOSTING
        window._last_rows = [
            {"name": config.device_name(), "host": "?", "connected": True},
            {"name": "remote-view", "host": "localhost", "connected": True},
        ]
        window._alert = mock.Mock()  # type: ignore[method-assign]
        window._on_calibrate()
        window._alert.assert_called_once()
        self.assertEqual(window._alert.call_args.args[0], "Unsafe acoustic targets")
        self.assertIn("more than one local", window._alert.call_args.args[1])
        self.assertIsNone(window._calibration_thread)

    def test_unsafe_microphone_choice_is_not_saved(self) -> None:
        window = self.make_window()

        class FakeDialog:
            def __init__(self, *_args, **_kwargs) -> None:
                pass

            def exec(self) -> QDialog.DialogCode:
                return QDialog.DialogCode.Accepted

            def values(self) -> dict[str, str]:
                return {"desk": "audiosync_sink.monitor"}

        window._alert = mock.Mock()  # type: ignore[method-assign]
        with mock.patch.object(app_mod, "MicrophoneDialog", FakeDialog):
            window._open_microphone_dialog([Target("desk")], {"desk": ["alsa_input.usb-mic"]})
        window._alert.assert_called_once()
        self.assertEqual(window.cfg.mic_sources, {})
        window.cfg.save.assert_not_called()

    def test_microphone_choice_must_come_from_the_verified_source_list(self) -> None:
        window = self.make_window()

        class FakeDialog:
            def __init__(self, *_args, **_kwargs) -> None:
                pass

            def exec(self) -> QDialog.DialogCode:
                return QDialog.DialogCode.Accepted

            def values(self) -> dict[str, str]:
                return {"desk": "alsa_input.spoofed"}

        window._alert = mock.Mock()  # type: ignore[method-assign]
        with mock.patch.object(app_mod, "MicrophoneDialog", FakeDialog):
            window._open_microphone_dialog([Target("desk")], {"desk": ["alsa_input.usb-mic"]})
        window._alert.assert_called_once()
        self.assertIn("verified", window._alert.call_args.args[1])
        self.assertEqual(window.cfg.mic_sources, {})
        window.cfg.save.assert_not_called()


class GuiRouteReasonTests(unittest.TestCase):
    def test_route_reason_blocks_preload_and_non_synced_modes(self) -> None:
        window = GuiRoutingGuardTests().make_window()
        window.state = app_mod.HOSTING
        window._playing_job = object()
        self.assertIn("preload", window._acoustic_route_reason().lower())
        window._playing_job = None
        window.cfg.playback = "off"
        self.assertIn("Synced", window._acoustic_route_reason())
        self.assertEqual(safety.validate_playback("synced"), "synced")


if __name__ == "__main__":
    unittest.main()
