"""Prune Snapcast's persisted pairing history without a running server."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from audiosync import config


def _client(name: str, ip: str, connected: bool, client_id: str | None = None) -> dict:
    return {
        "id": client_id or f"{name}-id",
        "connected": connected,
        "host": {"name": name, "ip": ip},
        "config": {"name": "", "volume": {"muted": False, "percent": 100}},
    }


def _server(*groups: list[dict]) -> dict:
    return {
        "ConfigVersion": 2,
        "Groups": [
            {"id": f"group-{index}", "name": "", "clients": group}
            for index, group in enumerate(groups)
        ],
    }


class PairingPruneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "server.json"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _write(self, payload: dict) -> None:
        self.path.write_text(json.dumps(payload), encoding="utf-8")

    def _read(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_removes_connected_and_disconnected_records_for_target(self) -> None:
        self._write(
            _server(
                [_client("living-room", "192.0.2.12", True)],
                [_client("living-room", "192.0.2.12", False, "living-room#2")],
                [_client("study-laptop", "192.0.2.13", True)],
            )
        )
        removed = config.prune_pairing_history(self.path, "living-room")
        self.assertEqual(removed, 2)
        names = [
            client["host"]["name"]
            for group in self._read()["Groups"]
            for client in group["clients"]
        ]
        self.assertEqual(names, ["study-laptop"])
        self.assertEqual(len(self._read()["Groups"]), 3, "groups are kept")

    def test_matches_by_ip_when_name_differs(self) -> None:
        self._write(_server([_client("", "192.0.2.12", True)]))
        removed = config.prune_pairing_history(self.path, "living-room", {"192.0.2.12"})
        self.assertEqual(removed, 1)
        self.assertEqual(self._read()["Groups"][0]["clients"], [])

    def test_malformed_json_is_a_noop(self) -> None:
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(config.prune_pairing_history(self.path, "living-room"), 0)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "{not json")

    def test_backup_is_written_before_rewriting(self) -> None:
        self._write(_server([_client("kitchen-pc", "127.0.0.1", False)]))
        self.assertEqual(config.prune_pairing_history(self.path, "kitchen-pc"), 1)
        backup = self.path.with_name(self.path.name + ".bak")
        self.assertTrue(backup.exists())
        self.assertEqual(len(json.loads(backup.read_text())["Groups"][0]["clients"]), 1)
        self.assertEqual(self._read()["Groups"][0]["clients"], [])


if __name__ == "__main__":
    unittest.main()
