# 2026-09-05: Conceptual Architecture & The Coprocessor Paradigm

## 1. Vision & Philosophy

Modern infrastructure is glued together by brownfield shell scripts and legacy binaries that orchestrate deployments, telemetry gathering, periodic maintenance, and distributed tasks.

While these scripts are ubiquitous, they were never designed for modern reliability requirements:
* **No non-blocking IPC**: Inter-process coordination in Bash is traditionally simulated via brittle temporary files, named pipes (`mkfifo`), or polling loops.
* **Lack of sovereign timekeeping & metrology**: Shell scripts cannot schedule self-correcting monotonic intervals or track timing jitter without spawning external subshells (`fork()` + `execve()`) on every tick.
* **Security & Data Sanitization**: Scrubbing sensitive secrets (tokens, API keys) or audit logging typically requires invasive pipeline rewrites.

The goal of `fd-harness` is **not to rewrite legacy software**, but to embrace the **Strangler Fig pattern** at the process level:
An unprivileged wrapper starts the legacy script, transparently instruments its standard POSIX file descriptors, and acts as an **external sovereign coprocessor**.

---

## 2. Conceptual Architecture & File Descriptor Topology

The diagram below illustrates how `fd-harness` encapsulates and supervises a target program:

```text
+-------------------------------------------------------------------------------+
|                               HOST / OPERATOR                                 |
|                                                                               |
|   Terminal Input (stdin)                          Terminal Output (stdout)    |
|            |                                                 ^                |
+------------|-------------------------------------------------|----------------+
             |                                                 |
             v                                                 | [Sanitized Output]
+--------------------------------------------------------------|----------------+
|                        fd-harness SUPERVISOR                 |                |
|                                                              |                |
|  +--------------------+                     +----------------+-------------+  |
|  | TerminalLineEditor |                     |    DLP / Stream Filter       |  |
|  | (Raw Mode & Hist)  |                     | (Regex Masking, Audit Logs)  |  |
|  +---------+----------+                     +----------------+-------------+  |
|            |                                                 ^                |
|            |                                                 | (App stdout)   |
|            |                                                 |                |
|            |   +---------------------------------------------+-------------+  |
|            |   |                   FDChannel Router                        |  |
|            |   | - Intercepts in-band directives (# @harness.*)            |  |
|            |   | - Forwards sanitized child output to DLP pipeline         |  |
|            |   | - Decodes instructions into typed models                  |  |
|            |   +------+-------------------+-------------------+------------+  |
|            |          |                   |                   |               |
|            |          v                   v                   v               |
|            |   +--------------+   +---------------+   +---------------+       |
|            |   |    Timer     |   |  Pratt / Calc |   |   Event Bus   |       |
|            |   |  Coprocessor |   |  Coprocessor  |   |  Coprocessor  |       |
|            |   | (Clocks,     |   | (Scientific   |   | (In-Memory    |       |
|            |   |  Jitter,     |   |  eval, vars,  |   |  Pub/Sub,     |       |
|            |   |  Overruns)   |   |  templates)   |   |  routing)     |       |
|            |   +------+-------+   +-------+-------+   +-------+-------+       |
|            |          |                   |                   |               |
|            |          +-------------------+-------------------+               |
|            |                              | (Events & Responses)              |
|            |                              v                                   |
|            |               +-------------------------------+                  |
|            |               |    Codec & Event Serializer   |                  |
|            |               |  (Positional Space-Delimited) |                  |
|            |               +--------------+----------------+                  |
|            |                              |                                   |
|            | [FD 4: User Input]           | [FD 0: Control Plane & Responses] |
|            |                              | [FD 3: Async Event Stream]        |
|            v                              v                                   |
|  +-------------------------------------------------------------------------+  |
|  |                        SUPERVISED CHILD PROCESS                         |  |
|  |               (Bash Script, Legacy Binary, Python, Perl)                |  |
|  |                                                                         |  |
|  |  FD 0 (stdin)  <-- read -r TAG ... (Synchronous command responses)      |  |
|  |  FD 1 (stdout) --> Directives (# @harness.*) & standard child logs       |  |
|  |  FD 2 (stderr) --> Inherited or redirected error stream                  |  |
|  |  FD 3 (events) <-- Dedicated data plane (autonomous ticks, bus topics)  |  |
|  |  FD 4 (user)   <-- Proxied human interactive input (isolated from FD 0) |  |
|  +-------------------------------------------------------------------------+  |
+-------------------------------------------------------------------------------+
```

---

## 3. The Core Abstractions

### A. Dedicated File Descriptor Plumbing
Rather than multiplexing everything over stdin/stdout (which creates race conditions between human keyboard input and control plane events), `fd-harness` arranges a dedicated topology:

* **FD 0 (Control Plane In)**: Dedicated to synchronous coprocessor responses (`tick ...`, `calc ...`, `wakeup`).
* **FD 1 (Control Plane Out & Logs)**: Intercepts `# @harness.*` directives. Lines not matching the harness prefix are passed through to the DLP pipeline.
* **FD 3 (Data Plane - Event Stream)**: Dedicated asynchronous pipe for background intervals, autonomous metronome ticks, and bus pub/sub subscriptions. The child can read events on FD 3 without blocking FD 0.
* **FD 4 (Human Input Plane)**: Proxied terminal input managed by `TerminalLineEditor`. Prevents interactive prompts from colliding with coprocessor events.

### B. In-Band Directives & Zero-Eval Protocol
Communication between the child process and the harness adheres to two fundamental design constraints:

1. **Non-Invasive In-Band Annotations**:
   The script invokes coprocessor features by printing structured comments to stdout:
   ```bash
   echo "# @harness.clock:init id=bench interval=1s policy=skip"
   echo "# @harness.calc expr=\"78 * 9\" store=\"k\""
   ```
   If executed outside the harness, these lines are ignored by the shell as inert comments. Under the harness, they are intercepted before reaching the terminal.

2. **Positional, Zero-Eval Responses**:
   The harness serializes events into clean, space-delimited columns:
   ```text
   tick bench 1 0 0.412 202291.9968 ok
   calc k=702
   ```
   The child extracts values natively using standard shell builtins with zero `eval` and zero subshell forks:
   ```bash
   read -r _TAG ID GRID_CYCLE SKIPPED LAG_MS MONO_TS STATUS
   ```

### C. Domain Coprocessors

* **Timer & Metrology Coprocessor**:
  Manages multiple sovereign clocks pegged to monotonic time (`CLOCK_MONOTONIC`). Provides jitter calculation (`lag_ms`), self-healing overrun policies (`skip` vs `catchup`), and push/pull cadencing.
* **Scientific Pratt & Calculator Coprocessor**:
  Offloads arithmetic, physical unit conversions (`1s / 60`, `500ms * 2`), scientific functions (`sin`, `sqrt`, `log`), and variable registers (`k=702`).
* **In-Memory Event Bus Coprocessor**:
  Provides lightweight topic-based pub/sub (`bus:subscribe`, `bus:emit`) routed through FD 3, allowing multiple routines or processes to exchange telemetry asynchronously.
* **DLP & Stream Redaction Interceptor**:
  Inspects child stdout in real time, applies regex masks to redact sensitive secrets before they reach the operator's screen, and produces audit events.

---

## 4. Summary

`fd-harness` turns standard POSIX file descriptors into a sovereign coprocessing bus. By decoupling timekeeping, computation, event pub/sub, and sanitization from the child process, legacy scripts can achieve modern reliability and metrology guarantees without rewriting a single line of business logic.
