"""Lightweight byte-offset index for JSONL observation files.

Each ``.jsonl`` file has an optional ``.idx`` sidecar that records
``(timestamp_ms, byte_offset)`` pairs periodically. ``read_jsonl_window``
uses this index to ``seek`` near the cutoff timestamp instead of scanning
from the file start — turning a window read from O(当天日志量) into
O(窗口日志量).

Index file format (text, one entry per line)::

    <timestamp_ms>\t<byte_offset>\n

Entries are monotonically non-decreasing in both fields (the JSONL tailer
writes records in timestamp order). The index is append-only: writes
append new entries, reads load the whole file into memory once.
"""

from __future__ import annotations

import bisect
from pathlib import Path


class JsonlOffsetIndex:
    """Append-only ``(timestamp_ms, byte_offset)`` index for a JSONL file.

    A new entry is recorded when either ``line_interval`` lines have been
    written since the last entry, or ``time_interval_ms`` milliseconds
    have elapsed since the last entry's timestamp — whichever comes first.
    This keeps the index small (≈1 entry per 256 lines or per minute)
    while bounding the worst-case scan overshoot to at most one interval.
    """

    DEFAULT_LINE_INTERVAL = 256
    DEFAULT_TIME_INTERVAL_MS = 60_000  # 1 minute

    def __init__(
        self,
        index_path: Path,
        line_interval: int = DEFAULT_LINE_INTERVAL,
        time_interval_ms: int = DEFAULT_TIME_INTERVAL_MS,
    ):
        self.index_path = index_path
        self.line_interval = max(1, int(line_interval))
        self.time_interval_ms = max(1, int(time_interval_ms))
        # Parallel lists for bisect; kept sorted by timestamp (append-only).
        self._timestamps: list[int] = []
        self._offsets: list[int] = []
        self._last_indexed_ts: int = 0
        self._lines_since_last_index: int = 0
        # How many entries have been persisted to disk. Entries beyond this
        # count are new and need to be flushed.
        self._persisted_count: int = 0
        self._loaded: bool = False

    @classmethod
    def for_jsonl(
        cls,
        jsonl_path: Path,
        line_interval: int = DEFAULT_LINE_INTERVAL,
        time_interval_ms: int = DEFAULT_TIME_INTERVAL_MS,
    ) -> "JsonlOffsetIndex":
        """Create an index path that sits next to ``jsonl_path``.

        ``20250705.jsonl`` → ``20250705.idx``
        """
        return cls(
            jsonl_path.with_suffix(".idx"),
            line_interval=line_interval,
            time_interval_ms=time_interval_ms,
        )

    # ------------------------------------------------------------------
    # Load / flush
    # ------------------------------------------------------------------
    def load(self) -> None:
        """Load existing index entries from disk (once)."""
        if self._loaded:
            return
        self._timestamps.clear()
        self._offsets.clear()
        try:
            with self.index_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    parts = line.split("\t")
                    if len(parts) != 2:
                        continue
                    try:
                        ts = int(parts[0])
                        off = int(parts[1])
                    except ValueError:
                        continue
                    self._timestamps.append(ts)
                    self._offsets.append(off)
        except FileNotFoundError:
            pass
        except OSError:
            pass
        if self._timestamps:
            self._last_indexed_ts = self._timestamps[-1]
        self._persisted_count = len(self._timestamps)
        self._loaded = True

    def flush(self) -> None:
        """Append new (un-persisted) entries to the ``.idx`` file."""
        if not self._loaded:
            # If never loaded, we can still flush — but we must not clobber
            # existing on-disk entries we haven't read. Load first.
            self.load()
        new_count = len(self._timestamps) - self._persisted_count
        if new_count <= 0:
            return
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        with self.index_path.open("a", encoding="utf-8") as handle:
            for i in range(self._persisted_count, len(self._timestamps)):
                handle.write(f"{self._timestamps[i]}\t{self._offsets[i]}\n")
        self._persisted_count = len(self._timestamps)

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------
    def maybe_index(self, timestamp_ms: int, byte_offset: int) -> bool:
        """Record an entry if interval thresholds are met.

        Call this after writing each JSONL line, passing the line's
        timestamp and its starting byte offset. Returns ``True`` if a
        new index entry was added.
        """
        # Ensure any existing on-disk entries are loaded before we append,
        # so we don't lose them when flush() writes only new entries.
        if not self._loaded:
            self.load()
        self._lines_since_last_index += 1
        # Only compare time gap if we have a previous entry; otherwise
        # _last_indexed_ts == 0 would make time_gap == timestamp_ms (huge)
        # and trigger an immediate index entry on the very first line.
        if self._last_indexed_ts > 0:
            time_gap = timestamp_ms - self._last_indexed_ts
        else:
            time_gap = 0
        if (
            self._lines_since_last_index >= self.line_interval
            or time_gap >= self.time_interval_ms
        ):
            self._timestamps.append(timestamp_ms)
            self._offsets.append(byte_offset)
            self._last_indexed_ts = timestamp_ms
            self._lines_since_last_index = 0
            return True
        return False

    # ------------------------------------------------------------------
    # Read path
    # ------------------------------------------------------------------
    def seek_offset(self, cutoff_ms: int) -> int:
        """Return the byte offset to start scanning for ``ts >= cutoff_ms``.

        Returns the offset of the last index entry whose timestamp is
        ``< cutoff_ms``. Scanning from this offset means we may re-read
        a few lines just before the cutoff (which ``read_jsonl_window``
        filters out via ``ts < cutoff_ms``), but we will never miss a
        record inside the window.

        Returns ``0`` if no suitable entry exists (scan from start).
        """
        self.load()
        if not self._timestamps:
            return 0
        # bisect_left returns the insertion point: the index of the first
        # entry with timestamp >= cutoff_ms.
        idx = bisect.bisect_left(self._timestamps, cutoff_ms)
        if idx == 0:
            # All entries have ts >= cutoff_ms → scan from file start.
            return 0
        # Use the entry just before cutoff: its line has ts < cutoff_ms,
        # so we'll skip it, but subsequent lines may enter the window.
        return self._offsets[idx - 1]

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------
    @property
    def entry_count(self) -> int:
        self.load()
        return len(self._timestamps)

    @property
    def is_empty(self) -> bool:
        self.load()
        return not self._timestamps
