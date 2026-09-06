"""
timer.py - Sovereign Timer & Clock Coprocessor.
Computes absolute monotonic deadlines, tracks jitter/lag, and self-heals overruns.
Supports autonomous Push intervals, Pull wait_next_tick, and live introspection.
"""
from dataclasses import dataclass
from typing import Callable, ClassVar, Dict, Optional, Tuple, Type, Union

from harness.coproc.base import BaseCoprocessor, CoprocessorContext
from harness.coproc.registry import register_coprocessor
from harness.core.scheduler import HeapScheduler
from harness.core.model import (
    CancelTimer,
    ClockListRequest,
    ClockListResult,
    ClockStatusRequest,
    ClockStatusResult,
    ClockWaitResult,
    Event,
    InitClock,
    Instruction,
    LegacyClock,
    LegacyTick,
    SetInterval,
    SetTimeout,
    Sleep,
    StartClock,
    Tick,
    Timeout,
    UpdateClock,
    WaitClock,
    Wakeup,
)


@dataclass
class ClockState:
    clock_id: str
    t0: Optional[float] = None
    interval: float = 1.0
    cycles: int = 0
    duration: float = 0.0
    policy: str = "skip"  # "skip" or "catchup"
    last_cycle: int = -1
    started: bool = False


@register_coprocessor
class TimerCoprocessor(BaseCoprocessor):
    handled_instructions: ClassVar[Tuple[Type[Instruction], ...]] = (
        InitClock,
        StartClock,
        UpdateClock,
        WaitClock,
        ClockStatusRequest,
        ClockListRequest,
        SetInterval,
        SetTimeout,
        Sleep,
        CancelTimer,
        LegacyClock,
    )

    def __init__(
        self,
        ctx: Union[CoprocessorContext, HeapScheduler],
        emit_event_fn: Optional[Callable[[Event], None]] = None,
    ):
        if not isinstance(ctx, CoprocessorContext):
            assert isinstance(ctx, HeapScheduler)
            ctx = CoprocessorContext(scheduler=ctx, emit_event=emit_event_fn or (lambda ev: None))
        super().__init__(ctx)
        self.scheduler = ctx.scheduler
        self.emit_event = ctx.emit_event
        self._clocks: Dict[str, ClockState] = {}

    def handle_instruction(self, inst: Instruction) -> None:
        if isinstance(inst, InitClock):
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
        elif isinstance(inst, SetInterval):
            self._start_interval(inst)
        elif isinstance(inst, SetTimeout):
            self._start_timeout(inst)
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
            last_cycle=-1,
            started=False,
        )

    def _start_clock(self, inst: StartClock):
        clock = self._clocks.get(inst.clock_id)
        now = self.scheduler.now()
        if clock is not None:
            clock.t0 = now
            clock.started = True
            clock.last_cycle = -1

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
                last_cycle=-1,
                started=True,
            )
            self._clocks[inst.clock_id] = clock

        # Lazy start if clock:start was omitted
        if not clock.started or clock.t0 is None:
            clock.t0 = now
            clock.started = True
            clock.last_cycle = -1

        # Check total duration limit if configured
        if clock.duration > 0.0 and (now - clock.t0) >= clock.duration:
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

        # Case 1: First call -> Cycle 0 starts immediately at T0
        if clock.last_cycle == -1:
            clock.last_cycle = 0
            if clock.cycles > 0 and clock.cycles <= 0:
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

            def on_schedule_tick():
                wake_now = self.scheduler.now()
                lag_ms = max(0.0, (wake_now - target_time) * 1000.0)
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
            if clock.policy == "skip":
                skipped = int((now - target_time) // clock.interval) + 1
                next_cycle = target_cycle + skipped
                next_target = clock.t0 + (next_cycle * clock.interval)
                clock.last_cycle = next_cycle

                # If the overrun skipped beyond the allowed max cycles
                if clock.cycles > 0 and next_cycle >= clock.cycles:
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

                def on_skipped_tick():
                    wake_now = self.scheduler.now()
                    lag_ms = max(0.0, (wake_now - next_target) * 1000.0)
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
    # Metronome / Interval (Push)
    # --------------------------------------------------------------------------
    def _start_interval(self, inst: SetInterval):
        key = f"interval:{inst.timer_id}"
        t0 = self.scheduler.now()
        first_target = t0 + inst.interval

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

        def on_timeout():
            self.emit_event(Timeout(timer_id=inst.timer_id, target_fd=inst.on_fd))

        self.scheduler.schedule_at(target, key, on_timeout)

    # --------------------------------------------------------------------------
    # Delegated Sleep
    # --------------------------------------------------------------------------
    def _start_sleep(self, inst: Sleep):
        key = f"sleep:{id(inst)}"
        target = self.scheduler.now() + inst.duration

        def on_wakeup():
            self.emit_event(Wakeup())

        self.scheduler.schedule_at(target, key, on_wakeup)

    # --------------------------------------------------------------------------
    # Cancellation
    # --------------------------------------------------------------------------
    def _cancel_timer(self, timer_id: str):
        self.scheduler.cancel(f"interval:{timer_id}")
        self.scheduler.cancel(f"once:{timer_id}")
        self.scheduler.cancel(f"clock_wait:{timer_id}")
        self._clocks.pop(timer_id, None)

    # --------------------------------------------------------------------------
    # Legacy Clock (for clock-tick.sh compatibility)
    # --------------------------------------------------------------------------
    def _start_legacy_clock(self, inst: LegacyClock):
        key = "legacy:clock"
        t0 = self.scheduler.now()

        self.emit_event(LegacyTick(cycle=0, monotonic_ts=t0))
        if inst.cycles == 1:
            return

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

        first_target = t0 + inst.interval
        self.scheduler.schedule_at(
            first_target,
            key,
            lambda: on_legacy_tick(1),
        )
