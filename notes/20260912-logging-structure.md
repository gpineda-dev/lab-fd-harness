# 2026-09-12: Structured Observability & Logging Architecture

## 1. The Observability Challenge in Process Supervision

Supervising black-box or brownfield scripts via file descriptors introduces unique observability challenges that standard application logging frameworks (e.g. `log4j`, Python `logging`, Syslog) cannot address:

1. **The Stream Pollution Dilemma**:
   When a supervisor multiplexes control directives (`# @harness.*`) and returns protocol messages (`tick`, `wakeup`, `calc`) over standard POSIX descriptors (FD 0, 1, 3, 4), printing diagnostic messages carelessly to `stdout` will corrupt downstream pipelines (e.g. `fd-harness run worker.sh | jq .` or CSV parsers).

2. **The Mismatch of Severity Levels (`DEBUG` / `INFO` / `WARN` / `ERROR`)**:
   In an event-driven coprocessor kernel, "severity" is an inadequate mental model. A metronome tick or an IPC pipe write is neither "INFO" nor "DEBUG"—it is a domain-specific telemetric event. Operators need to slice observability by **subsystem/domain** (`clock`, `timer`, `io`, `dlp`, `schedule`), not arbitrary subjective severity ranks.

3. **Dual Audience: Human Interactive DX vs. Machine Ingestion**:
   Developers debugging shell scripts interactively require colorized, microsecond-accurate terminal traces formatted as non-intrusive comments (`# [HARNESS] ...`). In contrast, production daemons and CI runners require structured, append-only NDJSON (JSON Lines) streaming to files or log collectors (Vector, Fluentbit, Datadog) with zero overhead.

To solve this, `fd-harness` implements a dedicated, strongly typed logging pipeline centered around four core CLI controls: `-v`, `-t`, `-f`, and `--log-level`.

---

## 2. CLI Control Flags: Summary & Matrix

| Flag | Long Form | Default | Supported Values | Primary Purpose |
| :--- | :--- | :--- | :--- | :--- |
| **`-v`** | `--show-directives` | `False` | Boolean flag | **Interactive DX macro**: Enables console tracing of intercepted directives and supervisor actions. Automatically defaults `--log-level` to `all` if unset. |
| **`-t`** | `--log-target` | `:stdout` | `:stdout`, `:stderr`, `<filepath>` | **Stream routing & pipeline safety**: Directs log output to standard streams or a dedicated log file on disk. |
| **`-f`** | `--log-format` | `text` | `text`, `jsonl` | **Serialization format**: Human-readable colorized text vs. canonical machine-readable JSONL. |
| **`--log-level`** | `--log-level` | `None` (`all` if `-v`) | Comma-separated subsystems: `all`, `io`, `timer`, `clock`, `schedule`, `dlp`, `bus`, `calc` | **Subsystem slicing**: Granular filtering of supervisor events by architectural domain. |

---

## 3. The Design Rationale: "The Why"

### A. Why Subsystem-Based Levels Instead of Severity Levels?
Traditional frameworks force developers to categorize events into arbitrary severity tiers (`DEBUG`, `INFO`, `WARN`, `ERROR`). In `fd-harness`:
- An I/O read on FD 1 is not "DEBUG", nor is a clock tick "INFO".
- Slicing by severity is noisy: enabling `DEBUG` floods the terminal with megabytes of IPC reads when the operator only wanted to inspect clock overruns.

Instead, `fd-harness` models log levels as **orthogonal functional subsystems**:
- `--log-level io`: Traces low-level descriptor reads and writes across FD 0, 1, 3, and 4.
- `--log-level timer`: Traces stateless pauses (`sleep`), grid alignments (`shift`), and one-shots (`once`).
- `--log-level clock`: Traces isochronous metronomes (`init`, `start`, `tick`, `overrun`, `status`).
- `--log-level schedule`: Traces multi-rule calendar agendas, tag matches, and catch-up holds.
- `--log-level dlp`: Traces sensitive pattern detections, token redaction, and data leaks.
- `--log-level all`: Activates all registered coprocessors simultaneously.

Subsystems can be combined dynamically via comma separation:
```bash
uv run fd-harness run -v --log-level "clock,timer" worker.sh
```

### B. Why `-v` is an Interactive DX Macro
In standard CLI tools, `-v` stands for "verbose". In `fd-harness`:
1. **Zero-Configuration Onboarding**: Developers running a test script do not want to memorize `--log-level all --log-format text --log-target :stdout`. Providing `-v` immediately lights up full visibility into what the supervisor is doing.
2. **Comment-Decorated Output (`# [HARNESS]`)**: All console text lines are prefixed with `# [HARNESS]`. In Unix conventions, `#` indicates comments. If a child process produces structured data (e.g. CSV or space-delimited records), harness comments can be trivially filtered or ignored by downstream utilities:
   ```bash
   grep -v '^# [HARNESS]'
   ```
3. **ANSI Dim Styling (`\033[90m`)**: Visual hierarchy is maintained. Child application output remains in bright, high-contrast text, while supervisor telemetry is dimmed in gray so it does not distract the human operator.

### C. Why Tri-Modal Targets (`:stdout`, `:stderr`, `<filepath>`)?
Stream routing is critical for POSIX composability:
1. **`:stdout` (Default interactive mode)**:
   Convenient for interactive runs where human observation is the primary goal.
2. **`:stderr` (Pipeline safety)**:
   If a supervised script outputs clean data to stdout to be piped into another program:
   ```bash
   uv run fd-harness run -v -t :stderr generate-json.sh | jq .
   ```
   Directing harness logs to `:stderr` guarantees that `jq` receives 100% pure JSON on stdin without syntax errors caused by harness trace lines.
3. **`<filepath>` (Headless & audit persistence)**:
   In production daemon mode or automated benchmarks, telemetry must be persisted asynchronously:
   ```bash
   uv run fd-harness run -t /var/log/harness/worker.jsonl -f jsonl --log-level all worker.sh
   ```
   The logger automatically creates parent directories and opens the file handle in append (`"a"`) mode with clean buffering.

### D. Why Dual Serialization (`text` vs. `jsonl`)?
1. **`text` format**:
   Optimized for human comprehension:
   ```text
   # [HARNESS] 2026-09-13 01:26:36.941069 [clock-tick.sh] [TIMER:SHIFT] to="*/1s" delay=59.5ms target=1789255597.0000
   # [HARNESS] 2026-09-13 01:26:37.000976 [clock-tick.sh] [TIMER:WAKE ] duration=0.060s lag=0.289ms
   ```
   Features fixed-width action alignment (`[TIMER:SHIFT]`, `[TIMER:WAKE ]`, `[IO:READ  FD 1]`) and local microsecond timestamps.

2. **`jsonl` format**:
   Optimized for streaming telemetry ingestion:
   ```json
   {"ts": "2026-09-13T01:26:36.941069", "engine": "clock-tick.sh", "action": {"domain": "TIMER", "name": "SHIFT"}, "payload": {"to": "*/1s", "delay_ms": 59.5, "target": 1789255597.0}}
   ```
   No multi-line blobs, no regex parsing required. Each line is an independent JSON document containing strict typing (numeric floats for latencies, string tags, boolean flags).

---

## 4. Architecture & Pipeline Plumbing

Logging in `fd-harness` is **event-driven and model-backed**, not string-formatted at the call site.

```text
+-----------------------------------------------------------------------------------------+
|                                    EVENT SOURCES                                        |
|                                                                                         |
|   FDChannel (I/O)       TimerCoprocessor      ClockCoprocessor      DlpCoprocessor      |
|   IORead, IOWrite       TimerSleep, Shift     ClockTick, Overrun    DLPLeak, Sanitize   |
+-----------------------------------------------------------------------------------------+
                                             |
                                             v  .to_log()
                               +---------------------------+
                               |     LogRecord Model       |
                               |  - action: {domain, name} |
                               |  - payload: {...}         |
                               |  - text: "..."            |
                               +---------------------------+
                                             |
                                             v
                               +---------------------------+
                               |       HarnessLogger       |
                               |  - is_enabled(subsystem)? |
                               +---------------------------+
                                             |
                      +----------------------+----------------------+
                      | (format: "text")                            | (format: "jsonl")
                      v                                             v
         +--------------------------+                  +--------------------------+
         | ANSI Formatted String    |                  | Canonical JSON Object    |
         | # [HARNESS] [DOMAIN:ACT] |                  | {"ts", "engine", ...}    |
         +--------------------------+                  +--------------------------+
                      |                                             |
                      +----------------------+----------------------+
                                             |
                                             v
                      +---------------------------------------------+
                      |               Target Stream                 |
                      |    :stdout   |   :stderr   |   file.jsonl   |
                      +---------------------------------------------+
```

### The `to_log()` Contract
Every first-class domain model implements the `.to_log()` protocol method:
```python
@dataclass(frozen=True)
class LogRecord:
    action: Dict[str, Any]      # {"domain": "TIMER", "name": "SHIFT", "fd": 1}
    payload: Dict[str, Any]     # Typed key-value properties
    text: str                   # Human-readable textual representation

    @property
    def subsystem(self) -> str:
        return self.action.get("domain", "")
```

When an event occurs in a coprocessor:
```python
self.logger.log(TimerShift(to="*/1s", delay_ms=59.5, target=1789255597.0), engine=self.engine_name)
```
1. `HarnessLogger.log()` invokes `obj.to_log()`.
2. It checks `is_enabled(record.subsystem)` against the active `--log-level`.
3. If active, it dispatches to the formatter (`text` or `jsonl`) and writes to the configured target stream.

---

## 5. Standard Recipes & Use Cases

### Recipe 1: Interactive Debugging (Verbose Terminal)
Observe all directives and coprocessor responses while running a script:
```bash
uv run fd-harness run -v ./experiments/01-cadence-drift/clock-tick.sh -s harnessed-pure
```

### Recipe 2: Pipeline-Safe Execution with Stderr Logs
Run a script producing data on stdout while keeping supervisor logs visible on stderr:
```bash
uv run fd-harness run -v -t :stderr ./scripts/dump-records.sh | jq '.items | length'
```

### Recipe 3: Production Metrology & Telemetry File
Run headless in production, logging only clock metrology and timing drift to a JSONL audit file:
```bash
uv run fd-harness run \
  --log-level "clock,timer" \
  -t /var/log/telemetry/cadence.jsonl \
  -f jsonl \
  ./services/worker.sh
```

### Recipe 4: Security / DLP Audit Stream
Capture all data-loss-prevention intercepts and secret leak alerts:
```bash
uv run fd-harness redact \
  -r ./dlp-rules.toml \
  --fail-on-leak \
  -t /var/log/audit/dlp-leaks.jsonl \
  -f jsonl \
  -- ./bin/export-sensitive-data
```

---

## 6. Canonical JSONL Schema Specification

Every JSONL entry produced by `HarnessLogger` adheres to the following specification:

```json
{
  "ts": "2026-09-13T01:26:36.941069",
  "engine": "clock-tick.sh",
  "action": {
    "domain": "TIMER",
    "name": "SHIFT",
    "fd": null
  },
  "payload": {
    "to": "*/1s",
    "delay_ms": 59.5,
    "target": 1789255597.0
  }
}
```

### Field Definitions:
- **`ts`** (`string`, ISO 8601): Microsecond-resolution timestamp of event occurrence.
- **`engine`** (`string`): Identifier of the engine/process instance (configured or script name).
- **`action`** (`object`):
  - **`domain`** (`string`): The architectural subsystem (`IO`, `TIMER`, `CLOCK`, `SCHEDULE`, `DLP`, `BUS`, `CALC`).
  - **`name`** (`string`): The specific operation (`READ`, `WRITE`, `SHIFT`, `WAKE`, `TICK`, `OVERRUN`, `HOLD`, `SCAN`, `LEAK`).
  - **`fd`** (`integer | null`): File descriptor index when the action is tied to I/O (e.g. `0`, `1`, `3`, `4`).
- **`payload`** (`object`): Arbitrary strongly-typed event data (durations in float seconds, delays in float milliseconds, arrays of tags, status strings).
