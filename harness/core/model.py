"""
model.py - Strongly typed Instructions and Events.
Lightweight dataclasses defining the protocol between Channels and Coprocessors.
"""
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


# ==============================================================================
# First-Class Structured Logging Models
# ==============================================================================
@dataclass(frozen=True)
class LogRecord:
    action: Dict[str, Any]
    payload: Dict[str, Any]
    text: str

    @property
    def subsystem(self) -> str:
        return self.action.get("domain", "")


@dataclass(frozen=True)
class IORead:
    fd: int
    msg: str

    def to_log(self) -> LogRecord:
        return LogRecord(
            action={"domain": "IO", "name": "READ", "fd": self.fd},
            payload={"msg": self.msg},
            text=self.msg,
        )


@dataclass(frozen=True)
class IOWrite:
    fd: int
    msg: str

    def to_log(self) -> LogRecord:
        return LogRecord(
            action={"domain": "IO", "name": "WRITE", "fd": self.fd},
            payload={"msg": self.msg},
            text=self.msg,
        )


@dataclass(frozen=True)
class ClockInit:
    id: str
    interval: float
    cycles: int = 0
    policy: str = "skip"
    align: Optional[str] = None
    phase_delay_ms: Optional[float] = None

    def to_log(self) -> LogRecord:
        payload: Dict[str, Any] = {
            "id": self.id,
            "interval": self.interval,
            "cycles": self.cycles,
            "policy": self.policy,
        }
        align_info = ""
        if self.align:
            grid_dict: Dict[str, Any] = {"align": self.align}
            if self.phase_delay_ms is not None:
                grid_dict["phase_delay_ms"] = round(self.phase_delay_ms, 1)
                align_info = f' align="{self.align}" (phase_delay={self.phase_delay_ms:.1f}ms)'
            else:
                align_info = f' align="{self.align}"'
            payload["grid"] = grid_dict

        text = f'id={self.id} interval={self.interval:.3f}s cycles={self.cycles} policy={self.policy}{align_info}'
        return LogRecord(
            action={"domain": "CLOCK", "name": "INIT"},
            payload=payload,
            text=text,
        )


@dataclass(frozen=True)
class ClockStart:
    id: str
    t0: float

    def to_log(self) -> LogRecord:
        return LogRecord(
            action={"domain": "CLOCK", "name": "START"},
            payload={"id": self.id, "t0": round(self.t0, 4)},
            text=f"id={self.id} started at T0={self.t0:.4f}",
        )


@dataclass(frozen=True)
class ClockUpdate:
    id: str
    interval: float
    cycles: int

    def to_log(self) -> LogRecord:
        return LogRecord(
            action={"domain": "CLOCK", "name": "UPDAT"},
            payload={"id": self.id, "interval": self.interval, "cycles": self.cycles},
            text=f"id={self.id} updated interval={self.interval:.3f}s cycles={self.cycles}",
        )


@dataclass(frozen=True)
class ClockHold:
    id: str
    cycle: int
    target: float
    delay_ms: float
    align: Optional[str] = None
    phase_lock: bool = False

    def to_log(self) -> LogRecord:
        payload: Dict[str, Any] = {
            "id": self.id,
            "cycle": self.cycle,
            "target": round(self.target, 4),
            "delay_ms": round(self.delay_ms, 1),
        }
        reason_str = ""
        if self.align or self.phase_lock:
            grid_info: Dict[str, Any] = {}
            if self.align:
                grid_info["align"] = self.align
            if self.phase_lock:
                grid_info["phase_lock"] = True
                reason_str = " (grid phase lock)"
            payload["grid"] = grid_info

        return LogRecord(
            action={"domain": "CLOCK", "name": "HOLD"},
            payload=payload,
            text=f"id={self.id} cycle={self.cycle} target={self.target:.4f} delay={self.delay_ms:.1f}ms{reason_str}",
        )


@dataclass(frozen=True)
class ClockTick:
    id: str
    cycle: int
    lag_ms: float
    skipped: int = 0

    def to_log(self) -> LogRecord:
        payload: Dict[str, Any] = {
            "id": self.id,
            "cycle": self.cycle,
            "lag_ms": round(self.lag_ms, 3),
        }
        skip_str = ""
        if self.skipped > 0:
            payload["skipped"] = self.skipped
            skip_str = f" skipped={self.skipped}"
        return LogRecord(
            action={"domain": "CLOCK", "name": "TICK"},
            payload=payload,
            text=f"id={self.id} cycle={self.cycle} lag={self.lag_ms:.3f}ms{skip_str}",
        )


@dataclass(frozen=True)
class ClockOverrun:
    id: str
    cycle: int
    lag_ms: float
    policy: str
    skipped: int = 0
    next_cycle: Optional[int] = None

    def to_log(self) -> LogRecord:
        payload: Dict[str, Any] = {
            "id": self.id,
            "cycle": self.cycle,
            "lag_ms": round(self.lag_ms, 1),
            "policy": self.policy,
        }
        if self.skipped > 0:
            payload["skipped"] = self.skipped
        if self.next_cycle is not None:
            payload["next_cycle"] = self.next_cycle

        if self.policy == "skip":
            text = (
                f"id={self.id} overrun cycle={self.cycle} lag={self.lag_ms:.1f}ms "
                f"policy=skip skipped={self.skipped} next_cycle={self.next_cycle}"
            )
        else:
            text = f"id={self.id} overrun cycle={self.cycle} lag={self.lag_ms:.1f}ms policy=catchup"

        return LogRecord(
            action={"domain": "CLOCK", "name": "OVERR"},
            payload=payload,
            text=text,
        )


@dataclass(frozen=True)
class ClockDone:
    id: str
    reason: str
    cycles: Optional[int] = None
    duration: Optional[float] = None

    def to_log(self) -> LogRecord:
        payload: Dict[str, Any] = {"id": self.id, "reason": self.reason}
        if self.cycles is not None:
            payload["cycles"] = self.cycles
        if self.duration is not None:
            payload["duration"] = self.duration
        
        if self.duration is not None:
            text = f"id={self.id} duration limit reached ({self.duration:.3f}s)"
        elif self.cycles is not None and "skipped" in self.reason:
            text = f"id={self.id} skipped beyond cycle budget ({self.cycles})"
        elif self.cycles is not None:
            text = f"id={self.id} cycle budget reached ({self.cycles})"
        else:
            text = f"id={self.id} {self.reason}"

        return LogRecord(
            action={"domain": "CLOCK", "name": "DONE"},
            payload=payload,
            text=text,
        )


@dataclass(frozen=True)
class TimerInterval:
    id: str
    interval: float
    cycles: int = 0

    def to_log(self) -> LogRecord:
        return LogRecord(
            action={"domain": "TIMER", "name": "INTRV"},
            payload={"id": self.id, "interval": self.interval, "cycles": self.cycles},
            text=f"id={self.id} interval={self.interval:.3f}s cycles={self.cycles}",
        )


@dataclass(frozen=True)
class TimerTimeout:
    id: str
    after: float
    target: float

    def to_log(self) -> LogRecord:
        return LogRecord(
            action={"domain": "TIMER", "name": "TMOUT"},
            payload={"id": self.id, "timeout": self.after, "target": round(self.target, 4)},
            text=f"id={self.id} timeout={self.after:.3f}s target={self.target:.4f}",
        )


@dataclass(frozen=True)
class TimerSleep:
    duration: float
    target: float

    def to_log(self) -> LogRecord:
        return LogRecord(
            action={"domain": "TIMER", "name": "SLEEP"},
            payload={"duration": self.duration, "target": round(self.target, 4)},
            text=f"duration={self.duration:.3f}s target={self.target:.4f}",
        )


@dataclass(frozen=True)
class TimerWake:
    duration: float
    lag_ms: float

    def to_log(self) -> LogRecord:
        return LogRecord(
            action={"domain": "TIMER", "name": "WAKE"},
            payload={"duration": self.duration, "lag_ms": round(self.lag_ms, 3)},
            text=f"duration={self.duration:.3f}s lag={self.lag_ms:.3f}ms",
        )


@dataclass(frozen=True)
class TimerShift:
    to: str
    delay_ms: float
    target: float

    def to_log(self) -> LogRecord:
        return LogRecord(
            action={"domain": "TIMER", "name": "SHIFT"},
            payload={
                "to": self.to,
                "delay_ms": round(self.delay_ms, 3),
                "target": round(self.target, 4),
            },
            text=f'to="{self.to}" delay={self.delay_ms:.1f}ms target={self.target:.4f}',
        )


@dataclass(frozen=True)
class DlpInit:
    rules: List[str]

    def to_log(self) -> LogRecord:
        rules_list = ", ".join(self.rules)
        return LogRecord(
            action={"domain": "DLP", "name": "INIT"},
            payload={"rules": list(self.rules), "count": len(self.rules)},
            text=f"Loaded {len(self.rules)} active rule(s): {rules_list}",
        )


@dataclass(frozen=True)
class DlpMutate:
    line: int
    rule: str
    token: str
    replacement: str
    action: str = "mask"

    def to_log(self) -> LogRecord:
        return LogRecord(
            action={"domain": "DLP", "name": "MUTATE"},
            payload={
                "line": self.line,
                "rule": self.rule,
                "token": self.token,
                "replacement": self.replacement,
                "action": self.action,
            },
            text=f"line={self.line} rule={self.rule} action={self.action} token={self.token} -> {self.replacement}",
        )


@dataclass(frozen=True)
class DlpAudit:
    violations: int
    lines: int

    def to_log(self) -> LogRecord:
        return LogRecord(
            action={"domain": "DLP", "name": "AUDIT"},
            payload={"violations": self.violations, "lines": self.lines},
            text=f"{self.violations} violation(s) intercepted across {self.lines} line(s)",
        )


@dataclass(frozen=True)
class ScheduleInitLog:
    id: str
    policy: str
    state_file: Optional[str] = None

    def to_log(self) -> LogRecord:
        payload: Dict[str, Any] = {"id": self.id, "policy": self.policy}
        if self.state_file:
            payload["state_file"] = self.state_file
        sf_str = f" state_file={self.state_file}" if self.state_file else ""
        return LogRecord(
            action={"domain": "SCHEDULE", "name": "INIT"},
            payload=payload,
            text=f"id={self.id} policy={self.policy}{sf_str}",
        )


@dataclass(frozen=True)
class ScheduleRuleLog:
    id: str
    expr: str
    tags: List[str]
    grid: Dict[str, Any]

    def to_log(self) -> LogRecord:
        tags_str = ",".join(self.tags)
        return LogRecord(
            action={"domain": "SCHEDULE", "name": "RULE"},
            payload={
                "id": self.id,
                "expr": self.expr,
                "tags": list(self.tags),
                "grid": dict(self.grid),
            },
            text=f'id={self.id} expr="{self.expr}" tags="{tags_str}"',
        )


@dataclass(frozen=True)
class ScheduleHoldLog:
    id: str
    target: float
    delay_ms: float
    tags: List[str]
    grid: Dict[str, Any]

    def to_log(self) -> LogRecord:
        tags_str = ",".join(self.tags)
        return LogRecord(
            action={"domain": "SCHEDULE", "name": "HOLD"},
            payload={
                "id": self.id,
                "target": round(self.target, 4),
                "delay_ms": round(self.delay_ms, 1),
                "tags": list(self.tags),
                "grid": dict(self.grid),
            },
            text=f'id={self.id} target={self.target:.4f} delay={self.delay_ms:.1f}ms tags="{tags_str}"',
        )


@dataclass(frozen=True)
class ScheduleTickLog:
    id: str
    scheduled: str
    lag_ms: float
    tags: List[str]
    status: str
    grid: Dict[str, Any]

    def to_log(self) -> LogRecord:
        tags_str = ",".join(self.tags)
        return LogRecord(
            action={"domain": "SCHEDULE", "name": "TICK"},
            payload={
                "id": self.id,
                "scheduled": self.scheduled,
                "lag_ms": round(self.lag_ms, 3),
                "tags": list(self.tags),
                "status": self.status,
                "grid": dict(self.grid),
            },
            text=f'id={self.id} scheduled={self.scheduled} lag={self.lag_ms:.3f}ms tags="{tags_str}" status={self.status}',
        )


@dataclass(frozen=True)
class ScheduleDumpLog:
    id: str
    checkpoint: str
    rule_count: int
    path: Optional[str] = None

    def to_log(self) -> LogRecord:
        payload: Dict[str, Any] = {
            "id": self.id,
            "checkpoint": self.checkpoint,
            "rule_count": self.rule_count,
        }
        if self.path:
            payload["path"] = self.path
        return LogRecord(
            action={"domain": "SCHEDULE", "name": "DUMP"},
            payload=payload,
            text=f"id={self.id} checkpoint={self.checkpoint} rules={self.rule_count} saved to {self.path or 'log'}",
        )


# ==============================================================================
# Instructions (Incoming from Channel/Codec to Coprocessor)
# ==============================================================================
@dataclass(frozen=True)
class Instruction:
    pass


@dataclass(frozen=True)
class ScheduleInit(Instruction):
    schedule_id: str
    policy: str = "skip"  # "skip" or "catchup"
    state_file: Optional[str] = None


@dataclass(frozen=True)
class ScheduleRule(Instruction):
    schedule_id: str
    expr: str
    tags: str = ""


@dataclass(frozen=True)
class ScheduleWait(Instruction):
    schedule_id: str


@dataclass(frozen=True)
class ScheduleDump(Instruction):
    schedule_id: str
    file_path: Optional[str] = None


@dataclass(frozen=True)
class ScheduleCancel(Instruction):
    schedule_id: str


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
class Shift(Instruction):
    to: str = "*/1s"


@dataclass(frozen=True)
class CancelTimer(Instruction):
    timer_id: str


@dataclass(frozen=True)
class LegacyClock(Instruction):
    interval: float
    cycles: int = 0
    align: Optional[str] = None


@dataclass(frozen=True)
class InitClock(Instruction):
    clock_id: str
    interval: float
    cycles: int = 0
    duration: float = 0.0
    policy: str = "skip"
    align: Optional[str] = None


@dataclass(frozen=True)
class StartClock(Instruction):
    clock_id: str
    align: Optional[str] = None


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
    replacement: Optional[str] = None
    name: Optional[str] = None
    action: str = "mask"  # "mask", "hash", "alias"
    template: Optional[str] = None
    salt: Optional[str] = None


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


@dataclass(frozen=True)
class TimeRequest(Instruction):
    pass


# ==============================================================================
# Events (Outgoing from Coprocessor to Channel/Codec)
# ==============================================================================
@dataclass(frozen=True)
class Event:
    pass


@dataclass(frozen=True)
class TimeResult(Event):
    epoch_ns: int
    wall_time: str
    monotonic_s: float
    target_fd: int = 0


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


@dataclass(frozen=True)
class ScheduleEvent(Event):
    schedule_id: str
    scheduled_iso: str
    lag_ms: float
    tags: str
    status: str = "ok"
    target_fd: int = 0
