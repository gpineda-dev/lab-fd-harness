"""
codec.py - Codec translating raw text lines into typed Instructions,
and serializing Events into clean, space-delimited positional protocol strings.
Zero eval required in Bash: read -r TAG ... natively extracts columns.
"""
import shlex
from typing import Any, Dict, Optional, Tuple

from harness.core.model import (
    BusEmit,
    BusEvent,
    BusSubscribe,
    CalcRequest,
    CalcResult,
    CalcListRequest,
    CalcListResult,
    CancelTimer,
    ClockListRequest,
    ClockListResult,
    ClockStatusRequest,
    ClockStatusResult,
    ClockWaitResult,
    Event,
    FilterMask,
    InitClock,
    Instruction,
    LegacyClock,
    LegacyTick,
    LogMessage,
    ScheduleCancel,
    ScheduleDump,
    ScheduleEvent,
    ScheduleInit,
    ScheduleRule,
    ScheduleWait,
    SetInterval,
    SetTimeout,
    Shift,
    Sleep,
    SprintRequest,
    SprintResult,
    StartClock,
    Tick,
    TimeRequest,
    TimeResult,
    Timeout,
    UpdateClock,
    WaitClock,
    Wakeup,
)

PREFIX = "# @harness."


from harness.utils.pratt import evaluate_temporal


def parse_duration(val: Any) -> float:
    """Parses a duration using strict TemporalGrammar (e.g. '1s / 60', '500ms', '2m + 30s')."""
    if isinstance(val, (int, float)):
        return float(val)

    s = str(val).strip().lower()
    if not s or s == "0":
        return 0.0

    try:
        return evaluate_temporal(s)
    except Exception:
        # Fallback for simple numeric string
        return float(s)


class AnnotationCodec:
    """Default text codec parsing # @harness directives."""

    def decode(self, line: str) -> Optional[Instruction]:
        stripped = line.strip()
        if not stripped.startswith(PREFIX):
            return None

        content = stripped[len(PREFIX):].strip()
        if not content:
            return None

        parts = shlex.split(content)
        cmd = parts[0]
        kwargs: Dict[str, Any] = {}

        for part in parts[1:]:
            if "=" in part:
                k, v = part.split("=", 1)
                kwargs[k] = v
            else:
                kwargs[part] = True

        return self._build_instruction(cmd, kwargs)

    def _build_instruction(self, cmd: str, kwargs: Dict[str, Any]) -> Optional[Instruction]:
        if cmd == "clock:init":
            clock_id = str(kwargs.get("id", "main"))
            interval = parse_duration(kwargs.get("interval", kwargs.get("every", 1.0)))
            cycles = int(kwargs.get("cycles", kwargs.get("n", 0)))
            duration = parse_duration(kwargs.get("duration", kwargs.get("timeout", 0.0)))
            policy = str(kwargs.get("policy", "skip"))
            align = str(kwargs["align"]) if "align" in kwargs else None
            return InitClock(
                clock_id=clock_id,
                interval=interval,
                cycles=cycles,
                duration=duration,
                policy=policy,
                align=align,
            )

        elif cmd == "clock:start":
            clock_id = str(kwargs.get("id", "main"))
            align = str(kwargs.get("align", kwargs.get("at", ""))) or None
            return StartClock(clock_id=clock_id, align=align)

        elif cmd == "clock:update":
            clock_id = str(kwargs.get("id", "main"))
            interval = parse_duration(kwargs["interval"]) if "interval" in kwargs else None
            expire_at = kwargs.get("expire_at")
            cycles = int(kwargs["cycles"]) if "cycles" in kwargs else None
            duration = parse_duration(kwargs["duration"]) if "duration" in kwargs else None
            policy = str(kwargs["policy"]) if "policy" in kwargs else None
            return UpdateClock(
                clock_id=clock_id,
                interval=interval,
                expire_at=expire_at,
                cycles=cycles,
                duration=duration,
                policy=policy,
            )

        elif cmd == "clock:wait":
            clock_id = str(kwargs.get("id", "main"))
            return WaitClock(clock_id=clock_id)

        elif cmd == "clock:status":
            clock_id = str(kwargs.get("id", "main"))
            return ClockStatusRequest(clock_id=clock_id)

        elif cmd == "clock:list":
            return ClockListRequest()

        elif cmd in ("clock:every", "clock:legacy", "clock"):
            interval = parse_duration(kwargs.get("interval", kwargs.get("every", 1.0)))
            cycles = int(kwargs.get("cycles", kwargs.get("n", 0)))
            align = str(kwargs["align"]) if "align" in kwargs else None
            return LegacyClock(interval=interval, cycles=cycles, align=align)

        elif cmd == "timer:interval":
            timer_id = str(kwargs.get("id", "default"))
            interval = parse_duration(kwargs.get("every", kwargs.get("interval", 1.0)))
            cycles = int(kwargs.get("cycles", 0))
            policy = str(kwargs.get("policy", "skip"))
            on_fd = int(kwargs.get("on_fd", kwargs.get("fd", 0)))
            return SetInterval(
                timer_id=timer_id,
                interval=interval,
                cycles=cycles,
                policy=policy,
                on_fd=on_fd,
            )

        elif cmd == "timer:once":
            timer_id = str(kwargs.get("id", "default"))
            after = parse_duration(kwargs.get("after", kwargs.get("duration", 1.0)))
            on_fd = int(kwargs.get("on_fd", kwargs.get("fd", 0)))
            return SetTimeout(timer_id=timer_id, after=after, on_fd=on_fd)

        elif cmd == "timer:cancel":
            timer_id = str(kwargs.get("id", ""))
            return CancelTimer(timer_id=timer_id)

        elif cmd in ("sleep", "timer:sleep"):
            duration = parse_duration(kwargs.get("duration", 0.0))
            return Sleep(duration=duration)

        elif cmd in ("shift", "timer:shift"):
            to = str(kwargs.get("to", kwargs.get("expr", kwargs.get("align", "*/1s"))))
            return Shift(to=to)

        elif cmd == "schedule:init":
            schedule_id = str(kwargs.get("id", "default"))
            policy = str(kwargs.get("policy", "skip"))
            state_file = str(kwargs["state_file"]) if "state_file" in kwargs else None
            return ScheduleInit(schedule_id=schedule_id, policy=policy, state_file=state_file)

        elif cmd in ("schedule:rule", "schedule:at"):
            schedule_id = str(kwargs.get("id", "default"))
            expr = str(kwargs.get("expr", kwargs.get("cron", "* * * * * *")))
            tags = str(kwargs.get("tags", kwargs.get("label", "")))
            return ScheduleRule(schedule_id=schedule_id, expr=expr, tags=tags)

        elif cmd == "schedule:wait":
            schedule_id = str(kwargs.get("id", "default"))
            return ScheduleWait(schedule_id=schedule_id)

        elif cmd == "schedule:dump":
            schedule_id = str(kwargs.get("id", "default"))
            file_path = str(kwargs.get("file", kwargs.get("path", ""))) or None
            return ScheduleDump(schedule_id=schedule_id, file_path=file_path)

        elif cmd == "schedule:cancel":
            schedule_id = str(kwargs.get("id", "default"))
            return ScheduleCancel(schedule_id=schedule_id)

        elif cmd in ("calc:list", "calc:vars"):
            return CalcListRequest()

        elif cmd == "calc":
            expr = str(kwargs.get("expr", ""))
            store = kwargs.get("store", kwargs.get("into"))
            return CalcRequest(expr=expr, store=str(store) if store else None)

        elif cmd == "sprint":
            template = str(kwargs.get("text", kwargs.get("template", "")))
            return SprintRequest(template=template)

        elif cmd == "filter:mask":
            pattern = str(kwargs.get("pattern", kwargs.get("regex", "")))
            action = str(kwargs.get("action", "mask"))
            template = kwargs.get("template")
            replacement = kwargs.get("replacement", kwargs.get("replace"))
            name = str(kwargs["name"]) if "name" in kwargs else None
            salt = str(kwargs["salt"]) if "salt" in kwargs else None
            return FilterMask(
                pattern=pattern,
                replacement=str(replacement) if replacement is not None else None,
                name=name,
                action=action,
                template=str(template) if template is not None else None,
                salt=salt,
            )

        elif cmd == "bus:subscribe":
            topic = str(kwargs.get("topic", kwargs.get("name", "*")))
            return BusSubscribe(topic=topic)

        elif cmd == "bus:emit":
            topic = str(kwargs.get("topic", kwargs.get("name", "default")))
            payload = str(kwargs.get("payload", kwargs.get("msg", kwargs.get("data", ""))))
            return BusEmit(topic=topic, payload=payload)

        elif cmd in ("log", "print"):
            text = str(kwargs.get("text", kwargs.get("msg", "")))
            level = str(kwargs.get("level", "INFO"))
            return LogMessage(text=text, level=level)

        elif cmd in ("time", "now", "date"):
            return TimeRequest()

        return None

    def encode(self, event: Event) -> str:
        if isinstance(event, TimeResult):
            return f"time {event.epoch_ns} {event.wall_time} {event.monotonic_s:.4f}\n"
        elif isinstance(event, ClockWaitResult):
            return f"tick {event.clock_id} {event.cycle} {event.skipped} {event.lag_ms:.3f} {event.monotonic_ts:.4f} {event.status}\n"
        elif isinstance(event, ClockStatusResult):
            return f"clock:status {event.clock_id} {event.interval:.3f} {event.elapsed_s:.3f} {event.last_cycle} {event.max_cycles} {event.status}\n"
        elif isinstance(event, ClockListResult):
            return f"clock:list {event.clocks} {event.count}\n"
        elif isinstance(event, CalcResult):
            return f"calc {event.value}\n"
        elif isinstance(event, CalcListResult):
            return f"calc:vars {event.variables} {event.count}\n"
        elif isinstance(event, SprintResult):
            return f"sprint {event.text}\n"
        elif isinstance(event, Tick):
            return f"timer:tick {event.timer_id} {event.cycle} {event.lag_ms:.3f}\n"
        elif isinstance(event, Timeout):
            return f"timer:timeout {event.timer_id}\n"
        elif isinstance(event, Wakeup):
            return "wakeup\n"
        elif isinstance(event, BusEvent):
            return f"bus:event {event.topic} {event.payload}\n"
        elif isinstance(event, ScheduleEvent):
            return f"schedule {event.schedule_id} {event.scheduled_iso} {event.lag_ms:.3f} {event.tags} {event.status}\n"
        elif isinstance(event, LegacyTick):
            return f"tick {event.cycle} {event.monotonic_ts:.4f}\n"
        return ""

