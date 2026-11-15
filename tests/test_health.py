"""Health signal tests that do not need a running client."""

from __future__ import annotations

import unittest
from subprocess import CompletedProcess
from unittest import mock

from audiosync.health import IDLE_MARKER, UNDERRUN_MARKERS, ClientTracker, RemoteUnderrunWatcher, count_underruns


def _row(client_id: str, connected: bool = True) -> dict:
    return {"id": client_id, "connected": connected, "name": client_id, "volume": {}}


class ClientTrackerTests(unittest.TestCase):
    def test_new_clients_are_not_drops(self) -> None:
        tracker = ClientTracker()
        self.assertEqual(tracker.observe([]), 0)
        self.assertEqual(tracker.observe([_row("a"), _row("b")]), 0)
        self.assertEqual(tracker.drops, 0)

    def test_disappearing_client_is_a_drop(self) -> None:
        tracker = ClientTracker()
        tracker.observe([_row("a"), _row("b")])
        self.assertEqual(tracker.observe([_row("a")]), 1)
        self.assertEqual(tracker.drops, 1)

    def test_disconnected_flag_is_a_drop(self) -> None:
        tracker = ClientTracker()
        tracker.observe([_row("a")])
        self.assertEqual(tracker.observe([_row("a", connected=False)]), 1)

    def test_reset_forgets_the_group(self) -> None:
        tracker = ClientTracker()
        tracker.observe([_row("a")])
        tracker.reset()
        self.assertEqual(tracker.observe([]), 0)

    def test_idle_marker_is_not_an_underrun(self) -> None:
        # The watcher must ignore "No chunks available"; that is idle, not starved.
        self.assertNotIn(IDLE_MARKER, UNDERRUN_MARKERS)
        self.assertTrue(any("underrun" in marker for marker in UNDERRUN_MARKERS))

    def test_underrun_counter_ignores_idle_lines(self) -> None:
        text = "No chunks available\nNot enough frames available\nbuffer underrun\n"
        self.assertEqual(count_underruns(text), 2)

    def test_remote_watcher_keeps_its_cursor(self) -> None:
        output = "No chunks available\nNot enough frames available\nbuffer underrun\n"
        with mock.patch("audiosync.health._run", return_value=CompletedProcess(["ssh"], 0, output, "")):
            watcher = RemoteUnderrunWatcher("remote", "host")
            self.assertEqual(watcher.poll(2_000_000_000.0), 2)
            self.assertEqual(watcher.since, 2_000_000_000.0)
            self.assertEqual(watcher.poll(2_000_000_010.0), 2)
            self.assertEqual(watcher.total, 4)


if __name__ == "__main__":
    unittest.main()
