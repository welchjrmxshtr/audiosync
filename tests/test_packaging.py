"""Packaging, shell-script, and setup-script contract tests."""

from __future__ import annotations

import os
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PackagingTests(unittest.TestCase):
    def test_pyproject_declares_the_console_entry_point_and_runtime_deps(self) -> None:
        data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        project = data["project"]
        self.assertEqual(project["name"], "audiosync")
        self.assertEqual(project["scripts"]["audiosync"], "audiosync.__main__:main")
        dependencies = {item.split(">=", 1)[0].lower() for item in project["dependencies"]}
        self.assertIn("pyside6", dependencies)
        self.assertIn("zeroconf", dependencies)
        self.assertGreaterEqual(tuple(int(part) for part in project["requires-python"].removeprefix(">=").split(".")), (3, 11))

    def test_console_entry_module_is_importable(self) -> None:
        from audiosync.__main__ import main

        self.assertTrue(callable(main))

    def test_readme_covers_install_mount_latency_and_uninstall(self) -> None:
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        for phrase in ("pyproject", "setup-smb-mount", "--remove-smb", "48,000 Hz", "Copy VLC"):
            self.assertIn(phrase, text)


class ShellScriptTests(unittest.TestCase):
    def test_shell_scripts_parse(self) -> None:
        for relative in ("install.sh", "scripts/setup-smb-mount.sh"):
            result = subprocess.run(
                ["bash", "-n", str(ROOT / relative)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_installer_has_explicit_safe_uninstall_controls(self) -> None:
        text = (ROOT / "install.sh").read_text(encoding="utf-8")
        for phrase in (
            "--dry-run",
            "--remove-smb",
            "--no-smb",
            "audiosync.system_files",
            ".audiosync.bak",
            "media",
        ):
            self.assertIn(phrase, text)

    def test_setup_script_is_executable_and_has_safe_invariants(self) -> None:
        script = ROOT / "scripts/setup-smb-mount.sh"
        self.assertTrue(os.access(script, os.X_OK))
        text = script.read_text(encoding="utf-8")
        for phrase in ("umask 077", "chmod 0600", "audiosync-managed-smb-mount", "cp --preserve=mode", "fstab"):
            self.assertIn(phrase, text)

    def test_setup_dry_run_changes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fstab = root / "fstab"
            fstab.write_text("/dev/vdb1 /mnt/storage ext4 defaults 0 2\n", encoding="utf-8")
            mountpoint = root / "media"
            credentials = root / "samba" / "credentials-storage"
            result = subprocess.run(
                [
                    "bash",
                    str(ROOT / "scripts/setup-smb-mount.sh"),
                    "--dry-run",
                    "--server", "192.0.2.10",
                    "--share", "storage",
                    "--mountpoint", str(mountpoint),
                    "--credentials", str(credentials),
                    "--owner", os.environ.get("USER", "root"),
                ],
                capture_output=True,
                text=True,
                timeout=30,
                env={
                    **os.environ,
                    "AUDIOSYNC_FSTAB": str(fstab),
                },
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("would add", result.stdout)
            self.assertEqual(fstab.read_text(encoding="utf-8"), "/dev/vdb1 /mnt/storage ext4 defaults 0 2\n")
            self.assertFalse(mountpoint.exists())
            self.assertFalse(credentials.exists())

    def test_setup_dry_run_reports_existing_entry_and_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            mountpoint = root / "media"
            mountpoint.mkdir()
            credentials = root / "samba" / "credentials-storage"
            credentials.parent.mkdir()
            credentials.write_text("username=u\npassword=p\n", encoding="utf-8")
            credentials.chmod(0o600)
            fstab = root / "fstab"
            fstab.write_text(
                f"# audiosync-managed-smb-mount\n//192.0.2.10/storage {mountpoint} cifs "
                f"credentials={credentials} 0 0\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    "bash",
                    str(ROOT / "scripts/setup-smb-mount.sh"),
                    "--dry-run",
                    "--server", "192.0.2.10",
                    "--share", "storage",
                    "--mountpoint", str(mountpoint),
                    "--credentials", str(credentials),
                    "--owner", os.environ.get("USER", "root"),
                ],
                capture_output=True,
                text=True,
                timeout=30,
                env={**os.environ, "AUDIOSYNC_FSTAB": str(fstab)},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("would keep the existing CIFS entry", result.stdout)
            self.assertIn("would keep existing credentials", result.stdout)
            self.assertEqual(fstab.read_text(encoding="utf-8").count("192.0.2.10"), 1)

    def test_uninstall_dry_run_does_not_touch_system_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fstab = root / "fstab"
            mountpoint = root / "storage"
            fstab.write_text(
                f"//192.0.2.10/storage {mountpoint} cifs defaults 0 0\n", encoding="utf-8"
            )
            env = {
                **os.environ,
                "AUDIOSYNC_FSTAB": str(fstab),
                "AUDIOSYNC_SMB_MOUNT": str(mountpoint),
                "AUDIOSYNC_SMB_CREDENTIALS": str(root / "credentials"),
                "HOME": str(root / "home"),
            }
            result = subprocess.run(
                ["bash", str(ROOT / "install.sh"), "--uninstall", "--dry-run", "--remove-smb"],
                capture_output=True,
                text=True,
                timeout=30,
                env=env,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("dry-run", result.stdout)
            self.assertTrue(fstab.exists())
            self.assertFalse(mountpoint.exists())


if __name__ == "__main__":
    unittest.main()
