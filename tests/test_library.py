"""Tests for SMB-aware library identity, paths, scanning and folder sync."""

from __future__ import annotations

import os
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from audiosync import config, library, preload

MOUNTINFO = (
    "36 25 0:32 / /mnt/smb-share rw,relatime shared:1 - cifs //169.254.99.29/storage "
    "rw,vers=3.1.1\n"
)

TESTPARM = """[global]
	workgroup = WORKGROUP
	server string = %h

[storage]
	path = /mnt/storage
	read only = No
	guest ok = Yes

[printers]
	path = /var/tmp
	printable = Yes
"""


class IdentityTests(unittest.TestCase):
    def test_mountinfo_line_parses_a_cifs_mount(self) -> None:
        mount = library.parse_mountinfo_line(MOUNTINFO.strip())
        assert mount is not None
        self.assertEqual(mount["mountpoint"], "/mnt/smb-share")
        self.assertEqual(mount["fstype"], "cifs")
        self.assertEqual(mount["source"], "//169.254.99.29/storage")

    def test_testparm_share_paths_skip_global(self) -> None:
        shares = library.parse_testparm(TESTPARM)
        self.assertIn(("storage", "/mnt/storage"), shares)
        self.assertNotIn("global", [share for share, _ in shares])

    def test_client_mount_and_server_path_share_one_identity(self) -> None:
        via_mount = library._identity_from_mount(Path("/mnt/smb-share/Media"), MOUNTINFO)
        via_server = library._identity_from_samba(Path("/mnt/storage/Media"), TESTPARM)
        self.assertEqual(via_mount, "smb:storage/Media")
        self.assertEqual(via_server, "smb:storage/Media")
        self.assertEqual(library.library_id(via_mount), library.library_id(via_server))

    def test_share_root_identity_has_no_relative_part(self) -> None:
        self.assertEqual(
            library._identity_from_samba(Path("/mnt/storage"), TESTPARM), "smb:storage"
        )

    def test_gvfs_share_paths_are_recognised(self) -> None:
        path = Path(
            "/run/user/1000/gvfs/smb-share:server=fileserver.local,share=storage/Media"
        )
        self.assertEqual(library._identity_from_gvfs(path), "smb:storage/Media")

    def test_non_cifs_mounts_are_not_smb(self) -> None:
        ext4 = "36 25 0:32 / /mnt/storage rw - ext4 /dev/sdb1 rw\n"
        self.assertIsNone(library._identity_from_mount(Path("/mnt/storage/Media"), ext4))

    def test_label_names_the_share_and_folder(self) -> None:
        self.assertEqual(library.label_for("smb:storage/Media"), "SMB storage / Media")
        self.assertEqual(
            library.label_for("local:/home/user/Music"), "Local /home/user/Music"
        )

    def test_resolve_source_rejects_missing_directory(self) -> None:
        with self.assertRaises(library.LibraryError):
            library.resolve_source("/definitely/missing")


class PathTests(unittest.TestCase):
    def test_safe_relative_rejects_traversal_and_absolute_paths(self) -> None:
        for bad in ("..", "../x", "/etc/passwd", ".", ""):
            with self.assertRaises(library.LibraryError):
                library.safe_relative(bad)
        self.assertEqual(library.safe_relative("Album/song.mp3"), "Album/song.mp3")

    def test_relative_media_path_rejects_a_sibling_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "Media"
            root.mkdir()
            sibling = Path(temp) / "Media2" / "x.mp3"
            sibling.parent.mkdir()
            sibling.write_bytes(b"x")
            with self.assertRaises(library.LibraryError):
                library.relative_media_path(root, sibling)
            inside = root / "x.mp3"
            inside.write_bytes(b"x")
            self.assertEqual(library.relative_media_path(root, inside), "x.mp3")

    def test_remote_relpath_is_library_scoped_and_home_relative(self) -> None:
        lib = library.Library("/mnt/storage/Media", "smb:storage/Media", "SMB storage / Media")
        rel = lib.remote_relpath("/mnt/storage/Media/Album/song.flac")
        self.assertEqual(
            rel,
            ".cache/audiosync/library/"
            + library.library_id("smb:storage/Media")
            + "/Album/song.flac",
        )
        self.assertFalse(rel.startswith("/"))

    def test_find_library_uses_the_longest_root_and_nothing_else(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            outer = base / "Media"
            outer.mkdir()
            inner = outer / "Shows"
            inner.mkdir()
            track = inner / "ep.mp3"
            track.write_bytes(b"x")
            roots = [
                {"path": str(outer), "identity": "smb:storage/Media"},
                {"path": str(inner), "identity": "smb:storage/Media/Shows"},
            ]
            found = library.find_library(track, roots)
            assert found is not None
            self.assertEqual(found.identity, "smb:storage/Media/Shows")
            self.assertIsNone(library.find_library(base / "Elsewhere.mp3", roots))


class ScanTests(unittest.TestCase):
    def test_scan_counts_files_and_skips_temporary_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "Media"
            root.mkdir()
            (root / "a.mp3").write_bytes(b"1234")
            nested = root / "nested"
            nested.mkdir()
            (nested / "b.flac").write_bytes(b"12")
            (root / "._a.mp3").write_bytes(b"x")
            (root / "b.mp3.part").write_bytes(b"x")
            trash = root / ".Trash-1000"
            trash.mkdir()
            (trash / "c.mp3").write_bytes(b"x")
            files, stats = library.scan_directory(root)
            self.assertEqual(files, ["a.mp3", "nested/b.flac"])
            self.assertEqual((stats.files, stats.bytes), (2, 6))

    def test_scan_rejects_a_filesystem_root_or_an_empty_folder(self) -> None:
        with self.assertRaises(library.LibraryError):
            library.scan_directory("/")
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(library.LibraryError):
                library.scan_directory(temp)
            with self.assertRaises(library.LibraryError):
                library.scan_directory(Path(temp) / "missing")


class RsyncTests(unittest.TestCase):
    def test_rsync_args_are_cifs_safe_and_delete_only_this_library(self) -> None:
        target = preload.Target("study-laptop", "192.0.2.13")
        destination = library.library_root("smb:storage/Media")
        argv = library.rsync_argv("/mnt/storage/Media", target, destination)
        self.assertEqual(argv[0], "rsync")
        for flag in ("-rlt", "--partial", "--safe-links", "--delete", "--protect-args"):
            self.assertIn(flag, argv)
        self.assertNotIn("-a", argv)
        self.assertTrue(argv[-2].endswith("/"))
        self.assertEqual(argv[-1], f"192.0.2.13:{destination}")
        self.assertTrue(destination.startswith(".cache/audiosync/library/"))
        self.assertNotIn("/Media", destination)
        ssh_command = argv[argv.index("-e") + 1]
        for option in preload.SSH_OPTIONS:
            self.assertIn(option, ssh_command)


class SyncJobTests(unittest.TestCase):
    def test_job_rejects_a_missing_source(self) -> None:
        with self.assertRaises(library.LibraryError):
            library.FolderSyncJob("/definitely/missing", [preload.Target("this device")])

    def test_cancelled_job_does_not_transfer(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "Media"
            root.mkdir()
            (root / "a.mp3").write_bytes(b"x")
            job = library.FolderSyncJob(root, [preload.Target("this device")])
            job.cancel()
            with self.assertRaises(library.LibrarySyncCancelled):
                job.run()

    def test_local_target_needs_no_transfer(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "Media"
            root.mkdir()
            (root / "a.mp3").write_bytes(b"x")
            messages: list[str] = []
            job = library.FolderSyncJob(root, [preload.Target("this device")])
            self.assertEqual(job.run(messages.append), 0)
            assert job.stats is not None
            self.assertEqual(job.stats.files, 1)
            self.assertTrue(any("local source" in message for message in messages))

    def test_this_machine_is_recognised_and_other_hosts_are_not(self) -> None:
        self.assertTrue(library.is_local_host(socket.gethostname()))
        self.assertTrue(library.is_local_host("localhost"))
        self.assertFalse(library.is_local_host("definitely-remote.invalid"))
        self.assertFalse(library.is_local_host(""))

    def test_this_machine_as_a_target_needs_no_ssh(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "Media"
            root.mkdir()
            (root / "a.mp3").write_bytes(b"x")
            messages: list[str] = []
            job = library.FolderSyncJob(root, [preload.Target("here", socket.gethostname())])
            self.assertEqual(job.run(messages.append), 0)
            self.assertEqual(job.failures, [])
            self.assertTrue(any("local source" in message for message in messages))

    def test_one_unreachable_device_does_not_discard_the_others(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "Media"
            root.mkdir()
            (root / "a.mp3").write_bytes(b"x")
            here, dead, ok = (
                preload.Target("here"),
                preload.Target("dead", "dead-host"),
                preload.Target("ok", "ok-host"),
            )
            with mock.patch.object(
                library, "probe_targets",
                return_value=([ok], [(dead, "SSH to dead-host did not answer")]),
            ), mock.patch.object(library.FolderSyncJob, "_run_remote") as transfer, \
                    mock.patch.object(library, "write_remote_marker"):
                job = library.FolderSyncJob(root, [here, dead, ok])
                copied = job.run()
            self.assertEqual(copied, 1)
            self.assertEqual(len(job.failures), 1)
            self.assertIn("dead", job.failures[0])
            self.assertEqual(
                [call.args[0].name for call in transfer.call_args_list], ["here", "ok"]
            )

    def test_all_unreachable_devices_raise_one_clear_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "Media"
            root.mkdir()
            (root / "a.mp3").write_bytes(b"x")
            dead = preload.Target("dead", "dead-host")
            with mock.patch.object(
                library, "probe_targets",
                return_value=([], [(dead, "SSH to dead-host did not answer within 10s")]),
            ):
                job = library.FolderSyncJob(root, [preload.Target("here"), dead])
                with self.assertRaises(library.LibraryError) as ctx:
                    job.run()
            self.assertIn("dead-host", str(ctx.exception))

    def test_unreachable_devices_are_never_transferred(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "Media"
            root.mkdir()
            (root / "a.mp3").write_bytes(b"x")
            dead1 = preload.Target("dead-1", "dead-1.invalid")
            dead2 = preload.Target("dead-2", "dead-2.invalid")
            with mock.patch.object(
                library, "probe_targets",
                return_value=([], [(dead1, "no answer"), (dead2, "no answer")]),
            ), mock.patch.object(library.FolderSyncJob, "_run_remote") as transfer:
                job = library.FolderSyncJob(root, [dead1, dead2])
                with self.assertRaises(library.LibraryError):
                    job.run()
            self.assertEqual(len(job.failures), 2)
            transfer.assert_not_called()

    def test_reachable_device_still_syncs_when_another_is_down(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "Media"
            root.mkdir()
            (root / "a.mp3").write_bytes(b"x")
            here = preload.Target("here")
            down = preload.Target("down", "down.invalid")
            up = preload.Target("up", "up.invalid")
            with mock.patch.object(
                library, "probe_targets",
                return_value=([up], [(down, "SSH failed")]),
            ), mock.patch.object(library.FolderSyncJob, "_run_remote") as transfer, \
                    mock.patch.object(library, "write_remote_marker"):
                job = library.FolderSyncJob(root, [here, down, up])
                copied = job.run()
            self.assertEqual(copied, 1)
            self.assertEqual(
                [call.args[0].name for call in transfer.call_args_list], ["here", "up"]
            )
            self.assertEqual(len(job.failures), 1)
            self.assertIn("down", job.failures[0])

    def test_ssh_setup_timeout_reports_the_cause(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "Media"
            root.mkdir()
            (root / "a.mp3").write_bytes(b"x")
            job = library.FolderSyncJob(root, [preload.Target("dead", "dead-host")])
            expired = subprocess.TimeoutExpired("ssh", library.SSH_SETUP_TIMEOUT_S)
            with mock.patch.object(library.subprocess, "run", side_effect=expired):
                with self.assertRaises(library.LibraryError) as ctx:
                    job._run_remote(preload.Target("dead", "dead-host"), None)
            message = str(ctx.exception)
            self.assertIn("15s", message)
            self.assertIn("keyless SSH", message)

    def test_tailscale_ssh_interception_is_explained(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "Media"
            root.mkdir()
            (root / "a.mp3").write_bytes(b"x")
            job = library.FolderSyncJob(root, [preload.Target("desk", "desktop-host")])
            blocked = subprocess.CompletedProcess(
                [], 255, stdout="",
                stderr="# Tailscale SSH requires an additional check.\n",
            )
            with mock.patch.object(library.subprocess, "run", return_value=blocked):
                with self.assertRaises(library.LibraryError) as ctx:
                    job._run_remote(preload.Target("desk", "desktop-host"), None)
            self.assertIn("Tailscale SSH intercepted", str(ctx.exception))

    def test_marker_write_is_a_noop_for_the_local_target(self) -> None:
        lib = library.Library("/tmp", "local:/tmp", "Local /tmp")
        library.write_remote_marker(
            preload.Target("this device"), lib, library.LibraryStats(1, 1)
        )


class DependencyTests(unittest.TestCase):
    def test_host_missing_rsync_is_reported_before_any_transfer(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "Media"
            root.mkdir()
            (root / "a.mp3").write_bytes(b"x")
            job = library.FolderSyncJob(root, [preload.Target("this device")])
            with mock.patch.object(library.shutil, "which", return_value=None):
                with self.assertRaises(library.LibraryError) as ctx:
                    job.run()
            self.assertIn("rsync", str(ctx.exception))
            self.assertIn("not installed on this device", str(ctx.exception))

    def test_target_missing_rsync_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "Media"
            root.mkdir()
            (root / "a.mp3").write_bytes(b"x")
            job = library.FolderSyncJob(root, [preload.Target("laptop", "laptop.invalid")])
            missing = subprocess.CompletedProcess(
                [], 127, stdout="", stderr="rsync is not installed on this device\n"
            )
            with mock.patch.object(library.subprocess, "run", return_value=missing):
                with self.assertRaises(library.LibraryError) as ctx:
                    job._run_remote(preload.Target("laptop", "laptop.invalid"), None)
            self.assertIn("rsync is not installed on this device", str(ctx.exception))

    def test_mkdir_command_checks_rsync_on_the_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "Media"
            root.mkdir()
            (root / "a.mp3").write_bytes(b"x")
            job = library.FolderSyncJob(root, [preload.Target("laptop", "laptop.invalid")])
            ok = subprocess.CompletedProcess([], 0, stdout="", stderr="")
            with mock.patch.object(library.subprocess, "run", return_value=ok) as run:
                with mock.patch.object(library.subprocess, "Popen") as popen:
                    popen.return_value.poll.return_value = 1
                    popen.return_value.returncode = 1
                    popen.return_value.stderr.read.return_value = "rsync failed"
                    with self.assertRaises(library.LibraryError):
                        job._run_remote(preload.Target("laptop", "laptop.invalid"), None)
            command = run.call_args.args[0][-1]
            self.assertIn("command -v rsync", command)


class ConfigMigrationTests(unittest.TestCase):
    def test_plain_string_roots_become_records(self) -> None:
        records = config.normalize_library_roots(
            ["/mnt/storage/Media", {"path": "/mnt/storage/Shows", "identity": "smb:storage/Shows"}, "", 5]
        )
        self.assertEqual(
            records,
            [
                {"path": "/mnt/storage/Media", "identity": ""},
                {"path": "/mnt/storage/Shows", "identity": "smb:storage/Shows"},
            ],
        )

    def test_missing_or_invalid_roots_become_an_empty_list(self) -> None:
        self.assertEqual(config.normalize_library_roots(None), [])
        self.assertEqual(config.normalize_library_roots("nope"), [])

    def test_find_library_accepts_a_legacy_string_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "Media"
            root.mkdir()
            track = root / "a.mp3"
            track.write_bytes(b"x")
            with mock.patch.object(library, "resolve_source") as resolve:
                resolve.return_value = library.Library(
                    str(root), "smb:storage/Media", "SMB storage / Media"
                )
                found = library.find_library(track, [str(root)])
            self.assertIsNotNone(found)
            assert found is not None
            self.assertEqual(found.identity, "smb:storage/Media")


class SourceValidationTests(unittest.TestCase):
    def test_unreadable_source_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "Media"
            root.mkdir()
            (root / "a.mp3").write_bytes(b"x")
            os.chmod(root, 0o000)
            try:
                if os.access(root, os.R_OK):
                    self.skipTest("running with privileges that ignore permissions")
                with self.assertRaises(library.LibraryError) as ctx:
                    library.scan_directory(root)
                self.assertIn("not readable", str(ctx.exception))
            finally:
                os.chmod(root, 0o700)

    def test_symlink_pointing_outside_the_root_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "Media"
            root.mkdir()
            outside = Path(temp) / "Elsewhere" / "song.mp3"
            outside.parent.mkdir()
            outside.write_bytes(b"x")
            link = root / "shortcut"
            link.symlink_to(outside.parent)
            with self.assertRaises(library.LibraryError):
                library.relative_media_path(root, link / "song.mp3")
            self.assertIsNone(
                library.find_library(link / "song.mp3", [{"path": str(root), "identity": "smb:storage/Media"}])
            )


if __name__ == "__main__":
    unittest.main()


class AbsentRootTests(unittest.TestCase):
    """An unmounted nofail share is skipped, not reported as a failure."""

    IDENTITY = "smb:storage/myMusic"

    def _absent_root(self, temp: str) -> Path:
        # Guaranteed absent, like a mountpoint whose share has not connected.
        return Path(temp) / "unmounted" / "myMusic"

    def test_resolve_source_reports_an_absent_root_as_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = self._absent_root(temp)
            with self.assertRaises(library.LibraryUnavailable) as caught:
                library.resolve_source(root)
            message = str(caught.exception)
            self.assertIn(str(root), message)
            self.assertIn("is not available right now", message)
            self.assertIn("nofail", message)

    def test_unavailable_root_is_still_a_library_error(self) -> None:
        # Callers that only know LibraryError keep working unchanged.
        self.assertTrue(issubclass(library.LibraryUnavailable, library.LibraryError))

    def test_folder_sync_names_the_root_identity_when_it_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = self._absent_root(temp)
            with self.assertRaises(library.LibraryUnavailable) as caught:
                library.FolderSyncJob(
                    root, [preload.Target("this device")], identity=self.IDENTITY
                )
            message = str(caught.exception)
            self.assertIn("SMB storage / myMusic", message)
            self.assertIn("missing directory", message)
            self.assertIn("nofail", message)

    def test_folder_sync_without_an_identity_hint_still_names_the_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = self._absent_root(temp)
            with self.assertRaises(library.LibraryUnavailable) as caught:
                library.FolderSyncJob(root, [preload.Target("this device")])
            self.assertIn(str(root), str(caught.exception))

    def test_scanning_an_absent_root_names_the_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = self._absent_root(temp)
            with self.assertRaises(library.LibraryUnavailable) as caught:
                library.scan_directory(root, self.IDENTITY)
            message = str(caught.exception)
            self.assertIn("SMB storage / myMusic", message)
            self.assertIn("missing directory", message)
            self.assertIn(str(root), message)

    def test_available_tracks_whether_the_root_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            present = library.Library(temp, "local:" + temp, "Local " + temp)
            absent = library.Library(
                str(self._absent_root(temp)), self.IDENTITY, "SMB storage / myMusic"
            )
            self.assertTrue(present.available)
            self.assertFalse(absent.available)
