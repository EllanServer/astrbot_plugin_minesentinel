"""Bounded observation window sampling."""

from __future__ import annotations

import heapq

from ..models import ObservationRecord
from ..observation_priority import observation_priority_score
from .models import RecentObservationWindow


class RecentWindowBuilder:
    """Keeps bounded analysis records while counting the complete window."""

    def __init__(
        self,
        max_records: int,
    ):
        self.max_records = max(1, max_records)
        self.priority_limit = max(1, min(self.max_records, (self.max_records * 2 + 2) // 3))
        self.reservoir_limit = max(0, self.max_records - self.priority_limit)
        self.priority_records: list[tuple[float, int, ObservationRecord]] = []
        self.reservoir_records: list[ObservationRecord] = []
        self.identities: set[str] = set()
        self.total_count = 0

    def add(self, record: ObservationRecord):
        self.total_count += 1
        if record.identity:
            self.identities.add(record.identity)
        self._add_priority_record(record)
        self._add_reservoir_record(record)

    def build(self) -> RecentObservationWindow:
        records = self._merge_bounded_records()
        records.sort(key=lambda item: item.timestamp)
        return RecentObservationWindow(
            records=records,
            total_count=self.total_count,
            unique_players=len(self.identities),
            truncated=self.total_count > len(records),
            max_records=self.max_records,
        )

    def _add_priority_record(self, record: ObservationRecord):
        if self.priority_limit <= 0:
            return
        score = observation_priority_score(record)
        if score <= 0:
            return
        item = (score, self.total_count, record)
        if len(self.priority_records) < self.priority_limit:
            heapq.heappush(self.priority_records, item)
            return
        if item[:2] > self.priority_records[0][:2]:
            heapq.heapreplace(self.priority_records, item)

    def _add_reservoir_record(self, record: ObservationRecord):
        if self.reservoir_limit <= 0:
            return
        if len(self.reservoir_records) < self.reservoir_limit:
            self.reservoir_records.append(record)
            return

        # Deterministic reservoir-style replacement: bounded memory while still
        # sampling across the whole time window instead of keeping only the head.
        index = (self.total_count * 1103515245 + 12345) % self.total_count
        if index < self.reservoir_limit:
            self.reservoir_records[index] = record

    def _merge_bounded_records(self) -> list[ObservationRecord]:
        merged: list[ObservationRecord] = []
        seen_ids: set[int] = set()
        for _score, _idx, record in sorted(
            self.priority_records,
            key=lambda item: (-item[0], item[1]),
        ):
            if id(record) in seen_ids:
                continue
            merged.append(record)
            seen_ids.add(id(record))
            if len(merged) >= self.max_records:
                return merged

        for record in self.reservoir_records:
            if id(record) in seen_ids:
                continue
            merged.append(record)
            seen_ids.add(id(record))
            if len(merged) >= self.max_records:
                break
        return merged
