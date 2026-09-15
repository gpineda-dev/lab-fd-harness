# Engineering Notes: In-Band Stream Control, DataStructures, State & Game Primitives
**Date:** 2026-09-14 / 2026-09-15  
**Topic:** DLP/Anonymization, Stateful BiMap Vaults, Coprocessor DataStructures, Grid/Raycast Engine, Multi-Console Attach  
**Format:** Direct architecture notes (English, non-fluff)

---

## 1. Stream DLP & Reversible Mapping (BiMap Vault)

* **Classification of Redaction Operations:**
  * `mask`: Destructive replacement (`[REDACTED]`, `***`). Breaks cross-line correlation.
  * `hash`: Salted cryptographic one-way projection (`HMAC(val, salt)[:8]`). Format-breaking.
  * `alias` / `pseudonymize`: Stateful format-preserving bijection ($A \leftrightarrow B$). Retains session correlation. Fully reversible with state file.
  * `perturb` / `generalize`: Numeric jitter ($val \pm \Delta$), bucketing (timestamps, subnets `192.168.1.0/24`), invariant preservation (e.g. enforcing $bytes_{out} > bytes_{in}$).

* **Dual Generation Strategies (Collision-Free Guarantee):**
  * **Deterministic / By-Construction Unique (Zero collisions):**
    * Sequential IDs (`usr_001`, `usr_002`).
    * Sequential CIDR allocation (`10.99.0.1`, `10.99.0.2` on base `10.99.0.0/16`).
  * **Pseudo-Random / Semantic with Rejection Sampling:**
    * Generators: `names` (Docker-style `clever_curie`), RFC-safe domains (`@example.com`), Luhn-valid test credit cards.
    * Collision handling: Check `reverse_map` in $O(1)$. Re-sample up to `MAX_ATTEMPTS` (e.g. 20).
    * Deterministic Fallback: Append monotonic counter suffix if space saturates (`clever_curie_21`).

* **Stateful BiMap Architecture:**
  * `forward_map: Dict[original, alias]` ($O(1)$ lookup for stream replace).
  * `reverse_map: Dict[alias, original]` ($O(1)$ anti-collision validation + post-mortem unmasking).
  * **Persistence (Vault):**
    * Loaded on startup (`mode=read` or `mode=rw`).
    * Appends new entities discovered during streaming.
    * Atomic flush to disk on exit or `# @harness.dlp:flush`.
    * Command inversion: `harness dlp unmask --vault vault.json < clean.log > restored.log`.

---

## 2. In-Memory DataStructures Coprocessor (`# @harness.ds`)

* **Core Problem in POSIX Shell:** Bash offers only 1D indexed and associative arrays. Operations like `shift`/`pop` require $O(N)$ reallocations. No native heap, priority queue, or LRU.
* **Primitive Catalog:**
  * `heapq` (Binary Min/Max Heap): $O(\log N)$ push/pop for event loops, deterministic task scheduling, and prioritized alert routing.
  * `deque` (Double-Ended Queue): $O(1)$ push/pop at head and tail. Ring-buffer semantics. Essential for sliding windows, worker queues, and game loops (Snake body).
  * `lru` (Least Recently Used Cache): Doubly-linked list + hash map. $O(1)$ lookups, updates, and automatic tail eviction on capacity limit. Configurable TTL.
  * `bimap`: Native bidirectional map backing the DLP/Vault subsystem.
  * `set`: Set operations (membership, union, intersection, difference) in $O(1)$.

---

## 3. Blob Store & Reference Handles (Pass-by-Reference)

* **Anti-Pattern:** Passing large data payloads (JSON, XML, binary dumps) directly over IPC pipes saturates POSIX pipe buffers (default 64 KB limit on Linux) and inflates Bash environment memory.
* **Handle Architecture (Decoupled Data vs Control Plane):**
  * **Blob Store (Data Plane):** High-capacity RAM / `/dev/shm` storing raw payloads.
  * **DataStructures (Control Plane):** Queues, heaps, and caches store **only lightweight 8-byte handles** (`h_9f4c` or Content-Addressed `BLAKE3(payload)[:12]`).
  * **Content-Addressed Storage (CAS):** Identical payloads automatically deduplicate and reuse the existing handle.
* **Lifecycle & Garbage Collection:**
  * Deterministic Reference Counting (`ref_count`).
  * Incremented on store/enqueue; decremented on eviction/pop.
  * Instant RAM release when `ref_count == 0` (zero-GC overhead).
* **Zero-Copy Kernel Integration:**
  * Primitive `# @harness.blob:as_fd ref="h_9f4c"` exposes an anonymous read-only file descriptor (`memfd_create`).
  * External binaries (`jq`, `grep`, `tar`) read directly from memory via `/proc/self/fd/X` with zero disk I/O and zero subshell buffering.

---

## 4. Multi-Dimensional Structs & Schema Engine (`# @harness.struct`)

* **Typed Object Store:**
  * Schema definition: `# @harness.struct:def name=Node schema="{ host: str, ip: ipv4, port: int, active: bool }"`
  * Instantiation: Returns typed opaque handle (e.g. `node#1`).
  * Getters/Setters via IPC: Path traversal (`regions[0].nodes[2].ip`) executes in $O(1)$ in coprocessor RAM.
* **Elimination of `jq` Fork Bombs:** Replaces nested `$(echo | jq)` loops (forking thousands of processes) with sub-microsecond in-memory path evaluations.
* **DuckDB Bridge:** Homogeneous struct collections project zero-copy virtual tables into in-memory DuckDB for vectorized SQL queries across active objects.

---

## 5. Grid Engine, Raycasting & Spatial Primitives (`# @harness.grid`)

* **Agnostic Game / Spatial Primitives (No hardcoded game rules):**
  * `grid:alloc name=board cols=W rows=H default=0`: Typed 2D memory array.
  * `grid:raycast from_x, from_y, dx, dy, stop_on`: Vector projection along arbitrary directions.
    * Connect Four: Gravity fall (`dx=0, dy=+1, stop_on="!=0"`).
    * Chess / Checkers: Line-of-sight checks along ranks, files, diagonals.
  * `grid:find_sequence len=N axes=[h, v, d]`: Generic 1D/2D convolution/scanner.
    * Matches $N$ identical consecutive non-zero elements.
    * Covers Connect Four ($N=4$), Tic-Tac-Toe ($N=3$), Gomoku ($N=5$).
  * `grid:view grid=board mappings={...} click_target="col|cell"`: Double-buffered ANSI renderer with mouse coordinate mapping.

---

## 6. Multi-Console Collaboration & Distributed Attach (`harness attach`)

* **Architecture:**
  * Single central Harness Engine session holding shared RAM state (Grid, DataStructures, DuckDB).
  * Single arbiter/coordinator script (e.g. Bash) processing incoming events from IPC.
* **Multi-Seat Capabilities:**
  * Multiple independent TTYs / remote SSH sessions attach to the same session (`harness attach --session ID`).
  * **Role-Based Views:**
    * Connect Four / Chess: Player 1 (Red view/input), Player 2 (Yellow view/input), Spectators (Read-only broadcast).
    * Mission Control / Ops: Database SRE panel, Network Engineer panel, Main Wall status board.
* **Synchronized State Broadcast:**
  * Actions triggered on Console A mutate central state.
  * Harness broadcasts delta ANSI frames to all attached Viewports simultaneously with zero terminal flicker.

---

## 7. Typed DataStructures & Struct Semantics (`List<User>` vs `List<ptr User>`)

* **Architectural Dilemma: By-Value (Contiguous) vs By-Reference (Pointers):**
  * **`List<User>` (By-Value / Flat Layout / C/Rust `Vec<T>`):**
    * Contiguous RAM block, cache-friendly (L1/L2 prefetching), zero pointer overhead.
    * Instant zero-copy bridge to Apache Arrow and DuckDB columnar scans.
    * Constraint: Fixed-size structs, mutation requires copying or index tracking, no shared identity across collections.
  * **`List<ptr User>` (By-Reference / Java & Python Object Heap):**
    * Stores 8-byte handles pointing to independent heap entities.
    * Shared Identity (Aliasing): The same `User` instance exists concurrently in `List<User>`, `Dict<email, User>`, and `Queue<User>`. Modifying `user.active` updates all views instantly.
    * Handles variable-size fields (nested lists, dynamic strings).
    * Cost: Indirection / pointer chasing.

* **Target Design for Harness:**
  * **User API (Front):** High-level declaration syntax: `# @harness.ds:create type=list name=users of=User`.
  * **Engine Implementation (Back):** Pointer-based (`ptr User`) with deterministic Reference Counting.
  * **Coprocessor Features Unlocked:**
    * Strong typing at insertion boundary (instant `TypeError` on schema mismatch).
    * Bulk projections without Bash loops: `# @harness.ds:pluck name=users prop="id"`.
    * In-memory native filtering: `# @harness.ds:filter name=users where="active == true"`.
    * On-demand flattening: Materialize a flat contiguous Arrow/DuckDB view when vectorized analytics or SQL execution is requested.

