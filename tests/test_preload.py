"""Pure tests for the local-file preload helpers."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from audiosync import library, preload


class PreloadHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.preflight_patch = mock.patch.object(
            preload,
            "preflight_target",
            side_effect=lambda target: (f"alsa_output.{target.name}", ""),
        )
        self.preflight_patch.start()
        self.addCleanup(self.preflight_patch.stop)

    def test_sha256_matches_known_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "sample.bin"
            path.write_bytes(b"audiosync")
            self.assertEqual(preload.sha256_file(path), hashlib.sha256(b"audiosync").hexdigest())

    def test_safe_name_removes_shell_and_path_characters(self) -> None:
        self.assertEqual(preload.safe_name("My Song (final) [1].mp3"), "My_Song_final_1_.mp3")
        self.assertEqual(preload.safe_name("../../etc/passwd"), "passwd")
        self.assertEqual(preload.safe_name(""), "media")

    def test_cache_path_is_home_relative_and_digest_scoped(self) -> None:
        rel = preload.cache_relpath("abc123", "My Film.mkv")
        self.assertEqual(rel, ".cache/audiosync/preload/abc123/My_Film.mkv")
        self.assertFalse(rel.startswith("/"))

    def test_player_argv_keeps_paths_and_start_time(self) -> None:
        argv = preload.player_argv(
            "/tmp/My Film.mkv", 1234.56789, True, "/tmp/player.pid", "alsa_output.safe"
        )
        self.assertEqual(argv[:2], ["python3", "-c"])
        self.assertEqual(
            argv[-6:-1],
            ["/tmp/My Film.mkv", "1234.568", "1", "/tmp/player.pid", "alsa_output.safe"],
        )
        self.assertEqual(argv[-1], preload.PLAYER_MARKER)
        self.assertIn("base64", argv[2])

    def test_player_script_prerolls_before_playing(self) -> None:
        self.assertIn('Gst.State.PAUSED', preload.PLAYER_SCRIPT)
        self.assertIn('Gst.State.PLAYING', preload.PLAYER_SCRIPT)
        self.assertIn('ASYNC_DONE', preload.PLAYER_SCRIPT)
        self.assertIn('waylandsink', preload.PLAYER_SCRIPT)
        self.assertIn('pulsesink', preload.PLAYER_SCRIPT)
        self.assertNotIn('autoaudiosink', preload.PLAYER_SCRIPT)
        self.assertIn('no hardware playback sink selected', preload.PLAYER_SCRIPT)
        self.assertIn('pidfile', preload.PLAYER_SCRIPT)
        self.assertIn('atexit', preload.PLAYER_SCRIPT)

    def test_stop_kills_only_a_marked_local_player(self) -> None:
        # The player publishes its own PID file, and only once its marked argv
        # is visible in /proc (argv is immutable after exec).  Writing it from
        # the parent instead races the child's exec: /proc/<pid>/cmdline can
        # still be empty, which _kill_pidfile correctly reads as "not ours".
        child = (
            "import os, sys, time\n"
            "marker = sys.argv[2].encode()\n"
            "if marker not in open('/proc/self/cmdline', 'rb').read():\n"
            "    raise SystemExit(3)\n"
            "with open(sys.argv[1], 'w', encoding='utf-8') as handle:\n"
            "    handle.write(str(os.getpid()))\n"
            "time.sleep(30)\n"
        )
        unmarked_child = (
            "import os, sys, time\n"
            "with open(sys.argv[1], 'w', encoding='utf-8') as handle:\n"
            "    handle.write(str(os.getpid()))\n"
            "time.sleep(30)\n"
        )
        with tempfile.TemporaryDirectory() as tempdir:
            pidfile = Path(tempdir) / "player.pid"
            other_pidfile = Path(tempdir) / "other.pid"
            marked = subprocess.Popen(
                [sys.executable, "-c", child, str(pidfile), preload.PLAYER_MARKER]
            )
            other = subprocess.Popen(
                [sys.executable, "-c", unmarked_child, str(other_pidfile)]
            )
            try:
                deadline = time.time() + 10
                while time.time() < deadline and not (pidfile.exists() and other_pidfile.exists()):
                    time.sleep(0.01)
                self.assertTrue(pidfile.exists(), "marked player never published its PID")
                self.assertTrue(other_pidfile.exists(), "unmarked process never published its PID")
                self.assertEqual(preload._kill_pidfile(pidfile), 1)
                marked.wait(timeout=5)
                self.assertFalse(pidfile.exists())
                self.assertEqual(preload._kill_pidfile(other_pidfile), 0)
                self.assertIsNone(other.poll())
            finally:
                for process in (marked, other):
                    if process.poll() is None:
                        process.kill()
                        process.wait()

    def test_rtp_selector_only_matches_the_legacy_audiosync_signature(self) -> None:
        legacy = {
            "name": "libpipewire-module-rtp-source",
            "argument": 'source.port = 46000\nnode.name = "rtp_synced_in"',
        }
        self.assertTrue(preload.is_audiosync_rtp(legacy))
        self.assertFalse(
            preload.is_audiosync_rtp(
                {"name": "libpipewire-module-rtp-source", "argument": 'node.name = "personal"'}
            )
        )
        self.assertFalse(
            preload.is_audiosync_rtp(
                {"name": "libpipewire-module-rtp-source", "argument": "rtp_synced_in port=5000"}
            )
        )

    def test_stop_script_targets_rtp_units_and_players(self) -> None:
        self.assertIn("unload-module", preload.STOP_SCRIPT)
        self.assertIn("pw-cli", preload.STOP_SCRIPT)
        self.assertIn("rtp_synced_in", preload.STOP_SCRIPT)
        self.assertNotIn("RTP Session with", preload.STOP_SCRIPT)
        self.assertIn("snapclient.service", preload.STOP_SCRIPT)
        self.assertIn("audiosync-mpv.sock", preload.STOP_SCRIPT)
        # argv can read empty right after exec; the stop must settle it and
        # keep the PID file rather than drop a player it cannot attribute.
        self.assertIn("read_cmdline", preload.STOP_SCRIPT)
        self.assertIn('state == "Z"', preload.STOP_SCRIPT)
        self.assertIn("Never signal a PID we cannot attribute", preload.STOP_SCRIPT)

    def test_local_copy_is_a_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "tone.wav"
            path.write_bytes(b"RIFF")
            target = preload.Target("this device")
            self.assertEqual(preload.copy_to(target, path, "unused"), str(path.resolve()))

    def test_job_rejects_missing_media(self) -> None:
        job = preload.Job("/definitely/missing.mp4", [preload.Target("this device")])
        with self.assertRaises(preload.PreloadError):
            job.run()

    def test_remote_relpath_uses_the_library_inside_a_synced_root(self) -> None:
        lib = library.Library("/mnt/storage/Media", "smb:storage/Media", "SMB storage / Media")
        inside = "/mnt/storage/Media/Album/a.mp3"
        self.assertEqual(
            preload.remote_relpath(inside, "abc", lib),
            ".cache/audiosync/library/"
            + library.library_id("smb:storage/Media")
            + "/Album/a.mp3",
        )
        self.assertEqual(
            preload.remote_relpath("/home/user/Music/b.mp3", "abc"),
            ".cache/audiosync/preload/abc/b.mp3",
        )

    def _run_library_job(self, root: Path, track: Path, digest: str, targets):
        """Run a library preload with the transfer and player launch stubbed."""
        lib = library.Library(str(root), "smb:storage/Media", "SMB storage / Media")
        messages: list[str] = []
        with mock.patch.object(preload, "probe_targets", return_value=(targets[1:], [])), \
                mock.patch.object(preload, "remote_sha256", return_value=digest) as remote, \
                mock.patch.object(preload, "copy_to", return_value="copied") as copy, \
                mock.patch.object(preload, "start", return_value=[]) as start:
            job = preload.Job(track, targets, delay_s=0, library=lib)
            job.run(messages.append)
        return lib, messages, remote, copy, start

    def test_job_reuses_a_matching_synced_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "Media"
            root.mkdir()
            track = root / "Album" / "a.mp3"
            track.parent.mkdir()
            track.write_bytes(b"RIFF")
            digest = preload.sha256_file(track)
            targets = [preload.Target("this device"), preload.Target("laptop", "laptop.local")]
            lib, messages, remote, copy, start = self._run_library_job(
                root, track, digest, targets
            )
            remote.assert_called_once_with(targets[1], lib.remote_relpath(track))
            # Only the local, already-present file goes through copy_to.
            self.assertEqual([call.args[0].name for call in copy.call_args_list], ["this device"])
            self.assertTrue(any("using synced copy" in message for message in messages))
            self.assertEqual(start.call_args.kwargs["remote_rel"], lib.remote_relpath(track))

    def test_job_sends_only_the_changed_file_when_the_copy_is_stale(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "Media"
            root.mkdir()
            track = root / "a.mp3"
            track.write_bytes(b"RIFF")
            targets = [preload.Target("this device"), preload.Target("laptop", "laptop.local")]
            lib = library.Library(str(root), "smb:storage/Media", "SMB storage / Media")
            messages: list[str] = []
            with mock.patch.object(preload, "probe_targets", return_value=(targets[1:], [])), \
                    mock.patch.object(preload, "remote_sha256", return_value="0" * 64), \
                    mock.patch.object(preload, "copy_to", return_value="copied") as copy, \
                    mock.patch.object(preload, "start", return_value=[]):
                preload.Job(track, targets, delay_s=0, library=lib).run(messages.append)
            remote_calls = [call for call in copy.call_args_list if call.args[0].host]
            self.assertEqual(len(remote_calls), 1)
            self.assertEqual(remote_calls[0].kwargs["rel"], lib.remote_relpath(track))
            self.assertTrue(any("stale" in message for message in messages))

    def test_job_without_a_library_keeps_the_per_digest_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            track = Path(temp) / "a.mp3"
            track.write_bytes(b"RIFF")
            targets = [preload.Target("this device"), preload.Target("laptop", "laptop.local")]
            messages: list[str] = []
            with mock.patch.object(preload, "probe_targets", return_value=(targets[1:], [])), \
                    mock.patch.object(preload, "remote_sha256") as remote, \
                    mock.patch.object(preload, "copy_to", return_value="copied") as copy, \
                    mock.patch.object(preload, "start", return_value=[]):
                preload.Job(track, targets, delay_s=0).run(messages.append)
            remote.assert_not_called()
            remote_calls = [call for call in copy.call_args_list if call.args[0].host]
            self.assertEqual(remote_calls[0].kwargs["rel"], None)

    def test_cancelled_job_does_not_start(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "tone.wav"
            path.write_bytes(b"RIFF")
            job = preload.Job(path, [preload.Target("this device")], delay_s=0)
            job.cancel()
            with self.assertRaises(preload.PreloadCancelled):
                job.run()


class PreloadProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.preflight_patch = mock.patch.object(
            preload,
            "preflight_target",
            side_effect=lambda target: (f"alsa_output.{target.name}", ""),
        )
        self.preflight_patch.start()
        self.addCleanup(self.preflight_patch.stop)

    def test_probe_targets_probes_each_remote_once(self) -> None:
        here = preload.Target("here")
        good = preload.Target("good", "good.invalid")
        bad = preload.Target("bad", "bad.invalid")

        def fake(target):
            return "no answer" if target.host == "bad.invalid" else None

        with mock.patch.object(preload, "probe_remote", side_effect=fake) as probe:
            reachable, failures = preload.probe_targets([here, good, bad])
        self.assertEqual(reachable, [good])
        self.assertEqual([target.name for target, _ in failures], ["bad"])
        self.assertEqual(probe.call_count, 2)

    def test_probe_timeout_message_names_the_bound_and_the_cause(self) -> None:
        expired = subprocess.TimeoutExpired("ssh", preload.SSH_PROBE_TIMEOUT_S)
        with mock.patch.object(preload.subprocess, "run", side_effect=expired):
            reason = preload.probe_remote(preload.Target("desk", "desk.invalid"))
        self.assertIn("10s", reason or "")
        self.assertIn("keyless SSH", reason or "")

    def test_tailscale_ssh_interception_is_explained(self) -> None:
        blocked = subprocess.CompletedProcess(
            [], 255, stdout="",
            stderr="# Tailscale SSH requires an additional check.\n",
        )
        with mock.patch.object(preload.subprocess, "run", return_value=blocked):
            reason = preload.probe_remote(preload.Target("desk", "desk.invalid"))
        self.assertIn("Tailscale SSH intercepted", reason or "")

    def test_self_host_is_never_an_ssh_target(self) -> None:
        target = preload.Target("here", socket.gethostname())
        self.assertTrue(preload.is_local_target(target))
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "tone.wav"
            path.write_bytes(b"RIFF")
            with mock.patch.object(preload.subprocess, "run") as run:
                self.assertEqual(preload.copy_to(target, path, "unused"), str(path.resolve()))
            run.assert_not_called()
        self.assertFalse(preload.is_local_target(preload.Target("other", "other.invalid")))

    def test_job_skips_and_names_an_unreachable_device(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "a.mp3"
            path.write_bytes(b"RIFF")
            here = preload.Target("here")
            good = preload.Target("good", "good.invalid")
            bad = preload.Target("bad", "bad.invalid")
            messages: list[str] = []
            with mock.patch.object(
                preload, "probe_targets",
                return_value=([good], [(bad, "SSH to bad.invalid did not answer within 10s")]),
            ), mock.patch.object(preload, "copy_to", return_value="copied"), \
                    mock.patch.object(preload, "start", return_value=[]) as start:
                job = preload.Job(path, [here, good, bad], delay_s=0)
                count = job.run(messages.append)
            self.assertEqual(count, 2)
            self.assertEqual([t.name for t in start.call_args.args[0]], ["here", "good"])
            self.assertEqual(len(job.failures), 1)
            self.assertIn("bad", job.failures[0])
            self.assertTrue(any("skipped" in message for message in messages))

    def test_job_fails_fast_when_no_remote_device_answers(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "a.mp3"
            path.write_bytes(b"RIFF")
            bad = preload.Target("bad", "bad.invalid")
            with mock.patch.object(
                preload, "probe_targets",
                return_value=([], [(bad, "SSH to bad.invalid did not answer within 10s")]),
            ), mock.patch.object(preload, "copy_to") as copy, \
                    mock.patch.object(preload, "start") as start:
                job = preload.Job(path, [preload.Target("here"), bad], delay_s=0)
                with self.assertRaises(preload.PreloadError) as ctx:
                    job.run()
            self.assertIn("bad.invalid", str(ctx.exception))
            copy.assert_not_called()
            start.assert_not_called()


class PreloadSafetyTests(unittest.TestCase):
    def test_incomplete_unit_state_fails_closed(self) -> None:
        target = preload.Target("desk")
        incomplete = subprocess.CompletedProcess([], 0, stdout="active\n", stderr="")
        with mock.patch.object(preload, "_target_control", return_value=incomplete):
            with self.assertRaises(preload.PreloadError):
                preload.active_capture_units(target)

    def test_unknown_unit_state_fails_closed(self) -> None:
        target = preload.Target("desk")
        unknown = subprocess.CompletedProcess([], 0, stdout="unknown\nunknown\n", stderr="")
        with mock.patch.object(preload, "_target_control", return_value=unknown):
            with self.assertRaises(preload.PreloadError):
                preload.active_capture_units(target)

    def test_virtual_default_sink_is_refused(self) -> None:
        target = preload.Target("desk", "desk.invalid")
        with mock.patch.object(preload, "playback_sink", return_value="audiosync_sink"), \
             mock.patch.object(preload, "active_capture_units", return_value=[]):
            sink, error = preload.preflight_target(target)
        self.assertEqual(sink, "")
        self.assertIn("virtual", error)

    def test_active_capture_units_are_refused(self) -> None:
        target = preload.Target("desk", "desk.invalid")
        with mock.patch.object(preload, "playback_sink", return_value="alsa_output.safe"), \
             mock.patch.object(preload, "current_hardware_sinks", return_value={"alsa_output.safe"}), \
             mock.patch.object(preload, "active_capture_units", return_value=["snapserver.service"]):
            sink, error = preload.preflight_target(target)
        self.assertEqual(sink, "")
        self.assertIn("snapserver.service", error)

    def test_safe_target_returns_its_hardware_sink(self) -> None:
        target = preload.Target("desk", "desk.invalid")
        with mock.patch.object(preload, "playback_sink", return_value="alsa_output.safe"), \
             mock.patch.object(preload, "current_hardware_sinks", return_value={"alsa_output.safe"}), \
             mock.patch.object(preload, "active_capture_units", return_value=[]):
            self.assertEqual(preload.preflight_target(target), ("alsa_output.safe", ""))

    def test_stale_default_sink_is_refused(self) -> None:
        target = preload.Target("desk", "desk.invalid")
        with mock.patch.object(preload, "playback_sink", return_value="alsa_output.stale"), \
             mock.patch.object(preload, "current_hardware_sinks", return_value={"alsa_output.safe"}), \
             mock.patch.object(preload, "active_capture_units", return_value=[]):
            sink, error = preload.preflight_target(target)
        self.assertEqual(sink, "")
        self.assertIn("not a current hardware output", error)

    def test_job_refuses_unsafe_target_before_start(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "a.mp3"
            path.write_bytes(b"RIFF")
            target = preload.Target("desk")
            with mock.patch.object(preload, "probe_targets", return_value=([], [])), \
                 mock.patch.object(preload, "preflight_target", return_value=("", "capture is active")), \
                 mock.patch.object(preload, "start") as start:
                job = preload.Job(path, [target], delay_s=0)
                with self.assertRaises(preload.PreloadError):
                    job.run()
            start.assert_not_called()

    def test_start_requires_an_explicit_hardware_sink(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "a.mp3"
            path.write_bytes(b"RIFF")
            target = preload.Target("desk")
            with self.assertRaises(preload.PreloadError):
                preload.start([target], path, "digest", 0.0, False)
            with self.assertRaises(preload.PreloadError):
                preload.start([target], path, "digest", 0.0, False, sinks={"desk": "audiosync_sink"})

    def test_start_passes_the_checked_sink_to_the_player(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "a.mp3"
            path.write_bytes(b"RIFF")
            target = preload.Target("desk")
            with mock.patch.object(preload.subprocess, "Popen", return_value=mock.Mock()) as popen:
                preload.start(
                    [target], path, "digest", 0.0, False, sinks={"desk": "alsa_output.safe"}
                )
            argv = popen.call_args.args[0]
            self.assertEqual(argv[-2], "alsa_output.safe")
            self.assertEqual(argv[-1], preload.PLAYER_MARKER)


class PeerTargetTests(unittest.TestCase):
    def test_announced_lan_address_is_the_host_and_the_name_stays(self) -> None:
        peers = [
            SimpleNamespace(name="laptop-a", address="192.0.2.10"),
            SimpleNamespace(name="laptop-b", address="192.0.2.11"),
        ]
        targets = preload.peer_targets(peers)
        self.assertEqual([t.name for t in targets], ["laptop-a", "laptop-b"])
        self.assertEqual([t.host for t in targets], ["192.0.2.10", "192.0.2.11"])

    def test_this_machine_and_duplicates_are_skipped(self) -> None:
        peers = [
            SimpleNamespace(name=socket.gethostname(), address="192.0.2.9"),
            SimpleNamespace(name="dup", address="192.0.2.10"),
            SimpleNamespace(name="dup", address="192.0.2.10"),
            SimpleNamespace(name="", address=""),
        ]
        self.assertEqual(
            [(t.name, t.host) for t in preload.peer_targets(peers)],
            [("dup", "192.0.2.10")],
        )

    def test_peer_without_an_address_falls_back_to_its_name(self) -> None:
        self.assertEqual(
            [(t.name, t.host) for t in preload.peer_targets([SimpleNamespace(name="laptop", address="")])],
            [("laptop", "laptop")],
        )

    def test_address_only_peer_uses_the_address_for_both(self) -> None:
        targets = preload.peer_targets([SimpleNamespace(name="", address="192.0.2.12")])
        self.assertEqual([(t.name, t.host) for t in targets], [("192.0.2.12", "192.0.2.12")])


class StopRestoreTests(unittest.TestCase):
    def test_stop_script_reports_units_and_starts_them_back(self) -> None:
        self.assertIn("stopped.append(unit)", preload.STOP_SCRIPT)
        self.assertIn('"active": stopped', preload.STOP_SCRIPT)
        self.assertIn('"started": started', preload.STOP_SCRIPT)
        self.assertIn('["systemctl", "--user", "start", unit]', preload.STOP_SCRIPT)
        # One invocation: a scan, a settle pause, a second scan for late PIDs.
        self.assertEqual(preload.STOP_SCRIPT.count("scan_players()"), 3)
        self.assertIn("time.sleep(0.25)", preload.STOP_SCRIPT)

    def test_stop_restores_the_units_it_took_down(self) -> None:
        targets = [preload.Target("laptop", "laptop.invalid")]
        stopped = {
            "players": 1,
            "rtp": 0,
            "units": 2,
            "active": ["snapclient.service", "snapcast-capture.service"],
            "started": ["snapclient.service"],
        }
        with mock.patch.object(preload, "_stop_target", return_value=stopped) as stop_target:
            affected = preload.stop(targets)
        self.assertEqual(affected, 1)
        stop_target.assert_called_once_with(
            targets[0], f"{preload.CACHE_ROOT}/*/player.pid", True
        )

    def test_stop_can_leave_the_units_down(self) -> None:
        targets = [preload.Target("laptop", "laptop.invalid")]
        stopped = {
            "players": 0,
            "rtp": 0,
            "units": 1,
            "active": ["snapclient.service"],
            "started": [],
        }
        with mock.patch.object(preload, "_stop_target", return_value=stopped) as stop_target:
            preload.stop(targets, restore=False)
        stop_target.assert_called_once_with(
            targets[0], f"{preload.CACHE_ROOT}/*/player.pid", False
        )

    def test_stop_reports_a_unit_that_did_not_come_back(self) -> None:
        targets = [preload.Target("laptop", "laptop.invalid")]
        stopped = {
            "players": 0,
            "rtp": 0,
            "units": 1,
            "active": ["snapclient.service"],
            "started": [],
        }
        with mock.patch.object(preload, "_stop_target", return_value=stopped):
            with self.assertRaises(preload.PreloadError) as ctx:
                preload.stop(targets)
        self.assertIn("Snapcast did not restart", str(ctx.exception))

    def test_stop_ignores_targets_that_had_no_units_up(self) -> None:
        targets = [preload.Target("laptop", "laptop.invalid")]
        stopped = {"players": 1, "rtp": 0, "units": 0, "active": [], "started": []}
        with mock.patch.object(preload, "_stop_target", return_value=stopped):
            affected = preload.stop(targets)
        self.assertEqual(affected, 1)

    def test_stop_targets_every_device_once_in_parallel(self) -> None:
        targets = [
            preload.Target("one", "one.invalid"),
            preload.Target("two", "two.invalid"),
        ]
        stopped = {"players": 1, "rtp": 0, "units": 0, "active": [], "started": []}
        with mock.patch.object(preload, "_stop_target", return_value=stopped) as stop_target:
            affected = preload.stop(targets)
        self.assertEqual(affected, 2)
        self.assertEqual(stop_target.call_count, 2)

    def test_stop_reports_an_unreachable_device(self) -> None:
        targets = [preload.Target("laptop", "laptop.invalid")]
        with mock.patch.object(preload, "_stop_target", side_effect=preload.PreloadError("down")):
            with self.assertRaises(preload.PreloadError) as ctx:
                preload.stop(targets)
        self.assertIn("laptop: down", str(ctx.exception))

    def test_stop_retries_a_device_once_after_a_transient_error(self) -> None:
        targets = [preload.Target("laptop", "laptop.invalid")]
        stopped = {"players": 1, "rtp": 0, "units": 0, "active": [], "started": []}
        with mock.patch.object(
            preload,
            "_stop_target",
            side_effect=[preload.PreloadError("timeout"), stopped],
        ) as stop_target:
            with mock.patch.object(preload.time, "sleep"):
                affected = preload.stop(targets)
        self.assertEqual(affected, 1)
        self.assertEqual(stop_target.call_count, 2)

    def test_stop_reports_a_device_that_fails_on_both_attempts(self) -> None:
        targets = [preload.Target("laptop", "laptop.invalid")]
        with mock.patch.object(
            preload,
            "_stop_target",
            side_effect=[preload.PreloadError("timeout"), preload.PreloadError("timeout")],
        ) as stop_target:
            with mock.patch.object(preload.time, "sleep"):
                with self.assertRaises(preload.PreloadError) as ctx:
                    preload.stop(targets)
        self.assertIn("laptop: timeout", str(ctx.exception))
        self.assertEqual(stop_target.call_count, 2)

    def test_stop_uses_the_digest_scoped_pattern_when_given(self) -> None:
        targets = [preload.Target("laptop", "laptop.invalid")]
        stopped = {"players": 0, "rtp": 0, "units": 0, "active": [], "started": []}
        with mock.patch.object(preload, "_stop_target", return_value=stopped) as stop_target:
            preload.stop(targets, digest="abc123")
        stop_target.assert_called_once_with(
            targets[0], preload.cache_relpath("abc123", "player.pid"), True
        )


class StopScriptBehaviourTests(unittest.TestCase):
    """Run the real stop script with systemd/D-Bus tools neutralised."""

    MARKED_CHILD = (
        "import os, sys, time\n"
        "marker = sys.argv[2].encode()\n"
        "if marker not in open('/proc/self/cmdline', 'rb').read():\n"
        "    raise SystemExit(3)\n"
        "with open(sys.argv[1], 'w', encoding='utf-8') as handle:\n"
        "    handle.write(str(os.getpid()))\n"
        "time.sleep(30)\n"
    )
    PLAIN_CHILD = (
        "import os, sys, time\n"
        "with open(sys.argv[1], 'w', encoding='utf-8') as handle:\n"
        "    handle.write(str(os.getpid()))\n"
        "time.sleep(30)\n"
    )

    def test_stop_script_starts_the_units_it_found_running(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            tools = Path(tempdir) / "bin"
            tools.mkdir()
            log = Path(tempdir) / "systemctl.log"
            systemctl = tools / "systemctl"
            systemctl.write_text(
                "#!/bin/sh\n"
                'printf \'%s\\n\' "$*" >> "$SYSTEMCTL_LOG"\n'
                'case "$*" in\n'
                '  *is-active*) echo active ; exit 0 ;;\n'
                "esac\n"
                "exit 0\n",
                encoding="utf-8",
            )
            systemctl.chmod(0o755)
            for name in ("pactl", "pw-cli"):
                tool = tools / name
                tool.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
                tool.chmod(0o755)
            script = Path(tempdir) / "stop_script.py"
            script.write_text(preload.STOP_SCRIPT, encoding="utf-8")
            env = dict(os.environ)
            env["PATH"] = str(tools) + os.pathsep + env.get("PATH", "")
            env["SYSTEMCTL_LOG"] = str(log)
            result = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    str(Path(tempdir) / "*.pid"),
                    preload.PLAYER_MARKER,
                    "1",
                ],
                capture_output=True,
                text=True,
                timeout=60,
                env=env,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            counts = json.loads(result.stdout.strip().splitlines()[-1])
            units = [
                "snapclient.service",
                "snapcast-capture.service",
                "snapserver.service",
            ]
            self.assertEqual(counts["active"], units)
            self.assertEqual(counts["started"], units)
            lines = log.read_text(encoding="utf-8").splitlines()
            starts = [line for line in lines if " start " in line]
            self.assertEqual(len(starts), len(units))
            for unit in units:
                self.assertTrue(any(line.endswith("start " + unit) for line in starts))

    def test_stop_script_kills_a_player_that_writes_its_pid_late(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            tools = Path(tempdir) / "bin"
            tools.mkdir()
            for name in ("systemctl", "pactl", "pw-cli"):
                tool = tools / name
                tool.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
                tool.chmod(0o755)
            pidfile = Path(tempdir) / "late.pid"
            child = (
                "import os, sys, time\n"
                "marker = sys.argv[2].encode()\n"
                "if marker not in open('/proc/self/cmdline', 'rb').read():\n"
                "    raise SystemExit(3)\n"
                "time.sleep(0.15)\n"
                "with open(sys.argv[1], 'w', encoding='utf-8') as handle:\n"
                "    handle.write(str(os.getpid()))\n"
                "time.sleep(30)\n"
            )
            process = subprocess.Popen(
                [sys.executable, "-c", child, str(pidfile), preload.PLAYER_MARKER]
            )
            try:
                script = Path(tempdir) / "stop_script.py"
                script.write_text(preload.STOP_SCRIPT, encoding="utf-8")
                env = dict(os.environ)
                env["PATH"] = str(tools) + os.pathsep + env.get("PATH", "")
                result = subprocess.run(
                    [
                        sys.executable,
                        str(script),
                        str(Path(tempdir) / "*.pid"),
                        preload.PLAYER_MARKER,
                        "0",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    env=env,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                counts = json.loads(result.stdout.strip().splitlines()[-1])
                self.assertEqual(counts["players"], 1)
                process.wait(timeout=5)
                self.assertEqual(process.returncode, -signal.SIGTERM)
                self.assertFalse(pidfile.exists())
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()

    def test_stop_script_kills_only_its_marked_player(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            tools = Path(tempdir) / "bin"
            tools.mkdir()
            for name in ("systemctl", "pactl", "pw-cli"):
                tool = tools / name
                tool.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
                tool.chmod(0o755)
            pidfile = Path(tempdir) / "marked.pid"
            other_pidfile = Path(tempdir) / "unmarked.pid"
            foreign_pidfile = Path(tempdir) / "foreign.pid"
            marked = subprocess.Popen(
                [sys.executable, "-c", self.MARKED_CHILD, str(pidfile), preload.PLAYER_MARKER]
            )
            other = subprocess.Popen(
                [sys.executable, "-c", self.PLAIN_CHILD, str(other_pidfile)]
            )
            # PID 2 is a kernel thread: alive, but it has no readable argv, so
            # the script must not attribute it to a player and must not signal it.
            foreign_pidfile.write_text("2\n", encoding="utf-8")
            try:
                deadline = time.time() + 10
                while time.time() < deadline and not (pidfile.exists() and other_pidfile.exists()):
                    time.sleep(0.01)
                self.assertTrue(pidfile.exists(), "marked player never published its PID")
                self.assertTrue(other_pidfile.exists(), "plain process never published its PID")
                self.assertEqual(Path("/proc/2/cmdline").read_bytes(), b"")
                script = Path(tempdir) / "stop_script.py"
                script.write_text(preload.STOP_SCRIPT, encoding="utf-8")
                env = dict(os.environ)
                env["PATH"] = str(tools) + os.pathsep + env.get("PATH", "")
                result = subprocess.run(
                    [
                        sys.executable,
                        str(script),
                        str(Path(tempdir) / "*.pid"),
                        preload.PLAYER_MARKER,
                        "0",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    env=env,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                counts = json.loads(result.stdout.strip().splitlines()[-1])
                self.assertEqual(counts["players"], 1)
                marked.wait(timeout=5)
                self.assertEqual(marked.returncode, -signal.SIGTERM)
                self.assertFalse(pidfile.exists())
                self.assertIsNone(other.poll())
                self.assertFalse(other_pidfile.exists())
                # Unattributable PID: never signalled, PID file kept for the
                # next stop pass so it can be attributed then.
                self.assertTrue(foreign_pidfile.exists())
                self.assertTrue(Path("/proc/2").exists())
            finally:
                for process in (marked, other):
                    if process.poll() is None:
                        process.kill()
                        process.wait()


if __name__ == "__main__":
    unittest.main()
