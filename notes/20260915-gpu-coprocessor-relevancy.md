# Architecture Notes: GPU Coprocessor Relevancy, Hardware Physics, and AI Packaging
**Date:** 2026-09-15  
**Topic:** Critical evaluation of GPU acceleration for Harness, CPU vs. GPU execution models, Ollama vs. llama.cpp, and scope delineation  
**Format:** Direct engineering notes (English, concise, non-fluff)

---

## 1. Hardware Physics: CPU vs. GPU Architectural Divergence

* **CPU Execution Model (Deep Pipelining & Branch Prediction):**
  * Few complex cores (e.g. 20 cores on Core i7-1470K).
  * Deep execution pipelines (15–20 stages), out-of-order execution, speculative execution, massive multi-megabyte L1/L2/L3 caches.
  * Optimized for **minimal latency on sequential code with dense conditional branching (`if/else`)**.
  * Ideal for: Shell orchestration, I/O multiplexing (`epoll`), state machines, and stream filters.

* **GPU Execution Model (SIMT / Massively Parallel Grid):**
  * Massive core count (e.g. 21,000+ CUDA cores on RTX 5090).
  * SIMT (*Single Instruction, Multiple Threads*): Cores execute in lockstep across 32-thread warps.
  * **Warp Divergence Penalty:** If threads within a warp take different branches, execution serializes.
  * **The "Relay Race" Synchronization Barrier:** Next execution generation begins only when the slowest thread completes:
    $$T_{\text{next}} = \max(T_{\text{warp\_threads}})$$
  * Optimized for **uniform arithmetic throughput on dense independent numerical data**, not control logic.

---

## 2. Memory Hierarchy & Latency Boundaries

```
Registers (1 cycle, ~256 KB/SM) 
  └── Shared Memory / L1 SRAM (5 cycles, ~128 KB/SM, ~15 TB/s)
        └── L2 SRAM Cache (30 cycles, ~96 MB, ~6 TB/s)
              └── VRAM GDDR7 (300 cycles, 32 GB, ~1.8 TB/s)
                    └── PCIe Gen5 Bus (3000 cycles, ~64 GB/s, Host RAM)
```

* **The PCIe Crossing Tax:**
  * Transporting data across PCIe introduces 1–10 ms of latency overhead.
  * Unless the kernel compute duration substantially exceeds the transfer cost, offloading to GPU yields negative acceleration.
* **Stream & State Locality:**
  * Stateful algorithms (e.g. Conway's Game of Life) operate on GPU via **Double-Buffer (Ping-Pong)**: Frame $T$ computes strictly from frozen Frame $T-1$ via shared SRAM/L2 cache without thread dependencies.
  * Sequential dependencies (e.g. CBC cipher chaining: $C_N = E(P_N \oplus C_{N-1})$) serialize execution, collapsing GPU utilization to a single active core.

---

## 3. Dissecting AI Packaging: Ollama vs. llama.cpp

* **Analogy Matrix:**
  $$\begin{array}{rcccl}
  \textbf{Docker} & = & \text{Packaging + HTTP Daemon} & \text{over} & \textbf{Linux Cgroups / Namespaces} \\
  \textbf{Ollama} & = & \text{Packaging + HTTP Daemon} & \text{over} & \textbf{llama.cpp (Pure C/C++ engine)} \\
  \textbf{Ocadran} & = & \text{Packaging + OCI Registry} & \text{over} & \textbf{Wazero (Pure Go WASI runtime)}
  \end{array}$$

* **Reality of Local LLMs:**
  * Models (Mistral, LLaMA) are static matrix weight files (`.gguf` format), not executable binaries.
  * `llama.cpp` is a self-contained C/C++ inference engine targeting CUDA/Metal directly. It requires no Python runtime and no HTTP daemon.
  * **Syscall-Free Execution (User-Mode Ring Buffers):**
    * Initialization involves kernel `ioctl()` syscalls via `/dev/nvidia*` for VRAM mapping.
    * Active token generation operates via **User-Space Command Queues mapped into RAM**, completely bypassing OS syscalls during execution—analogous to Linux `io_uring`.

---

## 4. Relevancy Audit for Harness: Why GPU is Largely Out-of-Scope

| Workload Domain | Hardware Sweet Spot | Verdict for Harness Core | Rationale |
| :--- | :--- | :--- | :--- |
| **I/O Routing & Proxying (`epoll`)** | CPU | **REJECT GPU** | PCIe round-trip penalty destroys sub-millisecond network latency. Batching requirements introduce unacceptable packet jitter. |
| **Vectorized Stream SQL (DuckDB)** | CPU | **REJECT GPU** | DuckDB uses CPU vectorization (AVX2/AVX-512) directly inside L3 cache. Data transfer over PCIe is 10x slower than local CPU compute. |
| **Script Orchestration & FSMs** | CPU | **REJECT GPU** | Control-flow branching causes fatal warp divergence on SIMT cores. |
| **TUI / Game Loops (Snake, Connect 4)** | CPU | **REJECT GPU** | Microsecond CPU frame diffing already delivers 60 FPS at < 0.5% CPU load. |
| **Local AI Inference (`llama.cpp`)** | GPU (VRAM) | **OPTIONAL PLUGIN** | Viable as an auxiliary `# @harness.ai` coprocessor for semantic log summarization and entity extraction. |
| **Massive Parallel Hashing / Forensic** | GPU | **OPTIONAL PLUGIN** | Offload only when bulk data volume ($> 10\text{ GB}$) justifies PCIe transfer tax (e.g. BLAKE3/SHA-256 on disk images). |

---

## 5. Architectural Takeaway

Harness remains fundamentally an **Operating System Service Coprocessor for the Shell**.
* **Core Principle:** Preserve extreme frugality, instant boot time, zero runtime dependencies, and portability across machines lacking discrete GPUs (Raspberry Pi, Cloud VMs, legacy servers).
* **Boundary:** GPU acceleration must strictly exist as an **optional, decoupled plug-in**, never as a mandatory foundation for Harness core operations.
