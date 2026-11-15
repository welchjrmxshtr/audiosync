"""Health signals the controller reacts to.

Two independent sources, because they mean different things:

* ``snapclient`` logs starvation when its chunk buffer runs dry - that is an
  underrun caused by too small a buffer for the link, so the buffer goes up.
* A client that was connected and is not any more means the connection
  dropped, which is a stronger signal and moves the buffer further.

Both are read-only observations; nothing here starts or stops audio.
"""

from __future__ import annotations

import time

from .backend import _run
from .preload import SSH_OPTIONS

#: Lines snapclient emits when it is starved.  "No chunks available" is what
#: an idle stream says and must not be counted, or silence would look like an
#: underrun.
UNDERRUN_MARKERS = (
    "not enough frames available",
    "buffer underrun",
    "underrun",
    "failed to get chunk",
)
IDLE_MARKER = "no chunks available"


def count_underruns(text: str) -> int:
    """Count starvation lines while treating the idle stream as silence."""
    count = 0
    for line in text.splitlines():
        lowered = line.lower()
        if IDLE_MARKER in lowered:
            continue
        if any(marker in lowered for marker in UNDERRUN_MARKERS):
            count += 1
    return count


class UnderrunWatcher:
    """Counts new underrun lines in the client journal since the last poll."""

    def __init__(self, unit: str = "snapclient.service", lines: int = 120) -> None:
        self.unit = unit
        self.lines = lines
        self.since = time.time()
        self.total = 0

    def poll(self, now: float | None = None) -> int:
        # The journal cursor is wall-clock.  A monotonic timestamp here would
        # be interpreted as "since 1970" and re-count every old line.
        stamp = now if now is not None else time.time()
        if stamp < 1_000_000_000:
            stamp = time.time()
        try:
            proc = _run(
                [
                    "journalctl",
                    "--user",
                    "-u",
                    self.unit,
                    "--since",
                    f"@{self.since:.3f}",
                    "-n",
                    str(self.lines),
                    "--no-pager",
                    "-o",
                    "cat",
                ],
                timeout=5,
            )
        except Exception:
            return 0
        self.since = stamp
        count = count_underruns(proc.stdout)
        self.total += count
        return count

    def reset(self, now: float | None = None) -> None:
        stamp = now if now is not None else time.time()
        self.since = stamp if stamp > 1_000_000_000 else time.time()
        self.total = 0


class RemoteUnderrunWatcher:
    """Counts new underrun lines in a remote Snapclient journal.

    Each client keeps its own wall-clock cursor.  Polling happens in a worker
    thread in the GUI; this class only builds and parses one SSH command.
    """

    def __init__(self, name: str, host: str, unit: str = "snapclient.service", lines: int = 120) -> None:
        self.name = name
        self.host = host
        self.unit = unit
        self.lines = lines
        self.since = time.time()
        self.total = 0

    def poll(self, now: float | None = None) -> int:
        stamp = now if now is not None else time.time()
        if stamp < 1_000_000_000:
            stamp = time.time()
        command = (
            f"journalctl --user -u {self.unit} --since @{self.since:.3f} "
            f"-n {int(self.lines)} --no-pager -o cat"
        )
        try:
            proc = _run(["ssh", *SSH_OPTIONS, self.host, command], timeout=6)
        except Exception:
            return 0
        if proc.returncode not in (0, 1):  # 1 is normal for an empty journal window
            return 0
        self.since = stamp
        count = count_underruns(proc.stdout)
        self.total += count
        return count

    def reset(self, now: float | None = None) -> None:
        stamp = now if now is not None else time.time()
        self.since = stamp if stamp > 1_000_000_000 else time.time()
        self.total = 0


class ClientTracker:
    """Reports clients that disappeared between two status snapshots."""

    def __init__(self) -> None:
        self.connected: set[str] = set()
        self.drops = 0

    def reset(self) -> None:
        self.connected.clear()

    def observe(self, rows: list[dict]) -> int:
        current = {row["id"] for row in rows if row.get("connected")}
        gone = len(self.connected - current)
        self.connected = current
        self.drops += gone
        return gone
