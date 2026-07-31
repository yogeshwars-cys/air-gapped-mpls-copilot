# Phase 4 — Offline LLM Copilot & Incident Knowledge Base

**Goal:** Give the NOC operator plain-English answers to three questions for every fault — *what fails, why the risk is elevated, and what to do* — with zero cloud dependency. All inference runs on the local machine using an Ollama-served Mistral 7B model and a ChromaDB vector store.

**Status:** ✅ Complete — Ollama + Mistral 7B, ChromaDB RAG over ACP logs and runbooks, NLQ interface (interactive and single-shot), graceful structured fallback when Ollama is offline.

---

## File Structure

```
phase4-llm/
├── ikb_manager.py        # ChromaDB CRUD: seed runbooks, ingest ACP logs, query
├── llm_copilot.py        # AetherCopilot class: ACP explain + NLQ, RAG pipeline
├── nlq_interface.py      # Interactive NLQ terminal loop (intent detection, clarification)
├── setup_llm.sh          # One-time setup: Ollama install, Mistral pull, dep install, IKB seed
├── chroma_db/            # ChromaDB persistent vector store (auto-created on --seed)
└── runbooks/
    ├── topology.md             # 7-node MPLS topology reference (nodes, interfaces, VRFs)
    ├── mpls_bgp_flap.md        # BGP/OSPF flap diagnosis and recovery steps
    ├── packet_loss_corruption.md  # Packet loss and frame corruption runbook
    └── congestion_saturation.md   # Rate limiting and queue saturation runbook
```

---

## Architecture

```
                         ┌─────────────────────────────────────┐
                         │         AetherCopilot.explain(acp)  │
                         └────────────────┬────────────────────┘
                                          │
                         ┌────────────────▼────────────────────┐
                         │  1. Build RAG query from ACP fields  │
                         │     (fault_class + action + rationale)│
                         └────────────────┬────────────────────┘
                                          │
             ┌────────────────────────────▼────────────────────────────┐
             │                    ikb_manager.query_all()               │
             │  ┌──────────────────┐        ┌───────────────────────┐  │
             │  │ collection:      │        │ collection:           │  │
             │  │  runbooks        │        │  incidents            │  │
             │  │ (4 markdown docs)│        │ (ACP JSONL log)       │  │
             │  │ 384-dim vectors  │        │ 384-dim vectors       │  │
             │  └──────┬───────────┘        └────────────┬──────────┘  │
             │         └──────────────┬──────────────────┘             │
             │              top-k merged by cosine distance            │
             └────────────────────────┬────────────────────────────────┘
                                      │
                         ┌────────────▼──────────────────┐
                         │  2. Build structured prompt    │
                         │     ACP fields + RAG context   │
                         └────────────┬──────────────────┘
                                      │
              ┌───────────────────────▼──────────────────────────────┐
              │                Ollama (port 11434)                    │
              │  mistral:7b-instruct-q4_K_M  (4.1 GB GGUF, local)   │
              │  temperature=0.2, top_p=0.9, ctx=4096               │
              └───────────────────────┬──────────────────────────────┘
                                      │ or structured fallback if offline
                         ┌────────────▼──────────────────┐
                         │  3. Parse Q1 / Q2 / Q3 from   │
                         │     LLM free-form output       │
                         └────────────┬──────────────────┘
                                      │
                         ┌────────────▼──────────────────┐
                         │  AetherCopilotReport dict      │
                         │  {q1_what_fails, q2_why_risk,  │
                         │   q3_action, source, acp_id}   │
                         └───────────────────────────────┘
```

---

## Components

### `ikb_manager.py` — Incident Knowledge Base

Manages two ChromaDB collections using `all-MiniLM-L6-v2` (sentence-transformers) embeddings. Everything runs 100% offline after the initial model download.

| Collection | Source | Content |
|---|---|---|
| `runbooks` | `runbooks/*.md` | Markdown split by `##` headings into chunks |
| `incidents` | `ikb/incidents.jsonl` | ACP log entries — each ACP becomes one searchable document |

**Key functions:**

```python
seed_runbooks()             # Parse runbooks/*.md → chunk by heading → upsert into ChromaDB
ingest_acps()               # Sync ikb/incidents.jsonl → incidents collection (idempotent)
query(question, top_k, collection)   # Vector search in one collection
query_all(question, top_k)  # Search both collections, merge and re-rank by distance
format_context(results)     # Format search results into an LLM-ready context string
```

**CLI:**

```bash
python3 phase4-llm/ikb_manager.py --seed          # load runbooks → ChromaDB
python3 phase4-llm/ikb_manager.py --ingest-acps   # sync incidents.jsonl → ChromaDB
python3 phase4-llm/ikb_manager.py --query "BGP flap on pe1" --top-k 3
```

---

### `llm_copilot.py` — AetherCopilot

The main copilot class. Accepts an ACP object and returns structured Q1/Q2/Q3 answers. Also handles free-form NLQ.

**AetherCopilot API:**

```python
copilot = AetherCopilot(auto_seed=True)

# Per-ACP structured report
report = copilot.explain(acp)
# → {q1_what_fails, q2_why_risk, q3_action, source, acp_id, fault_class, severity}

# Free-form natural language query
answer = copilot.query("How do I recover from a BGP flap on pe1?")
# → str (LLM answer or structured fallback)
```

**Prompt template fields fed to Mistral:**

| Field | Source |
|---|---|
| `fault_class` | `acp.ml_analysis.predicted_fault_class` |
| `confidence` | `acp.ml_analysis.confidence_score` |
| `severity` | `acp.severity` |
| `ttf` | `acp.ml_analysis.estimated_time_to_failure_sec` |
| `execution_mode` | `acp.corroboration.execution_mode` |
| `engines_agree` | `acp.corroboration.engines_agree` |
| `top_features` | `acp.top_features` (top-5 attention heatmap features) |
| `twin_divergence` | `acp.digital_twin_divergence` |
| `context` | Top-3 IKB results (runbooks + incidents) |

**Offline fallback:** If Ollama is unreachable, `_fallback_answers()` produces rule-based answers directly from the ACP fields — no LLM required. The `source` field in the report will be `"structured_fallback"` instead of `"ollama"`.

---

### `nlq_interface.py` — Natural Language Query Terminal

Interactive operator terminal. Detects fault-class intent and node names from the query text to pre-filter IKB retrieval before passing to the LLM.

**Conversation flow:**

```
IDLE → CLARIFY → CONFIRM → (SIMULATE | REPORT) → IDLE
```

**Intent detection maps:**

| Keyword | Resolved fault class |
|---|---|
| bgp, ospf, flap, control | Control-Plane Flap |
| latency, delay, slow | Latency Spike |
| loss, drop | Packet Loss |
| corrupt, crc | Frame Corruption |
| congestion, throttle, bandwidth | Rate Limiting / Congestion |

**Usage:**

```bash
python3 phase4-llm/nlq_interface.py              # interactive loop
python3 phase4-llm/nlq_interface.py --once "what is wrong with pe1?"
```

---

### Runbooks

Pre-written operational playbooks that form the static half of the RAG knowledge base. Chunked by `##` heading so vector search hits the relevant procedure rather than the whole document.

| File | Contents |
|---|---|
| `topology.md` | Node roles, interface names, VRF assignments, link capacities, service types |
| `mpls_bgp_flap.md` | BGP/OSPF neighbor loss — diagnosis commands, recovery steps, BGP reconvergence timers |
| `packet_loss_corruption.md` | `tc qdisc show` + `ethtool` diagnostics, interface error counters, `ip link` recovery |
| `congestion_saturation.md` | `tc -s qdisc`, queue saturation thresholds, QoS shaping commands for VoIP preservation |

---

## Setup

Run **once** before going air-gapped (requires internet for Ollama + model download):

```bash
bash phase4-llm/setup_llm.sh
```

This script:
1. Installs Ollama (system daemon via official install script)
2. Pulls `mistral:7b-instruct-q4_K_M` (~4.1 GB, 4-bit quantized GGUF)
3. Installs Python deps (`chromadb`, `sentence-transformers`, `fastapi`, etc.)
4. Seeds ChromaDB with all 4 runbooks (calls `ikb_manager.py --seed`)

After setup, **everything runs 100% offline** — no egress, no external APIs.

---

## Air-Gap Guarantees

- ChromaDB stores all vectors on-disk under `phase4-llm/chroma_db/` — no cloud sync.
- `all-MiniLM-L6-v2` is downloaded once by sentence-transformers and cached in `~/.cache/torch/sentence_transformers/`. No re-download needed.
- Ollama serves Mistral 7B on `http://127.0.0.1:11434` — loopback only, no external routing.
- `AetherCopilot` detects Ollama availability via a 3-second timeout probe. If offline, the fallback path requires zero network calls.

---

## Integration with Phase 3 and Phase 5

- **Phase 3 → Phase 4:** `AnomalyContextPacket` (from `acp_manager.py`) is passed directly to `AetherCopilot.explain()`. The IKB is also auto-updated every time a new ACP is written (`ikb/incidents.jsonl`).
- **Phase 4 → Phase 5:** `app.py` imports `AetherCopilot` and exposes it via `POST /api/nlq` and the NLQ chatbox in the dashboard. The copilot is initialized lazily at dashboard startup.
