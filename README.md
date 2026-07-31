# Project Aether — Air-Gapped Predictive Copilot for Secure MPLS Operations

> Edge-Assisted Predictive Resilience and Heuristic Mitigation Framework for Air-Gapped Networks

**Hackathon:** Bharatiya Antariksh Hackathon 2026 (ISRO × Hack2skill)  
**Problem Statement 13:** Air-Gapped Predictive Copilot for Secure MPLS Operations

An autonomous, offline AI NOC Copilot that predicts network failures before SLA breach, mathematically validates those predictions, selects optimal mitigation from first principles, and explains everything in plain English — with **zero cloud dependency**.

---

## Team
- Charan Teja
- Yogeshwar
- Aranya Roy
- Pradyumna

---

## The Core Insight

Two independently computed predictions agreeing is stronger evidence than either alone:

| Layer | Type | What it answers |
|---|---|---|
| LSTM (stochastic) | ML | "Does this telemetry pattern resemble past failures?" |
| NetworkX graph (deterministic) | Analytical | "Will projected traffic mathematically saturate this queue?" |

**Both agree → fast action (confidence-gated auto-execute).**  
**Disagree → safety trip, operator review — no autonomous action.**

A stochastic model never directly controls infrastructure. Only the deterministic Policy Engine executes changes, and only above the operator-configured confidence threshold.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                           PROJECT AETHER                                │
│                                                                         │
│  Phase 1: Simulation          Phase 2: Telemetry                        │
│  ┌──────────────────┐         ┌─────────────────┐                       │
│  │ Containerlab     │──exec──▶│ exporter.py     │──scrape──▶ Prometheus │
│  │ 7-Node MPLS L3VPN│         │ (threaded, 8000)│           + Grafana   │
│  │ pe1,p1,pe2       │         └────────┬────────┘                       │
│  │ ce-branch1/2     │                  │ dataset.csv                    │
│  │ ce-hub, ce-dc    │◀──tc netem──┐    ▼                                │
│  └──────────────────┘            │  data_collector.py                  │
│                                  │  (labels rows with fault_type)       │
│  fault_injector.py ──────────────┘                                      │
│  scenario_runner.sh                                                     │
│                                                                         │
│  Phase 3: Predictive Engine                                             │
│  ┌───────────────────────────────────────────────────────────────┐      │
│  │  taxonomy.py (single source of truth — 6 classes, policy)    │      │
│  │                                                               │      │
│  │  LSTM Autoencoder   ──▶ anomaly score                        │      │
│  │  LSTM Classifier    ──▶ fault class + confidence             │      │
│  │  TTF Regressor      ──▶ seconds to SLA breach                │      │
│  │         │                        │                            │      │
│  │         ▼                        ▼                            │      │
│  │  acp_manager.py         graph_model.py                       │      │
│  │  (Anomaly Context       (Clonal State-Space Search)          │      │
│  │   Packet — JSON)        BASELINE / REROUTED / QOS_THROTTLED  │      │
│  │         │                        │                            │      │
│  │         └──────┬─────────────────┘                            │      │
│  │                ▼                                               │      │
│  │    aether_corroborator.py                                     │      │
│  │    AGREE  → Autonomy Policy → AUTO_EXECUTE                    │      │
│  │    DISAGREE → SAFETY TRIP  → RECOMMEND_ONLY                   │      │
│  └───────────────────────────────────────────────────────────────┘      │
│                                                                         │
│  Phase 4: Offline LLM                     Phase 5: NOC UI                │
│  ┌──────────────────────┐                ┌───────────────────────┐      │
│  │ Ollama + Mistral 7B  │──Q1/Q2/Q3──▶  │ FastAPI app.py        │      │
│  │ ChromaDB RAG (IKB)   │                │ WebSocket alerts      │      │
│  │ ikb_manager.py       │                │ NLQ chatbox           │      │
│  │ llm_copilot.py       │                │ Topology SVG          │      │
│  │ nlq_interface.py     │                │ Autonomy dial         │      │
│  └──────────────────────┘                └───────────────────────┘      │
│                                                                         │
│  Cross-cutting (v4):                                                    │
│  EMA threshold │ Syslog parser │ Digital Twin │ Attention heatmap       │
│  Ed25519 signing │ Air-gap compliance │ Benchmark harness │ Feedback CLI│                                               │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## Phase Status

| Phase | Description | Status |
|---|---|---|
| **1** | 7-node MPLS L3VPN (Containerlab + FRR) — CE/PE/P, OSPF/LDP/VPNv4 | ✅ Complete |
| **1** | Traffic generation (VoIP, DB, HTTP, SSH across L3VPN) | ✅ Complete |
| **1** | Fault injection — 5 types (latency, loss, corrupt, rate, flap) via tc-netem | ✅ Complete |
| **1** | SD-WAN overlay — real GRE tunnel PE1↔PE2 over the OSPF core (`overlay-setup.sh`) | ✅ Complete |
| **1** | Baseline QoS — HTB priority/interactive/bulk classes + DSCP on PE→CE egress (`qos-setup.sh`) | ✅ Complete |
| **1** | Scenario runner (all 4 validation scenarios, timed) | ✅ Complete |
| **2** | Custom threaded Prometheus exporter — interface + FRR + RTT/jitter metrics | ✅ Complete |
| **2** | RTT and jitter measurement via ICMP ping (per-link, 10-packet bursts) | ✅ Complete |
| **2** | NetFlow/IPFIX synthetic flow simulator (`netflow_simulator.py`, port 9995) | ✅ Complete |
| **2** | Application traffic generator — iperf3 CE-to-CE VPN flows + synthetic fallback | ✅ Complete |
| **2** | FRR syslog parser — native BGP/OSPF + syslog ADJCHANGE format, both supported | ✅ Complete |
| **2** | Prometheus + Grafana via Docker Compose | ✅ Complete |
| **2** | Labeled dataset collection (fault_injector → dataset.csv) | ✅ Complete |
| **3** | LSTM Autoencoder (unsupervised anomaly) | ✅ Complete |
| **3** | LSTM Attention Classifier (5-class fault) | ✅ Complete |
| **3** | TTF Regressor — `time_to_breach` lead-time target (sustained-breach label + precursor sampling); real ~20–43s live lead times | ✅ Complete |
| **3** | NetworkX Clonal State-Space Search + tunnel health API | ✅ Complete |
| **3** | Dual-model corroboration gate + autonomy policy matrix | ✅ Complete |
| **3** | Anomaly Context Packet (ACP) JSON schema | ✅ Complete |
| **3** | Fault taxonomy single source of truth (taxonomy.py) | ✅ Complete |
| **3** | EMA self-calibrating threshold (Bollinger-band, replaces fixed 3×) | ✅ Complete |
| **3** | Attention heatmap linear head (per-feature explainability) | ✅ Complete |
| **3** | Digital Twin (Holt-Winters forecast vs actual graph divergence) — fixed API wiring | ✅ Complete |
| **3** | Ed25519 model signing + air-gap compliance reporter | ✅ Complete |
| **3** | Per-service SLA YAML + graph scoring (voip / database / bulk) | ✅ Complete |
| **3** | Lead-time benchmark harness (5/5 detected, avg 503s before breach) | ✅ Complete |
| **3** | Operator feedback CLI (accept/reject → IKB false-positive rate) | ✅ Complete |
| **3** | Natural fault timing state machine (QUIET→FAULT→RESOLVE) | ✅ Complete |
| **4** | Offline LLM copilot (Ollama + Mistral 7B, graceful offline fallback) | ✅ Complete |
| **4** | ChromaDB RAG over ACP logs + topology runbooks (ikb_manager.py) | ✅ Complete |
| **4** | NLQ interface (intent detection, IKB retrieval, fallback answers) | ✅ Complete |
| **4** | Runbooks: BGP flap, congestion, packet loss, topology reference | ✅ Complete |
| **5** | FastAPI NOC dashboard (app.py) — Q1/Q2/Q3 overview, alerts, NLQ, compliance | ✅ Complete |
| **5** | WebSocket live alert stream + 4s polling dual-path fallback | ✅ Complete |
| **5** | Left-sidebar SPA — 7 views: Overview, Alerts, Ask Aether, History, Policy Matrix, Remediation Log, Validation | ✅ Complete |
| **5** | Q1/Q2/Q3 overview — the three NOC questions surfaced live from the latest ACP | ✅ Complete |
| **5** | Live topology SVG — real exporter telemetry when available, synthetic fallback (source-labelled) | ✅ Complete |
| **5** | Multi-turn NLQ — server-side session store, context-resolving follow-ups | ✅ Complete |
| **5** | Time-Travel Topology Playback — slider scrubs ACP snapshot history | ✅ Complete |
| **5** | Autonomy Matrix editor — live-editable policy table, locked rows for critical actions | ✅ Complete |
| **5** | Remediation Log — real `docker exec` command output for every action taken | ✅ Complete |
| **5** | Validation view — Phase 6 scenarios with lead time / MTTD + run button | ✅ Complete |
| **5** | `/api/tunnel-health`, `/api/netflow`, `/api/scenarios`, `/api/action-log` | ✅ Complete |
| **6** | Scenario validation suite (`run_scenarios.py`) — all 4 PS-13 scenarios automated | ✅ Complete |
| **6** | Scenario 1: gradual link degradation → benchmark lead-time | ✅ Complete |
| **6** | Scenario 2: BGP route flap → MTTD measurement | ✅ Complete |
| **6** | Scenario 3: telemetry collector failure → graceful degradation | ✅ Complete |
| **6** | Scenario 4: controller misconfiguration → policy drift detection + restore | ✅ Complete |

---

## Measured results & honest scope

| Metric | Result |
|---|---|
| Fault-class accuracy (held-out windows) | ~82% |
| Live prediction lead time | ~20–43s before SLA breach |
| Benchmark lead time (5 gradual-degradation scenarios) | 5/5 detected, **avg 503s**, up to 834s before breach |
| Phase 6 scenario validation | **4/4 passing** (S1 lead time 524s) |
| Air-gap compliance | signed probe report; Ed25519-signed models; 100% local inference |

**Honest scope (read before evaluating):**
- The demo runs on **synthetic data** — a temporally-realistic generated dataset and simulated
  NetFlow/traffic. The real Containerlab data plane is optional (`./run.sh --clab`).
- Remediation is currently **open-loop**: Aether predicts and acts, but does not yet verify the
  action worked or auto-roll-back when the fault clears (the highest-leverage next build).
- Full status vs. the problem statement is in [`GAP_ANALYSIS.md`](GAP_ANALYSIS.md); the
  forward-looking backlog of gaps and ideas is in [`GAPS_AND_IDEAS.md`](GAPS_AND_IDEAS.md).

---

## The three operational questions

The problem statement asks the NOC to answer three questions in real time. Every alert in
Aether is rendered against them, and the dashboard **Overview** surfaces them directly:

| | Question | How Aether answers |
|---|---|---|
| **Q1** | What is likely to fail next — and when? | fault class + confidence + **time-to-breach lead time** from the TTF regressor |
| **Q2** | Why is risk elevated — which signals contributed? | top attention features (decoded per node/metric) + corroboration rationale |
| **Q3** | What corrective action before SLA/security impact? | policy-gated action (`AUTO_EXECUTE` vs `RECOMMEND_ONLY`) + the exact FRR/`tc` commands |

---

## Dashboard Features (v5.0)

The NOC dashboard (`http://localhost:8080`) is a single-page application with a collapsible left sidebar — **7 views**:

| Panel | Description |
|---|---|
| **Overview** | The Q1/Q2/Q3 situation panels (above) over the live MPLS topology SVG. Animated red dashed links for active faults. Live air-gap compliance panel (signed probe results) + telemetry-source indicator (real exporter vs synthetic). Recent-alerts mini-feed. |
| **Alerts** | Full scrollable history of every ACP: fault class, severity, confidence, **TTF lead time**, execution mode, rationale. Click any alert for the full Q1/Q2/Q3 incident report + remediation commands. |
| **Ask Aether** | **Multi-turn** natural-language chat with Mistral 7B (offline). RAG retrieves runbook + incident context from ChromaDB before generating; follow-ups keep conversation context ("how do I fix it?"). Graceful offline RAG fallback if Ollama is down. |
| **History** | Time-Travel slider scrubs backward through every ACP snapshot; re-renders the topology at that historical state with the event details. |
| **Policy Matrix** | Live editor for the operator autonomy policy (see below). |
| **Remediation Log** | Every action Aether took — auto-executed or operator-approved — with the **real command stdout/stderr** captured from `docker exec`. Honest about failures (e.g. "No such container" when the lab isn't running). |
| **Validation** | The four Phase 6 scenarios with PASS/FAIL, measured **prediction lead time / MTTD**, and a "Run validation suite" button that launches `run_scenarios.py` and polls for completion. |

### Autonomy Policy Matrix

The matrix controls when the Edge Policy Engine may act autonomously vs. surface a recommendation:

| Action Class | Default Min Confidence | Default Mode | Editable |
|---|---|---|---|
| `REROUTE_BRANCH` | 80% | AUTO_EXECUTE | ✅ |
| `QOS_SHAPE_QUEUE` | 75% | AUTO_EXECUTE | ✅ |
| `CORE_PATH_FAILOVER` | 90% | RECOMMEND_ONLY | 🔒 locked |
| `NODE_ISOLATION` | 99% | RECOMMEND_ONLY | 🔒 locked |
| `NO_ACTION` | 100% | — | 🔒 locked |

Safety floors enforced in code regardless of operator settings: model disagreement always downgrades to `RECOMMEND_ONLY`; hub/DC-scope actions are never auto-executed.

### Time-Travel Topology Playback

Every ACP that arrives (via WebSocket or poll) is captured as a topology snapshot in the browser's in-memory history buffer. The time-travel slider indexes this buffer. Dragging left replays older states — the topology re-renders showing which links were degraded at that moment. Clicking **▶ Live** returns to the latest state.

---

## Quick Start

### One command (recommended)

```bash
pip install -r requirements.txt
bash phase4-llm/setup_llm.sh    # one-time, needs internet: Ollama + Mistral 7B + seed IKB

./run.sh                        # start everything (synthetic mode — no sudo, always works)
#  → NOC Dashboard at http://localhost:8080

./run.sh --clab                 # also deploy the real Containerlab topology + MPLS/VRF +
                                #   SD-WAN overlay + QoS + Prometheus/Grafana (no sudo needed
                                #   with docker-group access; falls back to synthetic if absent)
./run.sh --airgap               # synthetic stack inside a zero-egress network namespace —
                                #   the compliance probe genuinely reports COMPLIANT
./run.sh --clab --airgap        # BOTH: real lab on the host, copilot stack in a zero-egress
                                #   namespace. Real FRR telemetry still flows (docker exec is
                                #   a unix socket, which crosses network namespaces) and a
                                #   loopback-only bridge keeps http://localhost:8080 browsable
                                #   from the host. AIR-GAPPED + COMPLIANT with live lab data.
./run.sh stop                   # stop all Aether processes (lab stays up for fast reuse)
./run.sh destroy                # stop everything and tear down the Containerlab lab
```

`run.sh` starts: Ollama (LLM) · NetFlow simulator · traffic generator · fault streamer +
inference · NOC dashboard. Logs land in `.logs/`.

### Retraining / regenerating (optional)

```bash
# GPU PyTorch (RTX 4060). Skip the index-url for CPU-only.
pip install torch --index-url https://download.pytorch.org/whl/cu128

python3 phase3-models/generate_dataset.py --rows 100000   # → phase3-models/dataset_large.csv
python3 phase3-models/train_models.py --data phase3-models/dataset_large.csv --epochs 60
python3 phase3-models/train_models.py --regressor-only --data phase3-models/dataset_large.csv  # TTF only
python3 phase3-models/model_integrity.py --sign           # Ed25519 sign all models

# Quantify lead time and run the validation suite
python3 phase3-models/benchmark_harness.py
python3 phase6-validation/run_scenarios.py --no-containerlab
```

---

## Tech Stack

| Layer | Technology | Notes |
|---|---|---|
| Network sim | Containerlab 0.76 + FRRouting (FRR) | 7 nodes, Alpine-based |
| Telemetry | Custom Python exporter (stdlib only) | Threaded, 350+ features/node |
| Time-series DB | Prometheus 2.x + Grafana | Docker Compose, local only |
| ML models | PyTorch 2.x — LSTM Autoencoder, BiLSTM+Attention, TTF Regressor | CUDA (RTX 4060) / CPU fallback |
| Graph engine | NetworkX + Clonal State-Space Search | Millisecond-range for ≤50 nodes |
| Offline LLM | Ollama + Mistral 7B (mistral:7b-instruct-q4_K_M) | Graceful offline fallback |
| RAG / vector DB | ChromaDB (local persistent) + sentence-transformers | all-MiniLM-L6-v2, 100% offline |
| Frontend | FastAPI + inline HTML/JS | WebSocket alerts, NLQ chatbox, topology SVG |
| Trend forecasting | statsmodels Holt-Winters + linear slope fallback | Digital twin divergence signal |
| Model integrity | Ed25519 (cryptography library) | Private key offline, .pub.pem committed |
| Fault injection | `tc netem` / `tc tbf` / `ip link` via `docker exec` | No host kernel changes needed |
| Air-gap | Zero outbound deps at runtime | All images and models pre-loaded |

---

## Key Design Decisions

**Why two models instead of one?**  
A stochastic LSTM can detect *that* something is wrong; a deterministic graph model checks *whether the topology can mathematically support the predicted reroute*. Requiring agreement eliminates a whole class of false positives (ML fires on noise the graph shows is benign).

**Why taxonomy.py?**  
The original codebase defined fault class-id mappings in 5 separate files that disagreed with each other (id 4 meant "rate" in training but "Congestion" in inference, with a completely different display name). `taxonomy.py` is the single source of truth — all other files import from it.

**Why a custom Prometheus exporter instead of Telegraf?**  
Air-gap compliance. Telegraf has external plugin dependencies. The custom exporter is pure Python stdlib — zero egress, no package manager needed at runtime.

**Why `127.0.0.1` not `localhost` in the collector?**  
On Fedora / modern Linux, `localhost` resolves to `::1` (IPv6) but the exporter binds IPv4-only. urllib timeouts silently rather than refuse — fixed by using the explicit IPv4 loopback.

---

## Repository Structure

```
air-gapped-mpls-copilot/
├── README.md
├── run.sh                           # one-command launcher (synthetic | --clab | stop)
├── requirements.txt
├── GAP_ANALYSIS.md                  # problem-statement coverage audit (status)
├── GAPS_AND_IDEAS.md                # forward-looking backlog (gaps + ideas)
│
├── phase1-simulation/
│   └── topology/
│       ├── aether-lab.clab.yml      # canonical Containerlab topology (name: aether, 7 nodes)
│       ├── chunk3-setup.sh          # MPLS kernel + FRR config (OSPF/LDP/BGP/VRF), LAB= override
│       ├── overlay-setup.sh         # real GRE SD-WAN overlay PE1↔PE2 over the core
│       ├── qos-setup.sh             # baseline HTB QoS on PE→CE egress (DSCP classifiers)
│       ├── traffic_generator.sh     # VoIP + DB + HTTP + SSH flows via iperf3/hping3
│       ├── fault_injector.py        # tc netem/tbf fault injection (5 types)
│       ├── scenario_runner.sh       # Timed validation scenarios
│       └── continuous_fault_loop.sh # 30+ fault variants, 4-hour data collection
│
├── phase2-telemetry/
│   ├── exporter.py                  # Custom Prometheus exporter (interface + FRR + RTT/jitter)
│   ├── syslog_parser.py             # FRR syslog → BGP/OSPF events (native + syslog ADJCHANGE)
│   ├── netflow_simulator.py         # Synthetic NetFlow/IPFIX simulator (port 9995, /flows /summary)
│   ├── traffic_generator.py         # iperf3 CE-to-CE application traffic (VoIP/DB/bulk)
│   ├── docker-compose.yml           # Prometheus + Grafana stack
│   └── grafana-provisioning/        # Grafana datasource + dashboard JSON exports
│
├── phase3-models/
│   ├── taxonomy.py            # SINGLE SOURCE OF TRUTH — 6 fault classes + action matrix
│   ├── generate_dataset.py    # Synthetic dataset generator (100k rows, no lab needed)
│   ├── data_collector.py      # Live telemetry → labeled CSV bridge
│   ├── train_models.py        # Training script (all 3 models, CUDA, CosineAnnealingLR)
│   ├── predictive_engine.py   # Model definitions (Autoencoder, BiLSTM+Attention, Regressor)
│   ├── graph_model.py         # NetworkX clonal state-space search (SLA-aware)
│   ├── acp_manager.py         # Anomaly Context Packet schema + IKB audit log
│   ├── aether_corroborator.py # Dual-model corroboration gate + EPE + digital twin PSF
│   ├── inference_engine.py    # Live inference: EMA threshold, 3-model pipeline (digital twin wired)
│   ├── digital_twin.py        # Holt-Winters forecast → graph divergence (update+evaluate API)
│   ├── trend_forecaster.py    # EMA + Holt-Winters per-channel forecasting
│   ├── graph_model.py         # Clonal state-space search + tunnel health API
│   ├── model_integrity.py     # Ed25519 model signing + verification
│   ├── airgap_compliance.py   # Network probe + signed compliance report
│   ├── benchmark_harness.py   # Lead-time benchmark (5/5 scenarios, avg 503s before breach)
│   ├── fault_streamer.py      # State-machine fault generator (QUIET→FAULT→RESOLVE)
│   ├── feedback_cli.py        # Operator accept/reject ACP → IKB false-positive stats
│   ├── dataset_large.csv      # 100k-row synthetic training dataset (78 MB)
│   ├── saved/                 # Trained model weights + normalization params + signatures
│   ├── keys/                  # aether_model_key.pub.pem (private key stays offline)
│   ├── acp_logs/              # Live ACP JSON files (one per inference event)
│   └── ikb/incidents.jsonl    # Append-only ACP audit log
│
├── phase4-llm/
│   ├── ikb_manager.py         # ChromaDB CRUD (seed runbooks, ingest ACPs, query)
│   ├── llm_copilot.py         # AetherCopilot: RAG pipeline + Mistral 7B + fallback
│   ├── nlq_interface.py       # Interactive NLQ terminal (intent detection + clarification)
│   ├── setup_llm.sh           # One-time setup: Ollama + Mistral + deps + IKB seed
│   ├── chroma_db/             # ChromaDB persistent vector store
│   └── runbooks/
│       ├── topology.md              # Node/interface/VRF reference
│       ├── mpls_bgp_flap.md         # BGP/OSPF flap diagnosis + recovery
│       ├── packet_loss_corruption.md # Loss + corruption runbook
│       └── congestion_saturation.md  # Rate limiting + QoS runbook
│
├── phase5-dashboard/
│   └── app.py                 # FastAPI NOC dashboard v5.0 — 7-view SPA, WebSocket
│                              #   GET/PUT /api/policy      — autonomy matrix (live-edit)
│                              #   GET  /api/acps           — ACP history
│                              #   GET  /api/explain/{id}   — Q1/Q2/Q3 + remediation commands
│                              #   POST /api/nlq            — multi-turn copilot (+ /api/nlq/reset)
│                              #   GET  /api/metrics/live   — real exporter util + synthetic fallback
│                              #   GET  /api/tunnel-health  — MPLS LSP health from graph model
│                              #   GET  /api/netflow        — NetFlow summary bridge
│                              #   GET  /api/compliance     — signed air-gap report (cached)
│                              #   POST /api/execute-action — run + log a remediation
│                              #   GET  /api/action-log     — remediation audit log
│                              #   GET/POST /api/scenarios   — Phase 6 results + run trigger
│                              #   WS   /ws/alerts          — live ACP stream
│
├── phase5-integration/
│   └── README.md              # Phase 5 data-flow doc (ACP → explain → Q1/Q2/Q3)
│
├── phase6-validation/
│   └── run_scenarios.py       # PS-13 scenario validation suite (4 scenarios, --no-containerlab)
│
├── COMMANDS.md                # Full run guide + feature verification checklist
└── docs/
    ├── fault-injection.md     # All fault injection methods with commands
    ├── phase1-simulation-doc/
    ├── phase2-telemetry-doc/
    ├── phase3-models-doc/
    └── phase4-llm-doc/
```

## Where to start reading

| If you want | Read |
|---|---|
| To run the whole stack | [COMMANDS.md](COMMANDS.md) |
| The problem statement it answers | [PblmStmnt.md](PblmStmnt.md) |
| What is claimed vs. measured | "Measured results & honest scope" above |
| What is still missing | [GAP_ANALYSIS.md](GAP_ANALYSIS.md) |
| How faults are injected for demos | [docs/fault-injection.md](docs/fault-injection.md) |

## License

MIT — see [LICENSE](LICENSE).
