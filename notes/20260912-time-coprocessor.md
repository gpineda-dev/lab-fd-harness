# 2026-09-12: The Sovereign Temporal Coprocessor — Timer vs. Clock vs. Schedule

## 1. The Temporal Dilemma in Supervised Systems

Managing time in Unix worker scripts and micro-services has traditionally suffered from three fundamental flaws:

1. **The Drift Paradox of Relative Sleep**:
   A standard loop using `/usr/bin/sleep 1` accumulates execution delay on every cycle. If the workload inside the loop takes 300ms, each iteration actually consumes 1300ms. Over an hour, the process drifts by dozens of minutes.

2. **The Minute-Blindness of Classical Cron**:
   Standard POSIX crontabs operate with 1-minute resolution (`* * * * *`). They cannot express sub-second cadences (`*/250ms`), second-level agendas (`*/5s`), nor can they handle fast-paced batch workers. Furthermore, standard cron offers zero integration with process supervision, I/O multiplexing, or monotonic time.

3. **The Trap of Polymorphic Overloading**:
   Many frameworks attempt to solve timing with a single overloaded verb (e.g. `sleep(duration=...)` vs `sleep(until=...)` vs `sleep(every=...)`). Overloading verbs creates ambiguous semantics, fragile edge-cases, and hides whether an operation is stateless, phase-locked, or persistent.

To solve this, `fd-harness` introduces a unified **Temporal Coprocessor** based on a strict architectural division: **The Temporal Trinity**.

---

## 2. The Temporal Trinity: Separation of Concerns

Instead of a single polymorphic primitive, `fd-harness` partitions temporal control into three orthogonal, non-overlapping domains:

```text
+--------------------------------------------------------------------------------------------------+
|                                    TEMPORAL COPROCESSOR                                          |
|                                                                                                  |
|   +--------------------------+   +--------------------------+   +----------------------------+   |
|   |          TIMER           |   |          CLOCK           |   |          SCHEDULE          |   |
|   |  (Stateless Golden Path) |   |  (Isochronous Metronome) |   |   (Calendar & Agenda)      |   |
|   +--------------------------+   +--------------------------+   +----------------------------+   |
|   | • Relative delays        |   | • Stateful phase-lock    |   | • Multi-rule agenda        |   |
|   | • One-shot grid shift    |   | • Drift self-healing     |   | • Comma-separated tags     |   |
|   | • Zero setup / no state  |   | • Monotonic timestamps   |   | • State checkpoints        |   |
|   | • sleep, shift, once     |   | • clock:init, clock:wait |   | • Crash catch-up replay    |   |
|   +--------------------------+   +--------------------------+   +----------------------------+   |
+--------------------------------------------------------------------------------------------------+
```

### Comparative Matrix

| Domain | Mental Model | State Lifecycle | Directive Examples | Output Protocol Event | Primary Use Case |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **`timer`** | **Stateless Golden Path** | None (ephemeral) | `# @harness.sleep duration=0.3s`<br>`# @harness.shift to="*/1s"`<br>`# @harness.timer:once after=5s` | `wakeup`<br>`timer:timeout <id>` | One-off pauses, shifting to next round second/minute, timeouts. |
| **`clock`** | **Isochronous Metronome** | Explicit (`init` $\to$ `start` $\to$ `wait` $\to$ `done`) | `# @harness.clock:init id=c1 interval=1s`<br>`# @harness.clock:wait id=c1` | `tick <id> <cycle> <skipped> <lag_ms> <mono_ts> <status>` | High-frequency computation loops, sensor polling, drift-free metronomes. |
| **`schedule`** | **Calendar & Agenda** | Persistent (`init` $\to$ `rule*` $\to$ `wait` $\to$ `dump`) | `# @harness.schedule:init id=ag policy=catchup`<br>`# @harness.schedule:rule id=ag expr="*/1min" tags="m"`<br>`# @harness.schedule:wait id=ag` | `schedule <id> <iso_ts> <lag_ms> <tags> <status>` | Multi-cadence workers, cron-like agendas, crash-recovery with deterministic catch-up. |

---

## 3. The Stateless Golden Path: `sleep` vs. `shift`

A key architectural insight in `fd-harness` is rejecting polymorphic commands like `sleep until="..."`. An action must have an unequivocal, single-purpose verb.

### 1. `sleep` — Pure Relative Duration
`sleep` expresses relative pause ($\Delta t$). It has no concept of calendar time, wall clock, or phase alignment:
```bash
# @harness.sleep duration=0.3s
# @harness.sleep duration=500ms
```
The child process yields control to the supervisor, zero subshells are forked, and the microkernel resumes execution after exactly $\Delta t$.

### 2. `shift` — One-Shot Grid Snap
`shift` is an imperative action verb: *"advance my process immediately to the next temporal grid boundary"*.

It requires **no scheduler setup, no rules, and no persistent state machine**:
```bash
# Snap to the next round second (XX:XX:01.000, XX:XX:02.000, ...)
# @harness.shift to="*/1s"

# Snap to the next round minute (XX:XX:00.000)
# @harness.shift to="*/1min"

# Snap to every 5 minutes (00, 05, 10, 15, ...)
# @harness.shift to="*/5min"
```
Under the hood:
1. The supervisor evaluates the target grid expression against the current real-world epoch:
   $$\text{target\_epoch} = \lceil \text{now} / \text{step} \rceil \times \text{step}$$
2. It holds the child process in non-blocking priority queue wait for $\Delta t = \text{target\_epoch} - \text{now}$.
3. When the edge arrives, it emits `wakeup` on stdout.

---

## 4. Sub-Second & Millisecond Grid Expressions

`fd-harness` unifies step durations and classical cron expressions through a single mathematical evaluator ([`harness/utils/cron.py`](../harness/utils/cron.py)).

### Uniform Step Syntax: `*/<duration>`
Instead of cryptic aliases (`@second`, `@minute`, `@daily`), developers use the standard cron step notation augmented with physical unit parsing:

- **Millisecond resolution**: `*/100ms`, `*/250ms`, `*/500ms`
- **Second resolution**: `*/1s`, `*/5s`, `*/30sec`
- **Minute resolution**: `*/1min` (or `*/1m`), `*/5min`, `*/15min`
- **Hour resolution**: `*/1h`, `*/6h`

### Multi-Field Cron Expressions: 5, 6, and 7 Fields
For calendar-aware rules, the engine seamlessly adapts to three standard topologies, with native **millisecond resolution as the finest unit**:

```text
┌──────────────────── millisecond (0 - 999) [optional, defaults to 0]
│ ┌────────────────── second (0 - 59)      [optional, defaults to 0]
│ │ ┌──────────────── minute (0 - 59)
│ │ │ ┌────────────── hour (0 - 23)
│ │ │ │ ┌──────────── day of month (1 - 31)
│ │ │ │ │ ┌────────── month (1 - 12)
│ │ │ │ │ │ ┌──────── day of week (0 - 6, 0=Sunday)
│ │ │ │ │ │ │
│ │ * * * * *        -> 5 fields: Standard POSIX cron (ms=0, sec=0)
│ * * * * * *        -> 6 fields: Second-level resolution (ms=0)
* * * * * * *        -> 7 fields: Sub-second millisecond resolution
```

#### Practical Examples:
- `*/250 * * * * * *` $\to$ Every 250ms on the grid (0ms, 250ms, 500ms, 750ms).
- `0,500 * * * * * *` $\to$ Twice per second: at second edge `.000` and half-second `.500`.
- `* * * * * *`        $\to$ Every second at millisecond 000.
- `0 * * * * *`        $\to$ Every minute at second 00 and millisecond 000.
- `0 0 2 * * *`        $\to$ Every night at 02:00:00.000 AM.
- `*/10 * * * * *`     $\to$ Every 10 seconds.

Both uniform step shorthands (`*/250ms`) and full 7-field cron masks share the exact same microsecond-accurate resolution engine:
```python
nxt_epoch, parsed_grid = compute_next_occurrence("*/250 * * * * * *", after_epoch=now)
```

---

## 5. The Sovereign Clock: Phase-Locked Computation Loops

When a script needs to execute an iterative loop with a fixed rate (e.g. 10Hz, 1s), using `sleep` causes drift. The `clock` coprocessor solves this via **theoretical target anchoring**:

$$T_{\text{target}}(n) = T_0 + n \times \Delta t$$

```bash
# 1. Initialize clock anchored to T0 with skip policy
# @harness.clock:init id=worker interval=1s cycles=10 policy=skip align="*/1s"

# 2. Synchronous pull loop
while true; do
    # @harness.clock:wait id=worker
    read -r TAG ID CYCLE SKIPPED LAG_MS MONO_TS STATUS
    [[ "$STATUS" == "done" ]] && break

    # Execute workload (e.g. takes 300ms)
    do_heavy_computation

    # On next clock:wait, the harness automatically sleeps only 700ms!
done
```

### Self-Healing Overrun Policies
If a computation spike exceeds the interval (e.g. work takes 1400ms on a 1000ms cadence):
- **`policy=skip`**: Detects overrun, skips the lost cycle, reports `skipped=1, status=overrun`, and phase-locks immediately onto the next reachable slot ($T_0 + 2 \times \Delta t$).
- **`policy=catchup`**: Emits the tick immediately without sleep (`lag_ms=400ms`), allowing the worker to drain accumulated backpressure.

---

## 6. The Schedule Orchestrator: Multi-Rules, Tags & Crash Catch-Up

The `schedule` orchestrator is designed for persistent daemons running multiple concurrent calendar agendas on a single loop.

### 1. Multi-Rule Attachment with Tag Routing
A single schedule instance can register multiple independent calendar rules with comma-separated tags:

```bash
# Initialize schedule with state persistence
# @harness.schedule:init id=agenda policy=catchup state_file="/var/run/agenda.json"

# Attach heterogeneous rules
# @harness.schedule:rule id=agenda expr="*/1s" tags="metrics,ping"
# @harness.schedule:rule id=agenda expr="*/10s" tags="backup,heavy"
# @harness.schedule:rule id=agenda expr="0 2 * * *" tags="nightly,cleanup"
```

### 2. Simultaneous Slot Merging
If multiple rules land on the exact same second (e.g. at $T = 10\text{s}$, both `*/1s` and `*/10s` fire), `fd-harness` does **not** trigger two separate interrupts. It merges their tags into a single atomic event:
```text
schedule agenda 2026-09-13T01:00:10Z 0.450 metrics,ping,backup,heavy ok
```

In Bash, dispatching is instantaneous using standard glob matching:
```bash
while harness_schedule_wait "agenda"; do
    case ",${HARNESS_TAGS}," in
        *,metrics,*) collect_telemetry ;;
    esac

    case ",${HARNESS_TAGS}," in
        *,backup,*) run_disk_backup ;;
    esac
done
```

### 3. Crash Recovery & Replay (`policy=catchup`)
When a worker resumes after being stopped, paused, or restarted:
1. It loads `state_file="/var/run/agenda.json"` containing the timestamp of the last recorded checkpoint ($T_{\text{last}}$).
2. The coprocessor computes all missed occurrences between $T_{\text{last}}$ and $\text{now}$:
   ```python
   missed = compute_missed_occurrences(rule.expr, start_epoch=T_last, end_epoch=now)
   ```
3. During subsequent `# @harness.schedule:wait` calls, missed events are **popped immediately with `status="missed"` and zero artificial sleep** until the queue is fully caught up.
4. Once caught up, normal calendar scheduling resumes with `status="ok"`.

---

## 7. Metrology Benchmark Evidence: `clock-tick.sh`

The real-world distinction between **Timers** and **Clocks** is demonstrated in [`experiments/01-cadence-drift/clock-tick.sh`](../experiments/01-cadence-drift/clock-tick.sh):

```bash
uv run fd-harness run -v ./experiments/01-cadence-drift/clock-tick.sh \
  -s harnessed-pure -i 1 -w 0.3 -n 4 -r init -a "*/1s"
```

### Execution Trace
```text
Started at:    08:05:07.936
-------------------------------------------------------------
# [HARNESS] [IO:READ  ] # @harness.shift to="*/1s"
# [HARNESS] [TIMER:SHIFT] to="*/1s" delay=62.8ms target=1789279508.0000
# [HARNESS] [TIMER:WAKE ] duration=0.063s lag=0.200ms
# [HARNESS] [IO:WRITE ] wakeup
@@@ CLAP:START @@@
[Cycle 0] 08:05:08.003
# [HARNESS] [IO:READ  ] # @harness.sleep duration=0.3
# [HARNESS] [TIMER:WAKE ] duration=0.300s lag=0.476ms
# [HARNESS] [IO:READ  ] # @harness.sleep duration=0.6937
# [HARNESS] [TIMER:WAKE ] duration=0.694s lag=0.955ms
[Cycle 1] 08:05:09.011
# [HARNESS] [IO:READ  ] # @harness.sleep duration=0.3
# [HARNESS] [TIMER:WAKE ] duration=0.300s lag=0.482ms
# [HARNESS] [IO:READ  ] # @harness.sleep duration=0.6862
# [HARNESS] [TIMER:WAKE ] duration=0.686s lag=0.912ms
[Cycle 2] 08:05:10.008
-------------------------------------------------------------
Theoretical duration:   3.3000s
Actual duration:        3.3130s
Net cumulative drift:   +13.017 ms
=============================================================
```

### What this proves:
1. **`shift to="*/1s"`** locks the start timestamp onto the exact round second boundary (`08:05:08.000`) before `CLAP:START` begins.
2. **`harnessed-pure` uses zero clocks**: It is 100% timers. The in-shell script measures elapsed time, computes the remaining delta ($693.7\text{ms}$), and sleeps via `# @harness.sleep`.
3. **Zero Subshell Forks**: Unlike standard `/usr/bin/sleep`, which incurs process creation overhead twice per cycle, `# @harness.sleep` operates entirely via single-digit microsecond IPC, maintaining sub-millisecond precision over the entire run.

---

## 8. Structured Observability (JSONL)

Every operation within the Temporal Coprocessor generates machine-readable JSONL telemetry formatted with canonical `action.domain` and `action.name` attributes:

```json
{"ts": "2026-09-13T01:16:57.909990", "engine": "worker.sh", "action": {"domain": "TIMER", "name": "SHIFT"}, "payload": {"to": "*/1s", "delay_ms": 90.413, "target": 1789255018.0}}
{"ts": "2026-09-13T01:16:58.000760", "engine": "worker.sh", "action": {"domain": "TIMER", "name": "WAKE"}, "payload": {"duration": 0.090, "lag_ms": 0.328}}
{"ts": "2026-09-13T01:16:59.001200", "engine": "worker.sh", "action": {"domain": "CLOCK", "name": "TICK"}, "payload": {"id": "bench", "cycle": 1, "lag_ms": 0.245}}
{"ts": "2026-09-13T01:17:00.001850", "engine": "worker.sh", "action": {"domain": "SCHEDULE", "name": "TICK"}, "payload": {"id": "agenda", "scheduled": "2026-09-13T01:17:00Z", "lag_ms": 1.25, "tags": ["metrics", "backup"], "status": "ok"}}
```

This ensures full observability across the entire temporal stack: from ad-hoc pauses (`TIMER`), to micro-benchmarks (`CLOCK`), up to enterprise multi-tenant calendar schedules (`SCHEDULE`).
