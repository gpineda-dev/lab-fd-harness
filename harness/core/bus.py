"""
bus.py - In-memory synchronous pub/sub event bus for coordinator and engines.
"""
from typing import Callable, Dict, List


class EventBus:
    """
    Synchronous in-memory event bus facilitating multi-engine IPC.
    Topics can be subscribed to specifically (e.g. 'deploy:ready') or globally ('*').
    """

    def __init__(self):
        self._subscribers: Dict[str, List[Callable[[str, str], None]]] = {}

    def subscribe(self, topic: str, callback: Callable[[str, str], None]) -> None:
        """Register a subscriber callback(topic, payload) for a topic."""
        self._subscribers.setdefault(topic, []).append(callback)

    def unsubscribe(self, topic: str, callback: Callable[[str, str], None]) -> None:
        """Unregister a subscriber callback."""
        if topic in self._subscribers and callback in self._subscribers[topic]:
            self._subscribers[topic].remove(callback)

    def emit(self, topic: str, payload: str = "") -> None:
        """Publish an event across all matching subscribers synchronously."""
        targets = list(self._subscribers.get(topic, []))
        if topic != "*":
            targets.extend(self._subscribers.get("*", []))

        for cb in targets:
            try:
                cb(topic, payload)
            except Exception:
                pass
