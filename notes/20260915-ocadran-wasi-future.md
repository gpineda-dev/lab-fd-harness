# Architecture Roadmap: Ocadran Evolution — Go Engine, Wazero Runtime, WASI Coprocessors & OCI Registries
**Date:** 2026-09-15  
**Topic:** Transition from Python Prototype (`fd-harness`) to Industrial Binary (`ocadran` / Ouvrage-Cadran), Wazero WASI Sandboxing, Capability Security, and Community OCI Ecosystem  
**Format:** Direct engineering notes (English, concise, non-fluff)

---

## 1. The Strategic Arc: From Prototype to Industrial Standard

The project lifecycle is split into two complementary phases:
1. **The Laboratory Demonstrator (`fd-harness` / Python):**
   * Purpose: Academic proof-of-concept, `strace` verification, blog post series, developer ergonomics exploration.
   * Target audience: Systems engineers, hackers, researchers (ETH Zürich, INRIA, Univ. Tübingen).
2. **The Industrial Standard (`ocadran` / Go + Wazero):**
   * Purpose: Zero-dependency standalone static binary (~15 MB, instant boot ~2 ms), cross-compiled for `amd64`, `arm64`, `riscv64`, and Windows.
   * Target audience: Production environments, CI/CD runners, enterprise infrastructures, WASI/Bytecode Alliance community.

---

## 2. Core Engine Migration: Go + Wazero (Zero-CGO Pure Wasm)

* **Why Go Core:**
  * Native concurrency model: Goroutines and typed channels mirror the internal Harness multi-channel event bus and POSIX/Windows IPC pipes without thread overhead.
  * Single static binary distribution (`CGO_ENABLED=0`) eliminating shared library and Python version drift in production.
* **Why Wazero as WebAssembly Engine:**
  * **Pure Go (Zero CGO):** Compiles cleanly for all platforms without C compiler dependencies or dynamic linking issues.
  * **WASI Snapshot-1 & Preview-2 Compliance:** Executes standard `wasm32-wasi` guest binaries at near-native JIT/AOT speeds.
  * **Sub-Millisecond Cold Starts:** Instantiating a WASI coprocessor module takes < 1 ms (compared to 500 ms – 2 s for Docker containers).

---

## 3. WASI Coprocessors: Polyglot Micro-Engines

* **Definition:**
  * Rather than hardcoding domain-specific logic into the core engine, modular operations are implemented as standalone WASI guest modules.
  * Polyglot compilation targets: Rust (`cargo build --target wasm32-wasi`), Zig, Go (`GOARCH=wasm GOOS=wasip1`), C/C++.
* **Catalogue of Modular Coprocessors:**
  * `coproc-dlp`: Stream tokenization, named-entity parsing, and Vault mapping.
  * `coproc-crypto`: In-flight hashing (BLAKE3, SHA-256), signature verification, and payload encryption.
  * `coproc-grok`: Structured pattern matching and regex record extraction.
  * `coproc-fsm`: Complex workflow and protocol state machines (OAuth2, TCP emulation).
  * `coproc-onnx`: Embedded lightweight ML inference (anomaly detection on logs).

---

## 4. Capability-Based Security Model

Unlike traditional shell scripts or subshells executing with full host permissions (`rm -rf /` hazards):
* **Default Deny Sandbox:** A WASI coprocessor has **zero host access** by default (no file system, no network sockets, no environment variables, no clock access).
* **Explicit Capability Grants:** The `ocadran` host grants granular, scoped capabilities via directives or CLI configuration:
  * Filesystem: Scoped read-only access limited to a designated directory (`--mount dir=/tmp/logs:ro`).
  * Network: Restricted egress limited to specified endpoints (`--allow-net=api.internal.corp:443`).
  * Memory: Hard memory ceiling per coprocessor instance (e.g. `max_memory=16MB`).
* **Supply Chain Hardening:** Third-party community coprocessors cannot exfiltrate SSH keys or tamper with system binaries.

---

## 5. Community Distribution: OCI Registries (`ocadran pull/push`)

Leverages standard OCI (Open Container Initiative) Artifact specifications (compatible with Docker Hub, GitHub Container Registry `ghcr.io`, AWS ECR, and Harbor):

* **Developer Workflow (Authoring & Publishing):**
  ```bash
  # Compile from Rust to WASI
  cargo build --target wasm32-wasi --release

  # Package, tag, and push OCI artifact
  ocadran tag ./target/wasm32-wasi/release/dlp_engine.wasm ghcr.io/org/dlp-engine:v1.0
  ocadran push ghcr.io/org/dlp-engine:v1.0
  ```
* **Consumer Workflow (Pull & Execute in Shell):**
  ```bash
  # Pull verified coprocessor to local cache (~200 KB download)
  ocadran pull ghcr.io/org/dlp-engine:v1.0

  # Execute inside standard shell pipeline with zero local package installs
  cat raw.log | ocadran run --with ghcr.io/org/dlp-engine:v1.0 ./process.sh
  ```
* **Decoupled Cross-Platform Guarantees:**
  * Scripts no longer depend on local package managers (`apt`, `brew`, `apk`) or GNU vs. BSD toolchain differences.
  * The identical WASI module executes deterministically across macOS ARM, Linux x86, Alpine containers, and Windows.

---

## 6. Synthesis: The Quadruple Academic & Industry Alignment

`ocadran` represents the convergence point of four major computer science communities:

1. **ETH Zürich (Systems & Multikernel):**
   * Barrelfish philosophy realized in user-space: OS services as asynchronous message-passing coprocessors rather than monolithic LibC state.
2. **INRIA (Privacy & Stream Governance):**
   * Practical real-time streaming *Differential Privacy*, K-anonymity, and invariant preservation with reversible vaults.
3. **Univ. Tübingen / DuckDB Labs (In-Memory Database Systems):**
   * Repurposing columnar analytical engines into a real-time reactive state plane for OS streams and shell pipes.
4. **Bytecode Alliance (WASI & WebAssembly Foundation):**
   * Extending WASI beyond the browser and serverless into the foundation of terminal computing, system scripting, and container-free micro-utilities.
