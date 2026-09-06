"""
scheduler.py - Single-threaded priority queue scheduler powered by CPython's _heapq.
Zero asyncio, zero threads, O(log N) scheduling, O(1) next-deadline inspection.
"""
from dataclasses import dataclass, field
import heapq
import time
from typing import Any, Callable, Dict, List, Optional


@dataclass(order=True)
class ScheduledEntry:
    target_time: float
    sequence: int
    key: str = field(compare=False)
    action: Callable[[], None] = field(compare=False)
    cancelled: bool = field(default=False, compare=False)


class HeapScheduler:
    def __init__(self, time_fn: Optional[Callable[[], float]] = None):
        self._heap: List[ScheduledEntry] = []
        self._entries: Dict[str, ScheduledEntry] = {}
        self._sequence = 0
        self._time_fn = time_fn or time.monotonic

    def now(self) -> float:
        return self._time_fn()

    def schedule_at(self, target_time: float, key: str, action: Callable[[], None]):
        """Schedules an action at an absolute monotonic timestamp."""
        self.cancel(key)
        self._sequence += 1
        entry = ScheduledEntry(
            target_time=target_time,
            sequence=self._sequence,
            key=key,
            action=action,
        )
        self._entries[key] = entry
        heapq.heappush(self._heap, entry)

    def schedule_after(self, delay: float, key: str, action: Callable[[], None]):
        """Schedules an action after a relative delay in seconds."""
        self.schedule_at(self.now() + max(0.0, delay), key, action)

    def cancel(self, key: str):
        """Cancels a scheduled task in O(1) via lazy cancellation."""
        if key in self._entries:
            entry = self._entries.pop(key)
            entry.cancelled = True

    def time_to_next(self) -> Optional[float]:
        """
        Returns the number of seconds until the earliest non-cancelled event.
        Returns None if no events are pending (select() can wait indefinitely).
        Purges cancelled events encountered at the head of the min-heap in O(1) amortized.
        """
        while self._heap and self._heap[0].cancelled:
            heapq.heappop(self._heap)

        if not self._heap:
            return None

        return max(0.0, self._heap[0].target_time - self.now())

    def pop_due_events(self):
        """Pops and executes all events whose scheduled target timestamp has passed."""
        current_time = self.now()
        # 100ns epsilon prevents IEEE 754 precision artifacts from delaying due events
        while self._heap and self._heap[0].target_time <= (current_time + 1e-7):
            entry = heapq.heappop(self._heap)
            if not entry.cancelled:
                self._entries.pop(entry.key, None)
                entry.action()
