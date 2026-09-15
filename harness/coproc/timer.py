"""
timer.py - Sovereign Timer & Clock Coprocessor.
Computes absolute monotonic deadlines, tracks jitter/lag, and self-heals overruns.
Supports autonomous Push intervals, Pull wait_next_tick, and live introspection.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import math
import os
import time
from typing import Any, Callable, ClassVar, Dict, List, Optional, Tuple, Type, Union

from harness.coproc.base import BaseCoprocessor, CoprocessorContext
from harness.coproc.registry import register_coprocessor
from harness.core.codec import parse_duration
from harness.core.scheduler import HeapScheduler
from harness.utils.cron import (
    compute_missed_occurrences,
    compute_next_occurrence,
    parse_cron_expression,
)
from harness.core.model import (
    CancelTimer,
    ClockDone,
    ClockHold,
    ClockInit,
    ClockListRequest,
    ClockListResult,
    ClockOverrun,
    ClockStart,
    ClockStatusRequest,
    ClockStatusResult,
    ClockTick,
    ClockUpdate,
    ClockWaitResult,
    Event,
    InitClock,
    Instruction,
    LegacyClock,
    LegacyTick,
    ScheduleCancel,
    ScheduleDump,
    ScheduleDumpLog,
    ScheduleEvent,
    ScheduleHoldLog,
    ScheduleInit,
    ScheduleInitLog,
    ScheduleRule,
    ScheduleRuleLog,
    ScheduleTickLog,
    ScheduleWait,
    SetInterval,
    SetTimeout,
    Shift,
    Sleep,
    StartClock,
    Tick,
    Timeout,
    TimeRequest,
    TimeResult,
    TimerInterval,
    TimerShift,
    TimerSleep,
    TimerTimeout,
    TimerWake,
    UpdateClock,
    WaitClock,
    Wakeup,
)


def compute_align_delay(align_expr: str, now_epoch: Optional[float] = None) -> float:
    """
    Computes the delay in seconds until the next grid phase based on align_expr.
    Supports cron expressions, aliases (@second, @minute), steps ('*/1s', '*/100ms'), or duration syntax.
    """
    if now_epoch is None:
        now_epoch = time.time()

    try:
        next_epoch, _ = compute_next_occurrence(align_expr, after_epoch=now_epoch)
        delay = next_epoch - now_epoch
        return max(0.0, delay)
    except Exception:
        return 0.0


@dataclass
class ClockState:
    clock_id: str
    t0: Optional[float] = None
    interval: float = 1.0
    cycles: int = 0
    duration: float = 0.0
    policy: str = "skip"  # "skip" or "catchup"
    align: Optional[str] = None
    last_cycle: int = -1
    started: bool = False


@dataclass
class ScheduleRuleState:
    expr: str
    tags: List[str]
    grid: Dict[str, Any]


@dataclass
class MissedScheduleEvent:
    timestamp: float
    tags: List[str]
    grid: Dict[str, Any]


@dataclass
class ScheduleState:
    schedule_id: str
    policy: str = "skip"  # "skip" or "catchup"
    state_file: Optional[str] = None
    rules: List[ScheduleRuleState] = field(default_factory=list)
    last_checkpoint: Optional[float] = None
    missed_queue: List[MissedScheduleEvent] = field(default_factory=list)


@register_coprocessor
class TimerCoprocessor(BaseCoprocessor):
    handled_instructions: ClassVar[Tuple[Type[Instruction], ...]] = (
        InitClock,
        StartClock,
        UpdateClock,
        WaitClock,
        ClockStatusRequest,
        ClockListRequest,
        ScheduleInit,
        ScheduleRule,
        ScheduleWait,
        ScheduleDump,
        ScheduleCancel,
        SetInterval,
        SetTimeout,
        Shift,
        Sleep,
        CancelTimer,
        LegacyClock,
        TimeRequest,
    )

    def __init__(
        self,
        ctx: Union[CoprocessorContext, HeapScheduler],
        emit_event_fn: Optional[Callable[[Event], None]] = None,
        epoch_fn: Optional[Callable[[], float]] = None,
    ):
        if not isinstance(ctx, CoprocessorContext):
            assert isinstance(ctx, HeapScheduler)
            ctx = CoprocessorContext(scheduler=ctx, emit_event=emit_event_fn or (lambda ev: None))
        super().__init__(ctx)
        self.scheduler = ctx.scheduler
        self.emit_event = ctx.emit_event
        self._epoch_fn = epoch_fn or time.time
        self._clocks: Dict[str, ClockState] = {}
        self._schedules: Dict[str, ScheduleState] = {}

    def handle_instruction(self, inst: Instruction) -> None:
        if isinstance(inst, TimeRequest):
            now_ns = time.time_ns()
            wall_str = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            mono_s = round(time.monotonic(), 4)
            self.emit_event(TimeResult(epoch_ns=now_ns, wall_time=wall_str, monotonic_s=mono_s))
        elif isinstance(inst, InitClock):
            self._init_clock(inst)
        elif isinstance(inst, StartClock):
            self._start_clock(inst)
        elif isinstance(inst, UpdateClock):
            self._update_clock(inst)
        elif isinstance(inst, WaitClock):
            self._wait_clock(inst)
        elif isinstance(inst, ClockStatusRequest):
            self._status_clock(inst)
        elif isinstance(inst, ClockListRequest):
            self._list_clocks(inst)
        elif isinstance(inst, ScheduleInit):
            self._init_schedule(inst)
        elif isinstance(inst, ScheduleRule):
            self._rule_schedule(inst)
        elif isinstance(inst, ScheduleWait):
            self._wait_schedule(inst)
        elif isinstance(inst, ScheduleDump):
            self._dump_schedule(inst)
        elif isinstance(inst, ScheduleCancel):
            self._cancel_schedule(inst)
        elif isinstance(inst, SetInterval):
            self._start_interval(inst)
        elif isinstance(inst, SetTimeout):
            self._start_timeout(inst)
        elif isinstance(inst, Shift):
            self._start_shift(inst)
        elif isinstance(inst, Sleep):
            self._start_sleep(inst)
        elif isinstance(inst, CancelTimer):
            self._cancel_timer(inst.timer_id)
        elif isinstance(inst, LegacyClock):
            self._start_legacy_clock(inst)

    # --------------------------------------------------------------------------
    # Pull Synchronizer (clock:init, clock:start, clock:update, clock:wait)
    # --------------------------------------------------------------------------
    def _init_clock(self, inst: InitClock):
        self._clocks[inst.clock_id] = ClockState(
            clock_id=inst.clock_id,
            t0=None,
            interval=inst.interval,
            cycles=inst.cycles,
            duration=inst.duration,
            policy=inst.policy,
            align=inst.align,
            last_cycle=-1,
            started=False,
        )
        delay = compute_align_delay(inst.align, now_epoch=self._epoch_fn()) if inst.align else None
        self.ctx.log(
            ClockInit(
                id=inst.clock_id,
                interval=inst.interval,
                cycles=inst.cycles,
                policy=inst.policy,
                align=inst.align,
                phase_delay_ms=delay * 1000.0 if delay is not None else None,
            )
        )

    def _start_clock(self, inst: StartClock):
        clock = self._clocks.get(inst.clock_id)
        now = self.scheduler.now()
        if clock is not None:
            align_expr = inst.align or clock.align
            delay = compute_align_delay(align_expr, now_epoch=self._epoch_fn()) if align_expr else 0.0
            clock.t0 = now + delay
            clock.started = True
            clock.last_cycle = -1
            self.ctx.log(ClockStart(id=inst.clock_id, t0=clock.t0))

    def _update_clock(self, inst: UpdateClock):
        clock = self._clocks.get(inst.clock_id)
        if clock is None:
            return

        now = self.scheduler.now()

        if inst.interval is not None and inst.interval > 0:
            if clock.started and clock.t0 is not None and clock.last_cycle >= 0:
                # Phase continuity: anchor new interval to the theoretical timestamp of the last tick
                last_theoretical_time = clock.t0 + (clock.last_cycle * clock.interval)
                clock.t0 = last_theoretical_time - (clock.last_cycle * inst.interval)
            clock.interval = inst.interval

        if inst.expire_at is not None:
            if str(inst.expire_at).strip().lower() == "now":
                clock.cycles = max(0, clock.last_cycle + 1)
            else:
                clock.cycles = int(inst.expire_at)

        if inst.cycles is not None:
            clock.cycles = inst.cycles

        if inst.duration is not None:
            clock.duration = inst.duration

        if inst.policy is not None:
            clock.policy = inst.policy

        self.ctx.log(
            ClockUpdate(
                id=clock.clock_id,
                interval=clock.interval,
                cycles=clock.cycles,
            )
        )

    def _wait_clock(self, inst: WaitClock):
        clock = self._clocks.get(inst.clock_id)
        now = self.scheduler.now()

        if clock is None:
            clock = ClockState(
                clock_id=inst.clock_id,
                t0=now,
                interval=1.0,
                cycles=0,
                duration=0.0,
                policy="skip",
                align=None,
                last_cycle=-1,
                started=True,
            )
            self._clocks[inst.clock_id] = clock

        # Lazy start if clock:start was omitted
        if not clock.started or clock.t0 is None:
            delay = compute_align_delay(clock.align, now_epoch=self._epoch_fn()) if clock.align else 0.0
            clock.t0 = now + delay
            clock.started = True
            clock.last_cycle = -1

        # Check total duration limit if configured
        if clock.duration > 0.0 and (now - clock.t0) >= clock.duration:
            self.ctx.log(ClockDone(id=clock.clock_id, reason="duration limit reached", duration=clock.duration))
            self.emit_event(
                ClockWaitResult(
                    clock_id=clock.clock_id,
                    cycle=max(0, clock.last_cycle),
                    skipped=0,
                    lag_ms=0.0,
                    monotonic_ts=now,
                    status="done",
                )
            )
            return

        # Case 1: First call -> Cycle 0
        if clock.last_cycle == -1:
            target_time = clock.t0
            clock.last_cycle = 0

            # If T0 is in the future (due to grid alignment delay), schedule tick for T0!
            if now < target_time:
                delay_ms = (target_time - now) * 1000.0
                self.ctx.log(
                    ClockHold(
                        id=clock.clock_id,
                        cycle=0,
                        target=target_time,
                        delay_ms=delay_ms,
                        align=clock.align,
                        phase_lock=True,
                    )
                )

                def on_first_tick():
                    wake_now = self.scheduler.now()
                    lag_ms = max(0.0, (wake_now - target_time) * 1000.0)
                    self.ctx.log(ClockTick(id=clock.clock_id, cycle=0, lag_ms=lag_ms))
                    self.emit_event(
                        ClockWaitResult(
                            clock_id=clock.clock_id,
                            cycle=0,
                            skipped=0,
                            lag_ms=lag_ms,
                            monotonic_ts=wake_now,
                            status="ok" if (clock.cycles == 0 or clock.cycles > 1) else "done",
                        )
                    )

                self.scheduler.schedule_at(target_time, f"clock_wait:{clock.clock_id}", on_first_tick)
                return

            if clock.cycles > 0 and clock.cycles <= 0:
                self.ctx.log(ClockDone(id=clock.clock_id, reason="cycle budget reached"))
                self.emit_event(
                    ClockWaitResult(
                        clock_id=clock.clock_id,
                        cycle=0,
                        skipped=0,
                        lag_ms=0.0,
                        monotonic_ts=now,
                        status="done",
                    )
                )
                return

            self.ctx.log(ClockTick(id=clock.clock_id, cycle=0, lag_ms=0.0))
            self.emit_event(
                ClockWaitResult(
                    clock_id=clock.clock_id,
                    cycle=0,
                    skipped=0,
                    lag_ms=0.0,
                    monotonic_ts=now,
                    status="ok",
                )
            )
            return

        # Case 2: Subsequent calls -> Cycle 1, 2, ...
        target_cycle = clock.last_cycle + 1
        key = f"clock_wait:{clock.clock_id}"

        # If target cycle reaches or exceeds max cycles budget
        if clock.cycles > 0 and target_cycle >= clock.cycles:
            self.ctx.log(ClockDone(id=clock.clock_id, reason="cycle budget reached", cycles=clock.cycles))
            self.emit_event(
                ClockWaitResult(
                    clock_id=clock.clock_id,
                    cycle=target_cycle,
                    skipped=0,
                    lag_ms=0.0,
                    monotonic_ts=now,
                    status="done",
                )
            )
            return

        target_time = clock.t0 + (target_cycle * clock.interval)

        if now <= target_time:
            # On schedule
            clock.last_cycle = target_cycle
            delay_ms = (target_time - now) * 1000.0
            self.ctx.log(
                ClockHold(
                    id=clock.clock_id,
                    cycle=target_cycle,
                    target=target_time,
                    delay_ms=delay_ms,
                )
            )

            def on_schedule_tick():
                wake_now = self.scheduler.now()
                lag_ms = max(0.0, (wake_now - target_time) * 1000.0)
                self.ctx.log(ClockTick(id=clock.clock_id, cycle=target_cycle, lag_ms=lag_ms))
                self.emit_event(
                    ClockWaitResult(
                        clock_id=clock.clock_id,
                        cycle=target_cycle,
                        skipped=0,
                        lag_ms=lag_ms,
                        monotonic_ts=wake_now,
                        status="ok",
                    )
                )

            self.scheduler.schedule_at(target_time, key, on_schedule_tick)
        else:
            # Overrun detected!
            overrun_lag_ms = (now - target_time) * 1000.0
            if clock.policy == "skip":
                skipped = int((now - target_time) // clock.interval) + 1
                next_cycle = target_cycle + skipped
                next_target = clock.t0 + (next_cycle * clock.interval)
                clock.last_cycle = next_cycle

                self.ctx.log(
                    ClockOverrun(
                        id=clock.clock_id,
                        cycle=target_cycle,
                        lag_ms=overrun_lag_ms,
                        policy="skip",
                        skipped=skipped,
                        next_cycle=next_cycle,
                    )
                )

                # If the overrun skipped beyond the allowed max cycles
                if clock.cycles > 0 and next_cycle >= clock.cycles:
                    self.ctx.log(ClockDone(id=clock.clock_id, reason="skipped beyond cycle budget", cycles=clock.cycles))
                    self.emit_event(
                        ClockWaitResult(
                            clock_id=clock.clock_id,
                            cycle=next_cycle,
                            skipped=skipped,
                            lag_ms=0.0,
                            monotonic_ts=now,
                            status="done",
                        )
                    )
                    return

                delay_ms = (next_target - now) * 1000.0
                self.ctx.log(
                    ClockHold(
                        id=clock.clock_id,
                        cycle=next_cycle,
                        target=next_target,
                        delay_ms=delay_ms,
                    )
                )

                def on_skipped_tick():
                    wake_now = self.scheduler.now()
                    lag_ms = max(0.0, (wake_now - next_target) * 1000.0)
                    self.ctx.log(
                        ClockTick(
                            id=clock.clock_id,
                            cycle=next_cycle,
                            lag_ms=lag_ms,
                            skipped=skipped,
                        )
                    )
                    self.emit_event(
                        ClockWaitResult(
                            clock_id=clock.clock_id,
                            cycle=next_cycle,
                            skipped=skipped,
                            lag_ms=lag_ms,
                            monotonic_ts=wake_now,
                            status="overrun",
                        )
                    )

                self.scheduler.schedule_at(next_target, key, on_skipped_tick)
            else:
                # Catchup policy: immediate return without sleep
                clock.last_cycle = target_cycle
                lag_ms = (now - target_time) * 1000.0
                self.ctx.log(
                    ClockOverrun(
                        id=clock.clock_id,
                        cycle=target_cycle,
                        lag_ms=lag_ms,
                        policy="catchup",
                    )
                )
                self.ctx.log(ClockTick(id=clock.clock_id, cycle=target_cycle, lag_ms=lag_ms))
                self.emit_event(
                    ClockWaitResult(
                        clock_id=clock.clock_id,
                        cycle=target_cycle,
                        skipped=0,
                        lag_ms=lag_ms,
                        monotonic_ts=now,
                        status="overrun",
                    )
                )

    # --------------------------------------------------------------------------
    # Live Introspection (clock:status & clock:list)
    # --------------------------------------------------------------------------
    def _status_clock(self, inst: ClockStatusRequest):
        clock = self._clocks.get(inst.clock_id)
        now = self.scheduler.now()

        if clock is None:
            self.emit_event(
                ClockStatusResult(
                    clock_id=inst.clock_id,
                    interval=0.0,
                    elapsed_s=0.0,
                    last_cycle=0,
                    max_cycles=0,
                    status="not_found",
                )
            )
            return

        elapsed = (now - clock.t0) if (clock.started and clock.t0 is not None) else 0.0
        status = "done" if (clock.cycles > 0 and clock.last_cycle >= clock.cycles) else ("running" if clock.started else "idle")
        self.emit_event(
            ClockStatusResult(
                clock_id=clock.clock_id,
                interval=clock.interval,
                elapsed_s=elapsed,
                last_cycle=max(0, clock.last_cycle),
                max_cycles=clock.cycles,
                status=status,
            )
        )

    def _list_clocks(self, _inst: ClockListRequest):
        clock_names = ",".join(self._clocks.keys()) if self._clocks else "none"
        self.emit_event(
            ClockListResult(
                clocks=clock_names,
                count=len(self._clocks),
            )
        )

    # --------------------------------------------------------------------------
    # Schedule / Agenda Orchestrator (schedule:init, schedule:rule, schedule:wait, schedule:dump, schedule:cancel)
    # --------------------------------------------------------------------------
    def _init_schedule(self, inst: ScheduleInit):
        last_checkpoint: Optional[float] = None
        restored_rules: List[ScheduleRuleState] = []
        policy = inst.policy

        if inst.state_file and os.path.isfile(inst.state_file):
            try:
                with open(inst.state_file, "r") as f:
                    data = json.load(f)
                raw_cp = data.get("last_checkpoint")
                if raw_cp is not None:
                    if isinstance(raw_cp, (int, float)):
                        last_checkpoint = float(raw_cp)
                    elif isinstance(raw_cp, str):
                        try:
                            last_checkpoint = datetime.fromisoformat(raw_cp).timestamp()
                        except Exception:
                            last_checkpoint = float(raw_cp)
                if policy == "skip" and data.get("policy"):
                    policy = str(data["policy"])
                for r in data.get("rules", []):
                    rexpr = r.get("expr")
                    rtags_raw = r.get("tags", "")
                    if rexpr:
                        rtags = [t.strip() for t in str(rtags_raw).split(",") if t.strip()]
                        parsed_grid, _ = parse_cron_expression(rexpr)
                        restored_rules.append(ScheduleRuleState(expr=rexpr, tags=rtags, grid=parsed_grid))
            except Exception:
                pass

        sched = ScheduleState(
            schedule_id=inst.schedule_id,
            policy=policy,
            state_file=inst.state_file,
            rules=restored_rules,
            last_checkpoint=last_checkpoint,
        )
        self._schedules[inst.schedule_id] = sched

        if sched.policy == "catchup" and last_checkpoint is not None and restored_rules:
            now_epoch = self._epoch_fn()
            for r in restored_rules:
                self._enqueue_missed_for_rule(sched, r, start_epoch=last_checkpoint, end_epoch=now_epoch)

        self.ctx.log(
            ScheduleInitLog(
                id=inst.schedule_id,
                policy=sched.policy,
                state_file=inst.state_file,
            )
        )

    def _enqueue_missed_for_rule(
        self,
        sched: ScheduleState,
        rule: ScheduleRuleState,
        start_epoch: float,
        end_epoch: float,
    ) -> None:
        missed_epochs = compute_missed_occurrences(rule.expr, start_epoch=start_epoch, end_epoch=end_epoch)
        for ts in missed_epochs:
            existing = None
            for m in sched.missed_queue:
                if abs(m.timestamp - ts) < 0.001:
                    existing = m
                    break
            if existing:
                for tag in rule.tags:
                    if tag not in existing.tags:
                        existing.tags.append(tag)
                existing.grid.update(rule.grid)
            else:
                sched.missed_queue.append(
                    MissedScheduleEvent(
                        timestamp=ts,
                        tags=list(rule.tags),
                        grid=dict(rule.grid),
                    )
                )
        sched.missed_queue.sort(key=lambda m: m.timestamp)

    def _rule_schedule(self, inst: ScheduleRule):
        sched = self._schedules.get(inst.schedule_id)
        if sched is None:
            sched = ScheduleState(schedule_id=inst.schedule_id)
            self._schedules[inst.schedule_id] = sched

        tags_list = [t.strip() for t in inst.tags.split(",") if t.strip()]
        parsed_grid, _ = parse_cron_expression(inst.expr)

        existing = next((r for r in sched.rules if r.expr == inst.expr), None)
        if existing:
            for t in tags_list:
                if t not in existing.tags:
                    existing.tags.append(t)
            existing.grid = parsed_grid
            rule_state = existing
        else:
            rule_state = ScheduleRuleState(expr=inst.expr, tags=tags_list, grid=parsed_grid)
            sched.rules.append(rule_state)

        if sched.policy == "catchup" and sched.last_checkpoint is not None:
            now_epoch = self._epoch_fn()
            self._enqueue_missed_for_rule(sched, rule_state, start_epoch=sched.last_checkpoint, end_epoch=now_epoch)

        self.ctx.log(
            ScheduleRuleLog(
                id=inst.schedule_id,
                expr=inst.expr,
                tags=tags_list,
                grid=parsed_grid,
            )
        )

    def _wait_schedule(self, inst: ScheduleWait):
        sched = self._schedules.get(inst.schedule_id)
        if sched is None:
            sched = ScheduleState(schedule_id=inst.schedule_id)
            self._schedules[inst.schedule_id] = sched

        key = f"schedule_wait:{sched.schedule_id}"

        # 1. Missed queue replay (for catchup mode upon resume)
        if sched.missed_queue:
            missed = sched.missed_queue.pop(0)
            now_epoch = self._epoch_fn()
            lag_ms = max(0.0, (now_epoch - missed.timestamp) * 1000.0)
            sched.last_checkpoint = missed.timestamp
            self._save_state_if_needed(sched)

            scheduled_iso = datetime.fromtimestamp(missed.timestamp, tz=timezone.utc).isoformat()
            tags_str = ",".join(missed.tags) if missed.tags else "-"

            self.ctx.log(
                ScheduleTickLog(
                    id=sched.schedule_id,
                    scheduled=scheduled_iso,
                    lag_ms=lag_ms,
                    tags=missed.tags,
                    status="missed",
                    grid=missed.grid,
                )
            )
            self.emit_event(
                ScheduleEvent(
                    schedule_id=sched.schedule_id,
                    scheduled_iso=scheduled_iso,
                    lag_ms=lag_ms,
                    tags=tags_str,
                    status="missed",
                )
            )
            return

        # 2. No rules configured
        if not sched.rules:
            now_epoch = self._epoch_fn()
            now_iso = datetime.fromtimestamp(now_epoch, tz=timezone.utc).isoformat()
            self.emit_event(
                ScheduleEvent(
                    schedule_id=sched.schedule_id,
                    scheduled_iso=now_iso,
                    lag_ms=0.0,
                    tags="-",
                    status="no_rules",
                )
            )
            return

        # 3. Schedule next future occurrence across all rules
        now_epoch = self._epoch_fn()
        if sched.last_checkpoint is None:
            sched.last_checkpoint = now_epoch

        candidates: List[Tuple[float, ScheduleRuleState, Dict[str, Any]]] = []
        for r in sched.rules:
            nxt_epoch, parsed_grid = compute_next_occurrence(r.expr, after_epoch=now_epoch)
            candidates.append((nxt_epoch, r, parsed_grid))

        candidates.sort(key=lambda c: c[0])
        earliest_epoch = candidates[0][0]

        combined_tags: List[str] = []
        combined_grid: Dict[str, Any] = {}
        for ep, r, grid in candidates:
            if abs(ep - earliest_epoch) < 0.001:
                for t in r.tags:
                    if t not in combined_tags:
                        combined_tags.append(t)
                combined_grid.update(grid)

        delay = max(0.0, earliest_epoch - now_epoch)
        delay_ms = delay * 1000.0

        self.ctx.log(
            ScheduleHoldLog(
                id=sched.schedule_id,
                target=earliest_epoch,
                delay_ms=delay_ms,
                tags=combined_tags,
                grid=combined_grid,
            )
        )

        def on_schedule_tick():
            wake_epoch = self._epoch_fn()
            lag_ms = max(0.0, (wake_epoch - earliest_epoch) * 1000.0)
            sched.last_checkpoint = earliest_epoch
            self._save_state_if_needed(sched)

            scheduled_iso = datetime.fromtimestamp(earliest_epoch, tz=timezone.utc).isoformat()
            tags_str = ",".join(combined_tags) if combined_tags else "-"

            self.ctx.log(
                ScheduleTickLog(
                    id=sched.schedule_id,
                    scheduled=scheduled_iso,
                    lag_ms=lag_ms,
                    tags=combined_tags,
                    status="ok",
                    grid=combined_grid,
                )
            )
            self.emit_event(
                ScheduleEvent(
                    schedule_id=sched.schedule_id,
                    scheduled_iso=scheduled_iso,
                    lag_ms=lag_ms,
                    tags=tags_str,
                    status="ok",
                )
            )

        self.scheduler.schedule_after(delay, key, on_schedule_tick)

    def _dump_schedule(self, inst: ScheduleDump):
        sched = self._schedules.get(inst.schedule_id)
        if sched is None:
            return

        file_path = inst.file_path or sched.state_file
        cp_epoch = sched.last_checkpoint if sched.last_checkpoint is not None else self._epoch_fn()
        cp_iso = datetime.fromtimestamp(cp_epoch, tz=timezone.utc).isoformat()

        data = {
            "schedule_id": sched.schedule_id,
            "policy": sched.policy,
            "last_checkpoint": cp_epoch,
            "checkpoint_iso": cp_iso,
            "rules": [{"expr": r.expr, "tags": ",".join(r.tags)} for r in sched.rules],
        }

        if file_path:
            parent_dir = os.path.dirname(os.path.abspath(file_path))
            if parent_dir:
                os.makedirs(parent_dir, exist_ok=True)
            with open(file_path, "w") as f:
                json.dump(data, f, indent=2)

        self.ctx.log(
            ScheduleDumpLog(
                id=sched.schedule_id,
                checkpoint=cp_iso,
                rule_count=len(sched.rules),
                path=file_path,
            )
        )

    def _save_state_if_needed(self, sched: ScheduleState) -> None:
        if not sched.state_file:
            return
        try:
            cp_epoch = sched.last_checkpoint if sched.last_checkpoint is not None else self._epoch_fn()
            cp_iso = datetime.fromtimestamp(cp_epoch, tz=timezone.utc).isoformat()
            data = {
                "schedule_id": sched.schedule_id,
                "policy": sched.policy,
                "last_checkpoint": cp_epoch,
                "checkpoint_iso": cp_iso,
                "rules": [{"expr": r.expr, "tags": ",".join(r.tags)} for r in sched.rules],
            }
            parent_dir = os.path.dirname(os.path.abspath(sched.state_file))
            if parent_dir:
                os.makedirs(parent_dir, exist_ok=True)
            with open(sched.state_file, "w") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass

    def _cancel_schedule(self, inst: ScheduleCancel):
        self.scheduler.cancel(f"schedule_wait:{inst.schedule_id}")
        self._schedules.pop(inst.schedule_id, None)

    # --------------------------------------------------------------------------
    # Metronome / Interval (Push)
    # --------------------------------------------------------------------------
    def _start_interval(self, inst: SetInterval):
        key = f"interval:{inst.timer_id}"
        t0 = self.scheduler.now()
        first_target = t0 + inst.interval
        self.ctx.log(TimerInterval(id=inst.timer_id, interval=inst.interval, cycles=inst.cycles))

        def on_tick(cycle: int, scheduled_target: float):
            now = self.scheduler.now()
            lag_ms = max(0.0, (now - scheduled_target) * 1000.0)
            self.emit_event(
                Tick(
                    timer_id=inst.timer_id,
                    cycle=cycle,
                    lag_ms=lag_ms,
                    target_fd=inst.on_fd,
                )
            )

            next_cycle = cycle + 1
            if inst.cycles > 0 and next_cycle >= inst.cycles:
                return

            next_target = t0 + ((next_cycle + 1) * inst.interval)

            if inst.policy == "skip" and now > next_target:
                skipped = int((now - scheduled_target) // inst.interval)
                next_cycle += skipped
                next_target = t0 + ((next_cycle + 1) * inst.interval)

            self.scheduler.schedule_at(
                next_target,
                key,
                lambda: on_tick(next_cycle, next_target),
            )

        self.scheduler.schedule_at(
            first_target,
            key,
            lambda: on_tick(0, first_target),
        )

    # --------------------------------------------------------------------------
    # One-shot Timeout
    # --------------------------------------------------------------------------
    def _start_timeout(self, inst: SetTimeout):
        key = f"once:{inst.timer_id}"
        target = self.scheduler.now() + inst.after
        self.ctx.log(TimerTimeout(id=inst.timer_id, after=inst.after, target=target))

        def on_timeout():
            self.emit_event(Timeout(timer_id=inst.timer_id, target_fd=inst.on_fd))

        self.scheduler.schedule_at(target, key, on_timeout)

    # --------------------------------------------------------------------------
    # Delegated Sleep & Grid Shift (Stateless Golden Path)
    # --------------------------------------------------------------------------
    def _start_sleep(self, inst: Sleep):
        key = f"sleep:{id(inst)}"
        target = self.scheduler.now() + inst.duration
        self.ctx.log(TimerSleep(duration=inst.duration, target=target))

        def on_wakeup():
            wake_now = self.scheduler.now()
            lag_ms = max(0.0, (wake_now - target) * 1000.0)
            self.ctx.log(TimerWake(duration=inst.duration, lag_ms=lag_ms))
            self.emit_event(Wakeup())

        self.scheduler.schedule_at(target, key, on_wakeup)

    def _start_shift(self, inst: Shift):
        key = f"shift:{id(inst)}"
        now_epoch = self._epoch_fn()
        delay = compute_align_delay(inst.to, now_epoch=now_epoch)
        delay_ms = delay * 1000.0
        target_epoch = now_epoch + delay
        target_mono = self.scheduler.now() + delay

        self.ctx.log(TimerShift(to=inst.to, delay_ms=delay_ms, target=target_epoch))

        def on_shift():
            wake_now = self.scheduler.now()
            lag_ms = max(0.0, (wake_now - target_mono) * 1000.0)
            self.ctx.log(TimerWake(duration=delay, lag_ms=lag_ms))
            self.emit_event(Wakeup())

        if delay > 0.0:
            self.scheduler.schedule_at(target_mono, key, on_shift)
        else:
            on_shift()

    # --------------------------------------------------------------------------
    # Cancellation
    # --------------------------------------------------------------------------
    def _cancel_timer(self, timer_id: str):
        self.scheduler.cancel(f"interval:{timer_id}")
        self.scheduler.cancel(f"once:{timer_id}")
        self.scheduler.cancel(f"clock_wait:{timer_id}")
        self.scheduler.cancel(f"schedule_wait:{timer_id}")
        self._clocks.pop(timer_id, None)
        self._schedules.pop(timer_id, None)

    # --------------------------------------------------------------------------
    # Legacy Clock (for clock-tick.sh compatibility)
    # --------------------------------------------------------------------------
    def _start_legacy_clock(self, inst: LegacyClock):
        key = "legacy:clock"
        now = self.scheduler.now()
        delay = compute_align_delay(inst.align, now_epoch=self._epoch_fn()) if inst.align else 0.0
        t0 = now + delay

        def on_legacy_tick(cycle: int):
            now_ts = self.scheduler.now()
            self.emit_event(LegacyTick(cycle=cycle, monotonic_ts=now_ts))

            next_cycle = cycle + 1
            if inst.cycles > 0 and next_cycle >= inst.cycles:
                return

            next_target = t0 + (next_cycle * inst.interval)
            self.scheduler.schedule_at(
                next_target,
                key,
                lambda: on_legacy_tick(next_cycle),
            )

        if delay > 0.0:
            self.scheduler.schedule_at(t0, key, lambda: on_legacy_tick(0))
        else:
            on_legacy_tick(0)
