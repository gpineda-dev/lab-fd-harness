"""
base.py - Base abstractions for domain coprocessors and shared context.
"""
from dataclasses import dataclass, field
from typing import Any, Callable, ClassVar, Dict, Optional, Tuple, Type

from harness.core.model import Event, Instruction
from harness.core.scheduler import HeapScheduler


StreamFilter = Callable[[str], Optional[str]]


@dataclass
class CoprocessorContext:
    scheduler: HeapScheduler
    emit_event: Callable[[Event], None]
    variables: Dict[str, float] = field(default_factory=dict)
    bus: Optional[Any] = None  # Optional EventBus instance
    _stdout_filters: Dict[str, Tuple[int, StreamFilter]] = field(default_factory=dict)

    def attach_stdout_filter(self, name: str, fn: StreamFilter, priority: int = 100) -> None:
        """
        Attaches or updates a named filter on the child stdout stream.
        Lower priority value runs earlier in the pipeline.
        If any filter returns None, the line is swallowed/silenced.
        """
        self._stdout_filters[name] = (priority, fn)

    def detach_stdout_filter(self, name: str) -> None:
        """Detaches a named stdout filter if present."""
        self._stdout_filters.pop(name, None)

    def apply_stream_filters(self, line: str) -> Optional[str]:
        """
        Pipes a text line through all attached filters in ascending priority order.
        Returns the sanitized string, or None if silenced.
        """
        curr: Optional[str] = line
        # Sort by priority value
        for _, fn in sorted(self._stdout_filters.values(), key=lambda item: item[0]):
            if curr is None:
                return None
            curr = fn(curr)
        return curr


class BaseCoprocessor:
    """
    Abstract domain coprocessor.
    Declares instructions it handles via handled_instructions tuple.
    """
    handled_instructions: ClassVar[Tuple[Type[Instruction], ...]] = ()

    def __init__(self, ctx: CoprocessorContext):
        self.ctx = ctx

    def handle_instruction(self, inst: Instruction) -> None:
        raise NotImplementedError(f"{self.__class__.__name__} must implement handle_instruction")
