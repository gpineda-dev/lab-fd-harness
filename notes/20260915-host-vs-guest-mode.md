# Architecture Notes: Host vs. Guest Modes & Deep System Interception (LD_PRELOAD)
**Date:** 2026-09-15  
**Topic:** Harness Execution Topologies, Host vs. Guest Trade-offs, IPC Mechanisms, and Dynamic Library Preloading (OneAgent-grade Granularity)  
**Format:** Direct engineering notes (English, concise, non-fluff)

---

## 1. The Execution Topology Spectrum

Harness operates along an execution continuum ranging from non-intrusive command line utilities to deep dynamic binary instrumentation:

```
[ GUEST MODE ] ◄────────► [ IN-SCRIPT DAEMON ] ◄────────► [ HOST MODE ] ◄────────► [ LD_PRELOAD INJECTOR ]
  (CLI Utility)            (Scoped Background)             (Barrelfish-style)          (Libc Interception)
```

---

## 2. Host Mode (The Sovereign Supervisor)

* **Mechanism:**
  * Harness acts as parent process: `harness run ./script.sh`.
  * Controls physical I/O file descriptors (`stdin=0`, `stdout=1`, `stderr=2`).
  * Allocates dedicated out-of-band IPC channels (FD 3 for control/requests, FD 4 for responses/data).
* **Key Advantages:**
  * **Zero-Fork Execution:** Shell operations (`read -u 3`, `printf -v`) execute purely via shell built-ins. Latency: **~80 µs** vs ~2–5 ms for subshells.
  * **Topology-Level DLP vs. "Mutating FDs":**
    * File descriptors are **not** mutated or patched in-place.
    * The architecture establishes an explicit directional topology:
      $$\text{script:stdout (FD 1)} \xrightarrow{\quad\text{Pipe}\quad} [\text{Harness DLP Engine}] \xrightarrow{\quad\text{Sanitized}\quad} \text{harness:stdout (Real TTY/File)}$$
    * The child script remains completely oblivious: it writes to what it considers standard stdout (FD 1).
    * Harness acts as an **in-flight transformation proxy (Egress Filter)**: it intercepts the child's read-end pipe, processes lines via the Vault/Rules engine, and writes the sanitized stream to the host's actual stdout.
  * **Inviolable Egress Boundary:** Because Harness owns the pipe's consumer end, the child script cannot leak raw secrets to the terminal regardless of internal shell behavior.
  * **Kernel-Spliced Lifecycle:** Parent-child relationship bound by kernel `SIGCHLD` and pipe EOFs. Zero orphan/zombie background processes on failure or `SIGKILL`.
  * **Flicker-Free Presentation (TUI):** Harness holds exclusive PTY raw mode; calculates ANSI matrix diffs in memory (Double Buffering) for 60 FPS UI/game loops.
  * **Multi-Seat / Attach:** Facilitates `harness attach --session ID` across multiple physical terminals/SSH connections.

---

## 3. Guest Mode (The Pragmatic Utility)

* **Mechanism:**
  * Standalone invocation within existing scripts, Makefiles, or CI/CD pipelines: `val=$(harness ds:pop name=q)`.
  * Operates without wrapping the top-level parent process.
* **Trade-Offs & Costs:**
  * **Fork Tax:** Every command incurs process creation (`clone() + execve()`). Unsuitable for tight microsecond loops ($N > 1000$).
  * **Opt-In Redaction:** DLP requires explicit pipeline chaining (`cmd | harness dlp`).
* **Sub-Variant: Scoped In-Script Daemon (Private Session Daemon):**
  * Pattern similar to `ssh-agent`:
    ```bash
    eval "$(harness daemon --env)"
    trap 'harness daemon --stop' EXIT
    ```
  * Boots a transient background engine bound to a private UNIX domain socket (`/tmp/harness_$PID.sock`).
  * Consequent guest CLI calls act as thin RPC clients delegating work to the warm background daemon.
  * Preserves stateful memory (LRU caches, heaps, monotonic timers, DuckDB tables) across discrete shell script lines.
  * Clean teardown guaranteed via POSIX `trap ... EXIT`.

---

## 4. Physical IPC Mechanisms (Beyond Anonymous POSIX Pipes)

| IPC Mechanism | Kernel Primitive | Latency | Bandwidth / Throughput | Best-Fit Architectural Use Case |
| :--- | :--- | :--- | :--- | :--- |
| **POSIX Pipes (FD 3/4)** | Circular kernel buffer (64 KB) | ~80 µs | Moderate (~2–4 GB/s) | Standard Host Mode. Zero-config, native in Bash (`<&3`, `>&3`). |
| **Windows Named Pipes** | `\\.\pipe\...` (IOCP async) | ~50–90 µs | High | Native Windows CMD (`.bat`) and PowerShell cross-platform parity. |
| **POSIX Shared Memory (`shm`)** | `/dev/shm` + `mmap` | **~50–200 ns** | **Bus limit (~50 GB/s)** | Zero-Copy Data Plane. Transferring multi-MB blobs, DuckDB vectors, IPC rings. |
| **UNIX Domain Sockets (`AF_UNIX`)** | Local stream/datagram socket | ~20–40 µs | High | Guest-to-Daemon RPC. Supports descriptor passing (`SCM_RIGHTS`). |
| **Linux `io_uring`** | Shared SQ/CQ ring buffers | < 5 µs | Extreme | High-throughput asynchronous batch event streaming without kernel traps. |
| **POSIX RT Signals** | `SIGRTMIN`..`SIGRTMAX` | ~5–10 µs | Negligible (Payload-free) | Instant asynchronous event wakeups, bypassing polling loops. |

---

## 5. The Deep Interception Frontier: `LD_PRELOAD` (The Thin Shim Architecture)

### 5.1 The "Thin Shim / Fat Engine" Decoupling
* **Anti-Pattern:** Embedding the complete Harness runtime (DuckDB, regex engines, Pratt parsers) into a monolithic C dynamic library (`.so`). Causes memory bloat, dynamic linking delays, and isolated state across sub-processes.
* **Architecture:**
  * **Thin Shim (`libharness_shim.so`):** Ultra-lightweight C library (< 500 LOC, ~30 KB compiled). Zero external dependencies, pure libc entry points.
  * **Fat Engine (`harness daemon`):** High-level core (Go/Rust/Python) running in a separate process space holding all business logic, DLP vaults, and state.
  * **Bridge:** Inter-process communication via local `AF_UNIX` datagram socket or `/dev/shm` ring buffer (sub-microsecond overhead).

### 5.2 Intercepted Primitives & Unlocked Capabilities
By intercepting standard libc symbols via dynamic linker redirection, Harness controls unmodified legacy binaries:

* **I/O & Streams (`write`, `writev`, `read`):**
  * **Ghost DLP:** Intercepts output buffers before they reach kernel descriptor tables. Purges secrets even if a binary writes directly to `/dev/tty` or bypasses normal pipes.
  * **On-the-Fly Path Remapping:** Rewrites file paths and connection strings in memory before execution.
* **Time & Cadence (`nanosleep`, `clock_gettime`, `gettimeofday`):**
  * **Time-Warp / Acceleration:** Intercepts `sleep 60` in integration tests; advances virtual clock immediately or scales wait time (e.g. 10x warp factor).
  * **Deterministic Clock:** Eliminates platform jitter and guarantees synchronized cadence without shell modifications.
* **Network & Sockets (`socket`, `connect`, `sendto`, `recvfrom`):**
  * **User-Space Chaos Injection:** Simulates connection drops, packet corruption, and latency jitter without `root` privileges or kernel `iptables`/`netem` rules.
* **Process Lifecycle (`clone`, `fork`, `execve`):**
  * Tracks full process trees, captures subprocess metrics, and prevents fork-bomb denial-of-service conditions.

### 5.3 OneAgent-Grade Observability vs. Active Control
* **Industry Standard (Dynatrace OneAgent / Datadog Tracer):** Operates primarily in **passive observation** mode (instrumenting call stacks, measuring spans, forwarding APM metrics to cloud collectors).
* **Harness Interception:** Acts as a **bi-directional active coprocessor**:
  1. **Mutates streams in-flight:** Reversible pseudonymization, format-preserving encryption.
  2. **Alters execution flow:** Short-circuits slow operations, applies backpressure, injects rate-limiting.
  3. **Feeds in-memory analytics:** Telemetry events directly populate in-memory DuckDB tables for real-time SQL introspection.

### 5.4 Limitations & Constraints
* **Static Binaries:** Completely ineffective against static binaries (e.g. Go compiled with `CGO_ENABLED=0`, Rust using `musl`) as they bypass `ld.so`.
* **SUID Binaries:** The Linux kernel automatically ignores `LD_PRELOAD` for binaries with SUID permissions (`sudo`, `passwd`) for security boundary enforcement.
