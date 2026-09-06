# 2026-09-06: Extending the Harness — Part 1: Interactive Terminal UX & FD 4 Isolation

## 1. The Interactive Dilemma in Shell Scripts

Building rich, interactive CLI tools or REPLs (Read-Eval-Print Loops) in Bash has historically been a painful compromise:

1. **The Broken Terminal UX**:
   A standard Bash `read line` does not provide modern terminal ergonomics:
   * Pressing arrow keys emits raw ANSI escape codes (`^[[A`, `^[[B`, `^[[C`, `^[[D`) instead of navigating history or moving the cursor.
   * Editing in the middle of a command requires backspacing the entire tail of the line.
   * Command history requires either heavy external dependencies or invasive GNU Readline wrappers.

2. **The Control vs. User Input Collision**:
   In a supervised process architecture, the problem becomes catastrophic:
   * If the supervisor uses the child's `stdin` (FD 0) to deliver coprocessor responses (`tick ...`, `calc ...`, `wakeup`), and the child script also attempts to read human keystrokes from `stdin`, a race condition occurs.
   * Keyboard input and coprocessor protocol messages collide and corrupt the stream.

---

## 2. The Case Study: `samples/09-calculator-app.sh`

[`samples/09-calculator-app.sh`](../samples/09-calculator-app.sh) demonstrates an interactive scientific calculator backed by the Pratt arithmetic coprocessor:

```bash
uv run fd-harness run samples/09-calculator-app.sh
```

```text
============================================================
       PRATT COPROCESSOR CALCULATOR (Powered by fd-harness) 
============================================================
 Features & Expressions:
   <expr>            Evaluate (e.g. 5 * 6, 1s / 60, sqrt(64))
   <expr> => <var>   Store into variable (e.g. 8 + 6 => var1)
   Math Functions    sin, cos, tan, sqrt, exp, log, abs, round
   Math Constants    pi (3.14159...), e (2.71828...), tau

 Slash Commands:
   /list, /vars      List registered variables in coprocessor memory
   /mem              Display accumulator (0)
   /clear            Reset accumulator to 0
   /sprint <text>    Interpolate with [bracketed expressions]
   /help             Display this menu
   /exit, /quit      Exit calculator
============================================================
calc [mem=0]> 78 * 9 => k
Assigned: k=702
calc [mem=702]> k % 10 => l
Assigned: l=2
calc [mem=2]> /list
Registered variables (2):
  k = 702
  l = 2
```

From the perspective of the operator, the experience feels identical to Python's IPython or Node's REPL:
* **Up / Down arrows**: Cycle backward and forward through command history (recalling `78 * 9 => k`).
* **Left / Right arrows**: Move the cursor within the current expression to fix typos without erasing characters.
* **Home / End (`Ctrl-A` / `Ctrl-E`)**: Jump directly to the beginning or end of the line.
* **Backspace / Suppr (Delete)**: Delete characters under or before the cursor with proper terminal redraw.

Yet, **the child process is a 100-line Bash script without GNU Readline**!

---

## 3. Architecture: How the Harness Proxies Human Input

To achieve this, `fd-harness` introduces a split-plane architecture:

```text
+-------------------------------------------------------------------------------+
|                             HOST TERMINAL (TTY)                               |
|                                                                               |
|   Operator Keystrokes (Raw bytes: \x1b[A, \x1b[C, a, b, \n)                   |
|            |                                                                  |
|            v                                                                  |
|   +-----------------------------------------------------------------------+   |
|   |             TerminalLineEditor (Supervisor Host-Side)                 |   |
|   |                                                                       |   |
|   |  * cbreak / non-canonical mode (~ICANON, ~ECHO)                       |   |
|   |  * Escape sequence state machine                                      |   |
|   |  * Navigation:                                                        |   |
|   |      - Up / Down: history recall (buffer swap)                        |   |
|   |      - Left / Right: cursor index tracking (self.cursor)              |   |
|   |      - In-place insertion & deletion                                  |   |
|   |  * Visual redraw: \r\033[K{prompt}{line}\033[{col}G                   |   |
|   +-----------------------------------+-----------------------------------+   |
|                                       |                                       |
|                                       | [Clean submitted line on Enter:       |
|                                       |  os.write(out_fd, line + "\n")]       |
|                                       |                                       |
|   +-----------------------------------|-----------------------------------+   |
|   |                      FD 4 (User Input Pipe)                           |   |
|   |                                   |                                   |   |
|   |                                   v                                   |   |
|   |             SUPERVISED PROCESS (Bash Script / App)                    |   |
|   |                                                                       |   |
|   |  harness_user_read line  -->  read -u 4 -r line                       |   |
|   |                                                                       |   |
|   |  FD 0 (stdin)  <-- Unpolluted: reserved for coprocessor responses     |   |
|   |  FD 1 (stdout) --> Child prompt ("calc [mem=0]> ") & directives       |   |
|   |  FD 4 (user)   <-- Clean, human-entered string                        |   |
|   +-----------------------------------------------------------------------+   |
+-------------------------------------------------------------------------------+
```

### A. Terminal Cbreak Mode & Visual Redraw
When `fd-harness` launches an interactive application, `TerminalLineEditor` configures the host terminal:
- It disables canonical line-buffering (`~termios.ICANON`) and terminal echo (`~termios.ECHO`).
- Keystrokes are delivered byte-by-byte into the editor's reactor loop.
- Screen redrawing is performed using standard VT100 ANSI sequences:
  - `\r`: Carriage return to column 0.
  - `\033[K`: Clear from cursor to end of line.
  - `\033[{col}G`: Move cursor to absolute horizontal column position.

### B. Dedicated FD 4 Isolation
Instead of sending the completed line back onto FD 0, the supervisor injects it into **FD 4**:
- During process startup (`engine.py`), the supervisor creates an `os.pipe()` and duplicates the read end onto child file descriptor 4 (`os.dup2(user_r, 4)`).
- In the Bash script, reading user input is as simple as:
  ```bash
  # In lib_harness.sh:
  harness_user_read() {
      local var="$1"
      read -u 4 -r "$var"
  }
  ```
- **Total Protocol Isolation**: FD 0 receives strictly structured protocol events (`tick ...`, `calc ...`). FD 4 receives strictly sanitized human lines. Neither stream can ever interfere with the other.

---

## 4. Part 2: Non-Interactive DLP & Zero-Touch Stream Filtering

While interactive applications like `samples/09-calculator-app.sh` take advantage of dedicated file descriptor routing (FD 4), real-world operational environments frequently require the exact opposite: **purely non-interactive, zero-touch filtering**.

In production pipelines, thousands of legacy scripts, database migration runners, and CI/CD tools already exist. Many cannot be modified to insert `# @harness` directives, nor do they require bidirectional coprocessing. They simply emit logs to `stdout`—and occasionally leak sensitive credentials (API tokens, database URIs, JWTs).

To solve this, `fd-harness` extends its coprocessor architecture to function as a **standard Unix pipe filter on `stdin`**:

```text
+-------------------------------------------------------------------------------+
|                       ZERO-TOUCH UNIX STREAM PIPELINE                         |
|                                                                               |
|   Legacy Source / Command (cat, curl, ./deploy.sh, docker logs)               |
|                                |                                              |
|                                v  (Raw text stream with possible leaks)       |
|   ===================== STANDARD UNIX PIPE ================================   |
|                                |                                              |
|                                v  (sys.stdin)                                 |
|   +-----------------------------------------------------------------------+   |
|   |                  fd-harness redact (Pipe Filter)                      |   |
|   |                                                                       |   |
|   |  * Line-by-line streaming without buffering large files in RAM        |   |
|   |  * DlpCoprocessor regex inspection                                    |   |
|   |  * Replaces matching secrets with masks ([REDACTED], [SHIELDED])      |   |
|   |  * Tracks leak counts and rule hits for auditability                  |   |
|   +--------------------+----------------------------------+---------------+   |
|                        |                                  |                   |
|  [Sanitized stdout]    |                                  | [Audit Stderr]    |
|                        v                                  v                   |
|               OPERATOR TERMINAL / CI LOGS         AUDIT LOG REPORT            |
|               (Clean sanitized stream)            (Summary & leak stats)      |
+-------------------------------------------------------------------------------+
```

### The Three Modes of DLP Operation

As demonstrated in [`samples/11-redact/run-demo.sh`](../samples/11-redact/run-demo.sh), `fd-harness redact` can be adopted with zero friction across multiple operational patterns:

#### 1. Standard Unix Pipe (Headless / Cron / CI)
The most direct use-case: attaching to existing output via standard pipe without modifying the source command:
```bash
cat demo-server-logs.txt | uv run fd-harness redact --rules dlp-rules.toml
```

#### 2. Process Wrapping Mode
Executing and supervising a legacy binary directly, capturing and scrubbing both its `stdout` and `stderr`:
```bash
uv run fd-harness redact --rules dlp-rules.toml -- ./deploy.sh --stage production
```

#### 3. Ad-hoc Command-Line Masking
Quick one-liner filters without requiring an external configuration file:
```bash
gpineda@thinkpad-e15g2:~/Documents/gpineda-dev/labs/fd-harness$ echo "Internal key is SECRET-998811-XYZ" | uv run fd-harness redact -m 'SECRET-[0-9]+-[A-Z]+=[SHIELDED]' --no-summary
Internal key is [SHIELDED]
```

---

## 5. Architectural Benefits of Non-Interactive DLP

1. **Zero Source-Code Dependencies**:
   The producer process does not source `lib_harness.sh`, does not configure file descriptors, and does not need to know `fd-harness` exists. It remains completely unaware that its output is being filtered.

2. **Zero In-Memory Buffering**:
   Processing occurs strictly line-by-line as bytes arrive on `stdin`. Gigabyte-sized log dumps can stream through the DLP pipeline with constant $O(1)$ memory usage.

3. **Active Security Gate (`--fail-on-leak`)**:
   In CI/CD environments, redacting leaks on the screen is not always enough; you often want to fail the build if an unauthorized token was exposed. With `--fail-on-leak`, `fd-harness` automatically terminates with exit code `1` if any secret was intercepted.

4. **Two Complementary Consumption Paradigms**:
   With these extensions, `fd-harness` provides two distinct ways to interact with coprocessors:
   * **Active / Annotated**: Scripts proactively query coprocessors via `# @harness` and receive responses via `read -r` (e.g. timers, calculations, dynamic filter registration).
   * **Passive / Zero-Touch**: Legacy streams pass through the harness via `stdin` or CLI wrapping without a single annotation.

---

## 6. Part 3: Multi-Engine Coordination & The In-Memory Event Bus

Real-world operational architectures rarely consist of a single script running in isolation. Production environments typically require multiple concurrent processes running alongside each other:
* A **producer** polling external hardware or microservices.
* A **consumer** aggregating metrics or updating databases.
* A **watchdog** or healthcheck loop running periodic probes.

Traditionally, coordinating multiple shell processes is brittle:
- Backgrounding with `&` leaves orphan processes if the parent crashes.
- Inter-process communication requires temporary FIFO pipes (`mkfifo`), scratch files on disk, or heavyweight network brokers (Redis, RabbitMQ, Kafka).
- Process lifecycles, signal handling (`SIGINT`), and exit codes are difficult to synchronize cleanly.

To solve this, `fd-harness` provides **`HarnessCoordinator`**: a multi-process orchestration runtime that executes and synchronizes $N$ autonomous engines under a single kernel reactor.

---

## 7. The Multi-Engine Architecture

As demonstrated in [`samples/10-coordinator/`](../samples/10-coordinator/), the coordinator manages multiple processes declaratively:

```text
+-------------------------------------------------------------------------------+
|                      fd-harness COORDINATOR REACTOR                           |
|                                                                               |
|  +-------------------------------------------------------------------------+  |
|  |             Single-Threaded Multiplexing Reactor Loop                   |  |
|  |                                                                         |  |
|  |  * Single select.select() over all alive engine file descriptors        |  |
|  |  * Global min_timeout calculated across all independent schedulers      |  |
|  |  * Zero threads, zero asyncio, zero lock contention                     |  |
|  +------------------------------------+------------------------------------+  |
|                                       |                                       |
|                                       v                                       |
|  +-------------------------------------------------------------------------+  |
|  |                    IN-MEMORY EVENT BUS (Zero Broker)                    |  |
|  |                                                                         |  |
|  |  * Topic matching (e.g. 'telemetry:metric', wildcard '*')               |  |
|  |  * Instant routing across engine memory boundaries                      |  |
|  +---------------------+-----------------------------------+---------------+  |
|                        |                                   |                  |
|          [Emit Event]  |                                   | [Dispatch to FD 3|
|                        |                                   |  of Subscribers] |
|                        |                                   v                  |
|                        |                  +--------------------------------+  |
|                        |                  |       ENGINE 2: CONSUMER       |  |
|                        |                  |        (./consumer.sh)         |  |
|                        |                  |                                |  |
|                        |                  |  FD 3: read -u 3 -r ev top pl  |  |
|                        |                  +--------------------------------+  |
|                        v                                                      |
|       +--------------------------------+                                      |
|       |       ENGINE 1: PRODUCER       |                                      |
|       |        (./producer.sh)         |                                      |
|       |                                |                                      |
|       |  # @harness.clock:wait id=prod |                                      |
|       |  # @harness.bus:emit topic=... |                                      |
|       +--------------------------------+                                      |
+-------------------------------------------------------------------------------+
```

### A. Declarative Manifest: `harness.toml`
Instead of complex bash launch scripts, the deployment topology is declared in a clean TOML file:

```toml
[coordinator]
show_directives = false

[[engines]]
name = "consumer"
command = ["./consumer.sh"]
attach_stdin = false

[[engines]]
name = "producer"
command = ["./producer.sh"]
attach_stdin = false
```

Execution is a single command:
```bash
uv run fd-harness coord samples/10-coordinator
```

### B. In-Memory Event Bus (Zero External Broker)
Engines communicate asynchronously across process boundaries without writing to disk or connecting to network ports:
1. The **Consumer** subscribes to a topic:
   ```bash
   echo "# @harness.bus:subscribe topic='telemetry:metric'"
   ```
2. The **Producer** runs on its own sovereign clock (100ms interval) and pulses events:
   ```bash
   echo "# @harness.bus:emit topic='telemetry:metric' payload='sensor=temp_cpu value=41.5'"
   ```
3. The coordinator routes the event directly onto the Consumer's dedicated **FD 3** event stream:
   ```bash
   read -u 3 -r tag topic payload
   # tag="bus:event" topic="telemetry:metric" payload="sensor=temp_cpu value=41.5"
   ```

### C. Single-Threaded Reactor & Lifecycle Safety
The coordinator eliminates common multi-process pitfalls through disciplined systems engineering:
* **Deterministic Synchronous Multiplexing**: A single `select.select` call handles all file descriptors and all timer deadlines simultaneously.
* **Coordinated Teardown & Signal Trapping**: If the user hits `Ctrl-C`, the coordinator traps `SIGINT` and sends clean termination signals to all children, preventing zombie processes.
* **Exit Code Aggregation**: The coordinator runs until all child engines terminate, drains all remaining output pipes, and returns the exit status of each child engine.

---

## 8. Key Takeaways

1. **Terminal Ergonomics & Protocol Isolation (Part 1)**:
   By dedicating **FD 4** to proxied human input and keeping **FD 0** for synchronous control responses, interactive CLI apps gain rich line editing and history without stream collision.
2. **Universal Stream Filtering (Part 2)**:
   By exposing the DLP coprocessor as a non-interactive Unix filter on **`stdin`**, brownfield pipelines gain real-time secret sanitization with zero code changes.
3. **Multi-Engine Microkernel Coordination (Part 3)**:
   By combining a single-threaded synchronous reactor with an in-memory event bus over **FD 3**, multiple autonomous shell scripts can cooperate with nanosecond precision without external message brokers.
