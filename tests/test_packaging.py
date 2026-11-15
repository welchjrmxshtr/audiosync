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
        for phrase in (
            "pyproject",
            "setup-smb-mount",
            "--remove-smb",
            "48,000 Hz",
            "Copy VLC",
            "Bluetooth PAN",
        ):
            self.assertIn(phrase, text)


class ShellScriptTests(unittest.TestCase):
    def test_shell_scripts_parse(self) -> None:
        for relative in (
            "install.sh",
            "scripts/setup-smb-mount.sh",
            "scripts/setup-bt-pan.sh",
            "scripts/update.sh",
        ):
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

    def test_installer_offers_opt_in_auto_updates(self) -> None:
        text = (ROOT / "install.sh").read_text(encoding="utf-8")
        self.assertIn("WITH_AUTO_UPDATE=0", text)
        for phrase in (
            "--with-auto-update",
            "audiosync-update.service",
            "audiosync-update.timer",
            "enable --now audiosync-update.timer",
        ):
            self.assertIn(phrase, text)

    def test_installer_uninstall_removes_auto_update_units(self) -> None:
        text = (ROOT / "install.sh").read_text(encoding="utf-8")
        uninstall = text.split('if [ "$ACTION" = "uninstall" ]', 1)[1]
        for phrase in ("audiosync-update.timer", "audiosync-update.service"):
            self.assertIn(phrase, uninstall)

    def test_setup_script_is_executable_and_has_safe_invariants(self) -> None:
        script = ROOT / "scripts/setup-smb-mount.sh"
        self.assertTrue(os.access(script, os.X_OK))
        text = script.read_text(encoding="utf-8")
        for phrase in ("umask 077", "chmod 0600", "audiosync-managed-smb-mount", "cp --preserve=mode", "fstab"):
            self.assertIn(phrase, text)

    def test_bt_pan_helper_is_executable_and_explains_itself(self) -> None:
        script = ROOT / "scripts/setup-bt-pan.sh"
        self.assertTrue(os.access(script, os.X_OK))
        result = subprocess.run(
            ["bash", str(script), "--help"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        for phrase in ("--mode nap", "--mode panu", "--mac", "--dry-run"):
            self.assertIn(phrase, result.stdout)

    def test_bt_pan_dry_run_reports_instead_of_guessing(self) -> None:
        result = subprocess.run(
            [
                "bash",
                str(ROOT / "scripts/setup-bt-pan.sh"),
                "--dry-run",
                "--mode",
                "panu",
                "--mac",
                "AA:BB:CC:DD:EE:FF",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        # Without nmcli the script must say what is missing rather than pretend.
        self.assertIn(result.returncode, (0, 1))
        output = result.stdout + result.stderr
        self.assertTrue("nmcli" in output or "dry-run" in output, output)

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


_PACTL_SINKS = """
Sink #64
\tState: RUNNING
\tName: alsa_output.pci-0000_0b_00.6.analog-stereo
\tDescription: Test Analog Stereo
\tProperties:
\t\talsa.card = "1"
\t\talsa.device = "0"
\t\talsa.card_name = "Test Analog"
"""


class InstallerAutoUpdateTests(unittest.TestCase):
    """Exercise the installer against a throwaway HOME and stubbed system tools."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "systemctl.log"
        self._stub("systemctl", '#!/bin/sh\nprintf "%s\\n" "$*" >> "$SYSTEMCTL_LOG"\n')
        self._stub("snapserver", "#!/bin/sh\nexit 0\n")
        self._stub(
            "snapclient",
            '#!/bin/sh\ncase "$1" in --help) echo "snapclient 0.34 (pipewire)";; esac\nexit 0\n',
        )
        self._stub("parec", "#!/bin/sh\nexit 0\n")
        self._stub("sleep", "#!/bin/sh\nexit 0\n")
        self._stub(
            "pactl",
            "#!/bin/sh\n"
            'if [ "$1" = list ] && [ "$2" = short ]; then\n'
            "  printf '0\\taudiosync_sink\\tAudiosync\\n'\n"
            'elif [ "$1" = list ] && [ "$2" = sinks ]; then\n'
            "  cat <<'SINKS'\n" + _PACTL_SINKS + "SINKS\n"
            "fi\n"
            "exit 0\n",
        )

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _stub(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)

    def _run_install(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.update(
            {
                "HOME": str(self.home),
                "XDG_CONFIG_HOME": str(self.home / ".config"),
                "XDG_DATA_HOME": str(self.home / ".local/share"),
                "XDG_STATE_HOME": str(self.home / ".local/state"),
                "AUDIOSYNC_CONFIG_DIR": str(self.home / ".config/audiosync"),
                "AUDIOSYNC_STATE_DIR": str(self.home / ".local/share/audiosync"),
                "SYSTEMCTL_LOG": str(self.log),
                "PATH": f"{self.bin}{os.pathsep}{env.get('PATH', '')}",
            }
        )
        result = subprocess.run(
            ["bash", str(ROOT / "install.sh"), *args],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def _unit(self, name: str) -> Path:
        return self.home / ".config/systemd/user" / name

    def _calls(self) -> str:
        return self.log.read_text(encoding="utf-8") if self.log.exists() else ""

    def test_with_auto_update_installs_and_enables_timer(self) -> None:
        self._run_install("--with-auto-update")
        for unit in ("audiosync-update.service", "audiosync-update.timer"):
            self.assertEqual(
                self._unit(unit).read_text(encoding="utf-8"),
                (ROOT / "systemd" / unit).read_text(encoding="utf-8"),
            )
        self.assertIn("enable --now audiosync-update.timer", self._calls())

    def test_without_flag_leaves_auto_updates_off(self) -> None:
        self._run_install()
        self.assertFalse(self._unit("audiosync-update.service").exists())
        self.assertFalse(self._unit("audiosync-update.timer").exists())
        self.assertNotIn("enable --now audiosync-update.timer", self._calls())

    def test_uninstall_removes_auto_update_units(self) -> None:
        self._run_install("--with-auto-update")
        self._run_install("--uninstall", "--no-smb")
        self.assertFalse(self._unit("audiosync-update.service").exists())
        self.assertFalse(self._unit("audiosync-update.timer").exists())
        self.assertIn("disable --now audiosync-update.timer", self._calls())


if __name__ == "__main__":
    unittest.main()
