"""
model.py - Strongly typed Instructions and Events.
Lightweight dataclasses defining the protocol between Channels and Coprocessors.
"""
from dataclasses import dataclass
from typing import Any, Optional


# ==============================================================================
# Instructions (Incoming from Channel/Codec to Coprocessor)
# ==============================================================================
@dataclass(frozen=True)
class Instruction:
    pass


@dataclass(frozen=True)
class SetInterval(Instruction):
    timer_id: str
    interval: float
    cycles: int = 0
    policy: str = "skip"  # "skip" or "catchup"
    on_fd: int = 0  # 0 means default stdin, >0 targets a dedicated child FD (e.g. 3)


@dataclass(frozen=True)
class SetTimeout(Instruction):
    timer_id: str
    after: float
    on_fd: int = 0


@dataclass(frozen=True)
class Sleep(Instruction):
    duration: float


@dataclass(frozen=True)
class CancelTimer(Instruction):
    timer_id: str


@dataclass(frozen=True)
class LegacyClock(Instruction):
    interval: float
    cycles: int = 0


@dataclass(frozen=True)
class InitClock(Instruction):
    clock_id: str
    interval: float
    cycles: int = 0
    duration: float = 0.0
    policy: str = "skip"


@dataclass(frozen=True)
class StartClock(Instruction):
    clock_id: str


@dataclass(frozen=True)
class UpdateClock(Instruction):
    clock_id: str
    interval: Optional[float] = None
    expire_at: Optional[Any] = None
    cycles: Optional[int] = None
    duration: Optional[float] = None
    policy: Optional[str] = None


@dataclass(frozen=True)
class WaitClock(Instruction):
    clock_id: str


@dataclass(frozen=True)
class ClockStatusRequest(Instruction):
    clock_id: str


@dataclass(frozen=True)
class ClockListRequest(Instruction):
    pass


@dataclass(frozen=True)
class CalcRequest(Instruction):
    expr: str
    store: Optional[str] = None


@dataclass(frozen=True)
class CalcListRequest(Instruction):
    pass


@dataclass(frozen=True)
class SprintRequest(Instruction):
    template: str


@dataclass(frozen=True)
class FilterMask(Instruction):
    pattern: str
    replacement: str = "[REDACTED]"


@dataclass(frozen=True)
class LogMessage(Instruction):
    text: str
    level: str = "INFO"


@dataclass(frozen=True)
class BusSubscribe(Instruction):
    topic: str


@dataclass(frozen=True)
class BusEmit(Instruction):
    topic: str
    payload: str


# ==============================================================================
# Events (Outgoing from Coprocessor to Channel/Codec)
# ==============================================================================
@dataclass(frozen=True)
class Event:
    pass


@dataclass(frozen=True)
class CalcResult(Event):
    value: str


@dataclass(frozen=True)
class SprintResult(Event):
    text: str


@dataclass(frozen=True)
class Tick(Event):
    timer_id: str
    cycle: int
    lag_ms: float = 0.0
    target_fd: int = 0


@dataclass(frozen=True)
class Timeout(Event):
    timer_id: str
    target_fd: int = 0


@dataclass(frozen=True)
class Wakeup(Event):
    pass


@dataclass(frozen=True)
class LegacyTick(Event):
    cycle: int
    monotonic_ts: float = 0.0


@dataclass(frozen=True)
class ClockWaitResult(Event):
    clock_id: str
    cycle: int
    skipped: int
    lag_ms: float
    monotonic_ts: float
    status: str


@dataclass(frozen=True)
class ClockStatusResult(Event):
    clock_id: str
    interval: float
    elapsed_s: float
    last_cycle: int
    max_cycles: int
    status: str


@dataclass(frozen=True)
class ClockListResult(Event):
    clocks: str
    count: int


@dataclass(frozen=True)
class CalcListResult(Event):
    variables: str
    count: int


@dataclass(frozen=True)
class BusEvent(Event):
    topic: str
    payload: str
    target_fd: int = 3
