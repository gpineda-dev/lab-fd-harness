# Engineering Notes: Stream DLP, Multi-Action Policies & Reversible BiMap Vaults
**Date:** 2026-09-16 / 2026-09-17  
**Topic:** Data Loss Prevention (DLP), In-Flight Sanitization, BiMap Vaults, Template Formatting, Entity Pseudonymization  
**Format:** Direct architecture notes (English, non-fluff, mathematical models & systems design)

---

## 1. Context & The Paradigm Gap (1979 vs 2024)

### The 1979 Unix Ceiling
* Standard Unix stream processing tools (`sed`, `awk`, `tr`, `cut`) operate strictly **stateless and destructively**:
  $$f(x) = \text{constant}$$
* **The Failure Mode:** When sanitizing relational logs, masking `john.doe@company.com` into `[REDACTED]` destroys session correlation across lines. If `user_1` appears at line 10 and again at line 1,000, `sed` cannot preserve relational identity without leaking the original secret.

### The 2024 "Enterprise" Bloat
* **Heavyweight SaaS (Google Cloud DLP, HashiCorp Vault Transform):**
  * Network REST hop per batch/line, latency penalty ($>10-50\text{ms}$), costly egress pricing (\$3/GB), format-preserving encryption (FPE / AES-FF3-1) computational overhead.
* **ML / NER Pipelines (Microsoft Presidio, SpaCy):**
  * 2 GB models loaded in RAM, non-deterministic inference, heavy CPU usage ($>50\text{ms}$ per line), unusable in real-time Unix pipes (`stdout | dlp | ...`).

### The Frugal Unix Primitive (`fd-harness dlp` / `redact`)
* A sub-millisecond, zero-dependency streaming coprocessor providing in-flight regex interception, format-preserving template substitution, and stateful bijective mapping ($A \leftrightarrow B$) with offline reversible vault persistence.

---

## 2. Taxonomy of Sanitization Policies

| Policy | Mathematical Model | Statefulness | Format Preserved? | Relational Integrity? | Reversibility |
|---|---|---|---|---|---|
| **`mask`** | $f(x) = C$ or $T(\text{groups})$ | Stateless | Optional | No | Irreversible ($0$) |
| **`hash`** | $f(x) = \text{HMAC}(x, \text{salt})[:N]$ | Stateless (Deterministic) | Partial | Yes (Deterministic) | Irreversible ($0$) |
| **`alias`** | $f(x) = \text{BiMap}[x]$ | Stateful ($O(U)$ RAM) | Yes (Template) | Yes (1:1 Bijection) | Reversible ($1:1$) |
| **`perturb`** | $f(x) = x \pm \Delta$ or $\text{CIDR}(x, /24)$ | Stateless / Local | Yes | Approximate | Irreversible ($0$) |

### 1. Action: `mask` (Destructive Redaction)
* **Goal:** Permanent elimination of sensitive tokens from logs, traces, or terminal outputs.
* **Fast Path:** Constant string replacement (`[REDACTED]`, `***`) using compiled regex fast-path substitution.
* **Dynamic Template Path:** Syntax-preserving redaction retaining semantic metadata without leaking secrets (e.g. `(?P<tenant>[a-z]+)_[0-9]+` $\to$ `[TENANT:{tenant}:REDACTED]`).

### 2. Action: `hash` (Cryptographic One-Way Projection)
* **Goal:** Format-breaking or prefix-preserving deterministic projection for cross-stream correlation without reversible storage.
* **Implementation:** HMAC-SHA256 with user-supplied or session-generated salt:
  $$\text{digest} = \text{HMAC}_{\text{SHA256}}(token, salt)$$
* **Template Syntax:** `{hash}` (full 64-char hex) or `{hash:N}` (truncated to $N$ chars, e.g. `sk_live_{hash:8}`).
* **Properties:**
  * Same token $\to$ identical hash across processes sharing the same salt.
  * Zero memory growth ($O(1)$ RAM).

### 3. Action: `alias` (Stateful Multi-Dimensional Pseudonymization)
* **Goal:** High-fidelity simulation and debugging. Sensitive tokens are mapped to human-readable, format-compliant synthetic tokens while preserving 1:1 bijectivity across the entire lifecycle.
* **Execution Flow:**
  1. On token capture $T$, query `BiMapVault.forward[T]`.
  2. **Cache Hit:** Return existing alias $A$ (zero counter mutation).
  3. **Cache Miss:** Render template (incrementing namespaced sequence counter `{seq.<ns>}`), store $T \mapsto A$ in `forward_map` and $A \mapsto T$ in `reverse_map`, and return $A$.

---

## 3. Format-String Template Engine

Instead of rigid schema flags (`prefix`, `pad_zeros`, `suffix`), rules define expressive Python-style format strings evaluated in the context of the match:

```
{variable[:format_spec]}
```

### Supported Template Variables

| Placeholder | Resolution Mechanism | Example Output |
|---|---|---|
| `{raw}` | Original matched token string | `ghp_1234567890` |
| `{hash}` | Full HMAC-SHA256 hex digest | `e3b0c44298fc1c14...` |
| `{hash:N}` | Salted HMAC truncated to $N$ characters | `e3b0c442` (for `N=8`) |
| `{seq}` | Monotonic global counter | `1`, `2`, `3` |
| `{seq:03d}` | Formatted global counter with zero-padding | `001`, `002`, `003` |
| `{seq.<ns>:fmt}` | Namespaced monotonic counter (e.g. `seq.cust`, `seq.ip`) | `042` |
| `{<name>}` | Named regex capture group `(?P<name>...)` | `alice` |
| `{g1}`, `{g2}` | Positional regex capture groups | `10`, `0` |

### Concrete Examples

```toml
# IP Masking: Preserves subnet class, anonymizes host
[[rules]]
name = "internal-ip"
pattern = '10\.0\.(?P<subnet>\d+)\.(?P<host>\d+)'
action = "alias"
template = "10.0.{subnet}.{seq.ip:02d}"

# Customer ID Pseudonymization: Clean synthetic identifier
[[rules]]
name = "cust-id"
pattern = 'CUST-\d{6}'
action = "alias"
template = "client_{seq.cust:04d}"

# Stripe / Secret Key Hashing: Prefix preserved, entropy hashed
[[rules]]
name = "stripe-secret"
pattern = 'sk_live_[0-9a-zA-Z]{24}'
action = "hash"
template = "sk_live_{hash:10}"
```

---

## 4. BiMap Vault Architecture & Reversible Unmasking

```
  ============================= STREAMING (SANITISATION) =============================

    Raw Log Stream       +-----------------------+       Sanitized Stream
  ---------------------> | DlpCoprocessor (Pipe) | -------------------------> [Public/S3]
  "Connect CUST-1001"    +-----------------------+    "Connect client_001"
                                     |
                                     v
                        +-------------------------+
                        |       BiMapVault        |
                        | forward: CUST-1001->c_1 |
                        | reverse: (RAM inferred) |
                        +-------------------------+
                                     |
                                     v (Append-Only JSONL WAL)
                              [ vault.jsonl ]


  ============================= UNMASKING (RESTORATION) =============================

    Sanitized Stream     +---------------------------+       Restored Original Stream
  ---------------------> |   fd-harness dlp unmask   | -------------------------> [Secured Enclave]
  "Connect client_001"   +---------------------------+    "Connect CUST-1001"
                                     ^
                                     |
                              [ vault.jsonl ]
```

### Collision-Free Unmasking Algorithm
When unmasking a sanitized stream via `vault.unmask_line(line)`:
* **The Substring Hazard:** If alias `client_1` and alias `client_10` exist simultaneously, a naive regex or string replace can greedily replace `client_1` inside `client_10`, corrupting the output (`CUST-10010` instead of `CUST-1002`).
* **The Solution:** The reverse lookup engine dynamically sorts all active aliases by length in descending order before compiling the single-pass substitution pattern:
  $$\text{Pattern} = \bigvee_{k \in \text{sorted}(\text{keys}, \text{len descending})} \text{regex\_escape}(k)$$
* **Time Complexity:** Single pass $O(L)$ per line, where $L$ is line length.

### Standardized Event-Stream WAL Schema (`vault.jsonl`)
Zero disk redundancy: `reverse` mapping is fully reconstructed in RAM on load. Every record uses a standardized `{type, properties}` event envelope:

```jsonl
{"type": "vault_settings", "properties": {"version": 1, "salt": "demo-vault-salt-42", "counters": {"cust": 1, "ip": 1}}}
{"type": "mapping_item", "properties": {"raw": "CUST-1042", "alias": "client_001", "rule_id": "customer-id", "created": 1726532402.123}}
{"type": "mapping_item", "properties": {"raw": "10.0.0.42", "alias": "internal_ip_01", "rule_id": "internal-ip", "created": 1726532402.124}}
```

---

## 5. In-Band Protocol & Multi-Channel IPC

The DLP coprocessor interacts with the child process or stream through two interfaces:

### 1. Static Configuration (TOML)
```bash
fd-harness dlp redact --rules dlp.toml --vault vault.jsonl < app.log > clean.log
```

### 2. In-Band Directives (`# @harness.filter:mask`)
A running worker script can dynamically spawn and modify redaction rules at runtime without restarting the supervisor:

```bash
#!/usr/bin/env bash
# Dynamically register an alias rule for all following output
echo '# @harness.filter:mask pattern="user_[0-9]+" action="alias" template="u_{seq:02d}" name="usr"'

# All stdout below is automatically intercepted and sanitized on FD 1
echo "Processing transaction for user_42"
# Output received on terminal / file: "Processing transaction for u_01"
```

---

## 6. Future Expansion: Multi-Attribute Pivot Context (Phase II)

### The Sparse Correlation Dilemma
In distributed systems logs, identity attributes are often split across separate lines:
* Line 10: `User authenticated: id=98432 (session=sess_abc)`
* Line 500: `Network packet from 192.168.1.50 for session=sess_abc`

### Proposed Phase II Blueprint
Introduce **Entity Context Registers** (`entity = "user"`, `primary_key = true`):
1. Matching the primary key allocates/retrieves an `EntityRecord` in vault memory.
2. The active entity context (`user.id = 98432`) is attached to secondary identifiers (`session_token`, `ip_address`).
3. Downstream rules can reference contextual attributes: `{user.seq}`, ensuring that all telemetry belonging to the same entity shares identical relational synthetic keys across disjoint lines.

