# fd-harness

> **Reactive Stream Interposition Harness & Coprocessor Supervisor for Unix File Descriptors**

[![Series: First Principles](https://img.shields.io/badge/Series-First%20Principles-blue)](https://g.pineda.me/en/tags/file_descriptor/)
[![Status: Experimental Lab](https://img.shields.io/badge/Status-Research%20Lab%20%2F%20PoC-orange)](#experimental-status--disclaimer)
[![License: MIT](https://img.shields.io/badge/License-MIT-green)](LICENSE)

`fd-harness` is a lightweight, zero-dependency process supervisor that interposes itself between the outer environment and one or more supervised child processes. 

By treating standard file descriptors (`stdin`, `stdout`, `stderr`) as a bidirectional, reactive communication channel, `fd-harness` enables **real-time in-flight stream mutation**, **in-band IPC through log annotations**, and **specialized coprocessor routing** without requiring external SDKs, sidecars, or source code modifications.

---

> [!CAUTION] **Experimental Status & Disclaimer**  
> `fd-harness` is an **exploratory research lab, proof-of-concept, and educational playground** designed to investigate Unix and OS primitives from first principles. It is **NOT intended for production deployment as an off-the-shelf security product**. APIs, configuration formats, and internal interfaces are subject to breaking changes without notice. Use at your own risk for study, experimentation, and research.

---

## 📖 Deep-Dive Articles & Companion Labs

This repository serves as the reference implementation for the **First Principles Systems Series**:

* 📄 **Act I (Essay & Autopsy):** [What If Everything (Really) Were Just a File Descriptor? Act I: The Control Plane of stdout](https://g.pineda.me/en/posts/2026-09-18-harness-introduction-with-dlp/) *(🇫🇷 [Version Française](https://g.pineda.me/fr/posts/2026-09-18-harness-introduction-with-dlp/))*
* 📜 **Manifesto:** [The Craftsman's Exoskeleton: AI as a Mechanical Amplifier, Not an Oracle](https://g.pineda.me/en/posts/2026-09-19-ai-exoskelton-for-builders-not-oracle/) *(🇫🇷 [Version Française](https://g.pineda.me/fr/posts/2026-09-19-ai-exoskelton-for-builders-not-oracle/))*
* 🧪 **Companion Reproducible Labs:**
  * [`01-systemd-socket-bash-server`](https://github.com/gpineda-dev/lab-first-principles-samples/tree/main/01-systemd-socket-bash-server) — Raw socket activation & kernel autopsy.
  * [`02-bash-ansi-csi`](https://github.com/gpineda-dev/lab-first-principles-samples/tree/main/02-bash-ansi-csi) — Zero-fork ANSI CSI terminal state machine.
  * [`03-systemd-socket-dlp-harness`](https://github.com/gpineda-dev/lab-first-principles-samples/tree/main/03-systemd-socket-dlp-harness) — Transparent network membrane under `systemd.socket`.
  * [`04-sudoers-dlp-bastion`](https://github.com/gpineda-dev/lab-first-principles-samples/tree/main/04-sudoers-dlp-bastion) — SRE bastion pattern & `/etc/sudoers` delegation.

---

## The Mental Model

```
 [ External World (Terminal, Upstream Pipes, Log Sinks) ]
         ▲                               │
  stdout │ (Filtered / Mutated)    stdin │ (Forwarded / Injected)
         │                               ▼
 ┌───────────────────────────────────────────────────────────────┐
 │                   fd-harness Supervisor                       │
 │                                                               │
 │   ┌───────────────────────────────────────────────────────┐   │
 │   │               HarnessEngine (Instance N)              │   │
 │   │                                                       │   │
 │   │   ┌───────────────────────────────────────────────┐   │   │
 │   │   │     Stream Interceptor & Mutation Layer       │   │   │
 │   │   │                                               │   │   │
 │   │   │  • Intercepts in-band annotations (# @harness)│   │   │
 │   │   │  • Applies live stream mutations on stdout    │   │   │
 │   │   └───────────────────────▲───────────────────────┘   │   │
 │   │                           │                           │   │
 │   │                 [ CoprocessorRouter ]                 │   │
 │   │                  ├── DlpCoprocessor (Sanitizer)       │   │
 │   │                  ├── TimerCoprocessor (Virtual Clock) │   │
 │   │                  └── ... (Custom Coprocessors)        │   │
 │   └───────────────────────────┬───────────────────────────┘   │
 └───────────────────────────────┼───────────────────────────────┘
                                 │ Process IPC (stdin / stdout)
                                 ▼
               [ Supervised Child Process (Script/Binary) ]
```

### How It Works

1. **$N \times$ `HarnessEngine` Execution**:
   - Starts and supervises target scripts/binaries as child subprocesses.
   - Manages POSIX signal propagation, exit codes, and native privilege dropping (`user`, `group`, `umask`).
2. **Stream Interposition & Mutation**:
   - Sits between the child's `stdout`/`stderr` and the external output.
   - Any registered coprocessor can dynamically mutate, mask, alias, or suppress lines in real time before they reach the outside world.
3. **In-Band Annotation Protocol (IPC)**:
   - Supervised applications communicate with the harness simply by printing structured comments to stdout (e.g. `print('# @harness.clock:init id=main interval=1.0')`).
   - The harness strips these control directives so they never leak downstream, decodes their intentions, and routes them to the appropriate coprocessor.
4. **Coprocessor Dispatch**:
   - The `CoprocessorRouter` directs each annotation to its domain coprocessor (`TimerCoprocessor`, `DlpCoprocessor`, etc.).
   - Coprocessors can mutate streams, maintain internal state machines, or write synthetic events back into the child's `stdin`.
5. **Specialized "Golden Path" Workloads**:
   - For standalone operational tasks (such as Data Loss Prevention / sanitization), `fd-harness` provides dedicated commands that **pre-inject instructions at startup**.
   - The target application does not need to emit any annotations; the harness wraps it and applies the sanitization rules transparently.

---

## The Three Operating Modes

### 1. In-Band Protocol Mode (`fd-harness run`)

Supervises a process that actively controls the harness via structured `# @harness` annotations in its logs.

```bash
# Supervise a target script with directive tracing
fd-harness run -v ./worker.py

# Drop privileges to nobody:nogroup with restricted umask
fd-harness run -u nobody:nogroup --umask 027 ./worker.py
```

**How an application controls the harness from code (zero SDK):**
```python
import sys, time

# 1. Initialize a virtual timer coprocessor
print("# @harness.clock:init id=tick interval=1.0 cycles=5", flush=True)

# 2. Dynamically register a live stream masking rule
print('# @harness.filter:mask pattern="API_KEY_[0-9A-Z]+" action="hash" template="key_{hash:6}"', flush=True)

# 3. Regular application output (will be sanitized in flight)
print("Connected with secret API_KEY_9948AB12C", flush=True)
# Outside world sees: Connected with secret key_f4a1c0
```

---

### 2. Multi-Engine Topology (`fd-harness coord`)

Coordinates multiple independent `HarnessEngine` instances defined declaratively in a single `harness.toml` file.

```bash
fd-harness coord ./path/to/workspace/
```

**Example `harness.toml`:**
```toml
[coordinator]
name = "data-mesh"

[[engines]]
name = "ingest-worker"
command = "python3 ingest.py"
user = "nobody"
group = "nogroup"
umask = "027"

[[engines]]
name = "processor"
command = "./processor-bin"
show_directives = true
log_target = "processor.log"
log_format = "jsonl"
```

---

### 3. Golden Path: Streaming DLP & Pseudonymization (`fd-harness dlp`)

A specialized domain workflow where the harness pre-injects masking and pseudonymization instructions directly into the engine, requiring zero in-band directives or code changes from the supervised command.

#### A. Process Wrapper Mode (Supervision & Privilege Dropping)
```bash
fd-harness dlp redact \
  -r dlp-rules.toml \
  -V vault.jsonl \
  -u nobody:nogroup \
  --umask 027 \
  -- ./server.sh --port 8080
```

#### B. Unix Pipeline Mode (Streaming `stdin` $\to$ `stdout`)
```bash
zstdcat production-logs.zst | fd-harness dlp redact -r dlp-rules.toml -V vault.jsonl > sanitized.log
```

#### C. Deterministic Reverse Unmasking
Reconstruct original values from pseudonymized outputs using the non-redundant BiMap vault:
```bash
fd-harness dlp unmask -V vault.jsonl sanitized.log > original.log
```

---

## Core Engine Components

| Component | Role | Description |
| :--- | :--- | :--- |
| **`HarnessEngine`** | Process Supervisor | Launches child processes, drops OS privileges (`user`/`group`/`umask`), forwards POSIX signals, and manages the non-blocking I/O loop. |
| **`AnnotationCodec`** | In-Band Protocol | Parses `# @harness.<domain>:<action> key=val` strings from standard output into strongly typed `Instruction` objects. |
| **`CoprocessorRouter`** | Intent Dispatcher | Binds and dispatches instructions to their domain coprocessor and maintains the active chain of stream mutation filters. |
| **`HeapScheduler`** | Discrete Event Core | High-performance heap-based priority queue for sub-millisecond virtual clock scheduling and timer events. |
| **`TimerCoprocessor`** | Time & Clocks | Manages periodic timers, tick counts, and writes time events back into the child's `stdin`. |
| **`DlpCoprocessor`** | Stream Sanitizer | Executes multi-action pattern mutations (`mask`, `hash`, `alias`) and records mapping events. |
| **`BiMapVault`** | Reversible Pseudonyms | In-memory 1:1 forward/reverse map backed by an append-only JSONL Write-Ahead Log (`vault.jsonl`). |

---

## DLP Transformation Actions & WAL Schema

### Supported Mutation Strategies

- **`mask`** : Destructive static redaction (e.g. `[REDACTED]`, `Bearer [AUTH_TOKEN]`).
- **`hash`** : Truncated HMAC-SHA256 with secret salt (e.g. `tok_{hash:8}` $\to$ `tok_3f8a1b2c`).
- **`alias`** : Stateful 1:1 bijective sequence pseudonym (e.g. `client_{seq.cust:03d}` $\to$ `client_001`). Reversible via `fd-harness dlp unmask`.

### Rules Specification (`dlp-rules.toml`)

```toml
[dlp]
fail_on_leak = false
summary = true
vault_file = "vault.jsonl"
salt = "cluster-secret-salt"

[[rules]]
name = "customer-id"
pattern = 'CUST-\d{4}'
action = "alias"
template = "client_{seq.cust:03d}"

[[rules]]
name = "internal-ip"
pattern = '10\.\d{1,3}\.\d{1,3}\.\d{1,3}'
action = "alias"
template = "internal_ip_{seq.ip:02d}"

[[rules]]
name = "auth-token"
pattern = 'Bearer\s+[A-Za-z0-9_\-\.]{20,}'
action = "mask"
template = "Bearer [REDACTED_TOKEN]"
```

### Event-Sourced Vault WAL (`vault.jsonl`)

The BiMap vault is stored on disk as a non-redundant, append-only JSONL stream. Reverse mappings ($B \to A$) are dynamically reconstructed in RAM on startup:

```jsonl
{"type": "vault_settings", "properties": {"version": 1, "salt": "cluster-secret-salt", "counters": {"cust": 2, "ip": 2}}}
{"type": "mapping_item", "properties": {"raw": "CUST-1042", "alias": "client_001", "rule_id": "customer-id", "created": 1789604316.348}}
{"type": "mapping_item", "properties": {"raw": "10.0.0.42", "alias": "internal_ip_01", "rule_id": "internal-ip", "created": 1789604316.350}}
```

---

## CLI Command Hierarchy

```
fd-harness
├── run          # Supervise command with in-band directive parsing & privilege drop
├── coord        # Multi-engine topology coordinator (reads harness.toml)
├── dlp          # Specialized Data Loss Prevention suite
│   ├── redact   # Real-time stream sanitization (wrapper or stdin pipe)
│   └── unmask   # Reversible unmasking via BiMap vault
└── version      # Display version
```

### Key CLI Flags

- **Privilege Dropping (Run-As)** : `-u, --user <user[:group]>`, `-g, --group <group>`, `--umask <octal>`.
- **DLP Sanitization** : `-r, --rules <file>`, `-m, --mask <pattern=replacement>`, `-V, --vault <file>`, `--salt <str>`, `--fail-on-leak`.
- **Telemetry & Tracing** : `-v, --show-directives`, `--log-level <lvl>`, `-t, --log-target <:stdout|:stderr|path>`, `-f, --log-format <text|jsonl>`, `--strace <path>`.

---

## Verification & Tests

The test suite runs deterministically with a virtual clock in **under 60 milliseconds** using Python's built-in `unittest`:

```bash
python3 -m unittest discover -s tests -p "test_*.py"
```

To run the interactive DLP demo:
```bash
cd samples/11-redact
./run-demo.sh
```

---

## Design Principles

1. **Zero External Dependencies**: Pure Python standard library core. No external wheels required in production.
2. **Deterministic & Fast**: Sub-millisecond virtual clock scheduling; sub-process startup overhead under 10ms.
3. **Transparent Composition**: Adheres strictly to standard Unix streams and POSIX conventions.
