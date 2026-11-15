"""Tests for the optional SMB system-file detection and removal."""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from audiosync import system_files

EXT4_LINE = "/dev/vdb1 /mnt/storage ext4 defaults 0 2\n"


class SystemFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.samba = self.root / "samba"
        self.samba.mkdir()
        self.fstab = self.root / "fstab"
        self.credentials = self.samba / "credentials-storage"
        self.mountpoint = self.root / "storage"
        self.mountpoint.mkdir()
        self.patch = mock.patch.object(system_files, "SAMBA_DIR", self.samba)
        self.patch.start()

    def tearDown(self) -> None:
        self.patch.stop()
        self.tempdir.cleanup()

    def _cifs_line(self, credentials: Path | None = None) -> str:
        credentials = credentials or self.credentials
        return (
            f"//192.0.2.10/storage {self.mountpoint} cifs "
            f"credentials={credentials},uid=1000,gid=1000 0 0\n"
        )

    def _write_fstab(self, *lines: str) -> None:
        self.fstab.write_text("".join(lines), encoding="utf-8")

    def _write_credentials(self) -> None:
        self.credentials.write_text("username=u\npassword=p\n", encoding="utf-8")
        self.credentials.chmod(0o600)

    def test_detects_only_the_cifs_entry_for_the_configured_mountpoint(self) -> None:
        self._write_fstab(
            EXT4_LINE,
            self._cifs_line(),
            "//192.0.2.10/other /mnt/other cifs defaults 0 0\n",
        )
        artifacts = system_files.detect(self.fstab, self.credentials, self.mountpoint)
        self.assertEqual(len(artifacts.entries), 1)
        self.assertIn("192.0.2.10/storage", artifacts.entries[0].text)
        self.assertTrue(artifacts.mountpoint_empty)
        self.assertFalse(artifacts.mounted)
        self.assertTrue(artifacts.has_artifacts)

    def test_non_cifs_mountpoint_is_not_an_artifact(self) -> None:
        self._write_fstab(EXT4_LINE)
        (self.mountpoint / "keep.mp3").write_bytes(b"x")
        artifacts = system_files.detect(self.fstab, self.credentials, self.mountpoint)
        self.assertEqual(artifacts.entries, ())
        self.assertFalse(artifacts.has_artifacts)

    def test_credentials_are_limited_to_the_samba_directory(self) -> None:
        outside = self.root / "outside-credentials"
        outside.write_text("secret", encoding="utf-8")
        self._write_fstab(self._cifs_line(outside))
        artifacts = system_files.detect(self.fstab, self.credentials, self.mountpoint)
        self.assertEqual(artifacts.entries[0].credentials, ())
        self.assertNotIn(outside, artifacts.credentials)

    def test_remove_deletes_fstab_line_and_credentials_but_preserves_other_lines(self) -> None:
        self._write_fstab(EXT4_LINE, self._cifs_line())
        self._write_credentials()
        artifacts = system_files.detect(self.fstab, self.credentials, self.mountpoint)
        result = system_files.remove(artifacts)
        self.assertTrue(result.removed_fstab)
        self.assertEqual(self.fstab.read_text(encoding="utf-8"), EXT4_LINE)
        self.assertFalse(self.credentials.exists())
        self.assertFalse(self.mountpoint.exists())
        self.assertIsNotNone(result.fstab_backup)
        assert result.fstab_backup is not None
        self.assertIn("192.0.2.10/storage", result.fstab_backup.read_text(encoding="utf-8"))

    def test_nonempty_mountpoint_is_preserved_never_recursively_deleted(self) -> None:
        media = self.mountpoint / "myMusic" / "track.flac"
        media.parent.mkdir()
        media.write_bytes(b"audio")
        self._write_fstab(self._cifs_line())
        artifacts = system_files.detect(self.fstab, self.credentials, self.mountpoint)
        result = system_files.remove(artifacts)
        self.assertFalse(result.removed_mountpoint)
        self.assertTrue(media.exists())
        self.assertTrue(any("media preserved" in item for item in result.skipped))

    def test_mounted_mountpoint_is_not_removed(self) -> None:
        self._write_fstab(self._cifs_line())
        with mock.patch.object(system_files.os.path, "ismount", return_value=True):
            artifacts = system_files.detect(self.fstab, self.credentials, self.mountpoint)
            result = system_files.remove(artifacts)
        self.assertFalse(result.removed_mountpoint)
        self.assertTrue(self.mountpoint.is_dir())
        self.assertTrue(any("still mounted" in item for item in result.skipped))

    def test_remove_is_safe_to_repeat(self) -> None:
        self._write_fstab(self._cifs_line())
        first = system_files.remove(
            system_files.detect(self.fstab, self.credentials, self.mountpoint)
        )
        self.assertTrue(first.removed_fstab)
        second = system_files.remove(
            system_files.detect(self.fstab, self.credentials, self.mountpoint)
        )
        self.assertFalse(second.removed_fstab)
        self.assertEqual(second.removed_credentials, ())
        self.assertFalse(second.removed_mountpoint)

    def test_remove_drops_the_managed_marker_with_the_entry(self) -> None:
        self._write_fstab("\n# audiosync-managed-smb-mount\n" + self._cifs_line())
        artifacts = system_files.detect(self.fstab, self.credentials, self.mountpoint)
        self.assertEqual(artifacts.entries[0].marker_line, 2)
        system_files.remove(artifacts)
        self.assertNotIn("audiosync-managed-smb-mount", self.fstab.read_text(encoding="utf-8"))

    def test_remove_drops_the_root_only_credentials_backup_too(self) -> None:
        self._write_fstab(self._cifs_line())
        self._write_credentials()
        backup = self.credentials.with_name(self.credentials.name + ".audiosync.bak")
        backup.write_text("old credentials\n", encoding="utf-8")
        backup.chmod(0o600)
        artifacts = system_files.detect(self.fstab, self.credentials, self.mountpoint)
        result = system_files.remove(artifacts)
        self.assertIn(backup, result.removed_credentials)
        self.assertFalse(backup.exists())

    def test_check_exit_status_reports_detection(self) -> None:
        args = [
            "--fstab", str(self.fstab),
            "--credentials", str(self.credentials),
            "--mountpoint", str(self.mountpoint),
            "--check",
        ]
        self._write_fstab(self._cifs_line())
        self.assertEqual(system_files.main(args), 0)
        self._write_fstab(EXT4_LINE)
        self.mountpoint.rmdir()
        self.assertEqual(system_files.main(args), 1)

    def test_report_names_media_safety(self) -> None:
        self._write_fstab(self._cifs_line())
        (self.mountpoint / "keep.mp3").write_bytes(b"x")
        artifacts = system_files.detect(self.fstab, self.credentials, self.mountpoint)
        text = "\n".join(system_files.report_lines(artifacts))
        self.assertIn("media preserved", text)
        self.assertIn(str(self.credentials), text)

    def test_dry_run_remove_changes_nothing(self) -> None:
        self._write_fstab(self._cifs_line())
        self._write_credentials()
        with contextlib.redirect_stdout(io.StringIO()) as output:
            status = system_files.main(
                [
                    "--fstab", str(self.fstab),
                    "--credentials", str(self.credentials),
                    "--mountpoint", str(self.mountpoint),
                    "--remove",
                    "--dry-run",
                ]
            )
        self.assertEqual(status, 0)
        self.assertIn("dry-run", output.getvalue())
        self.assertTrue(self.fstab.exists())
        self.assertTrue(self.credentials.exists())
        self.assertTrue(self.mountpoint.exists())


if __name__ == "__main__":
    unittest.main()
