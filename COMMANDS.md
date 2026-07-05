# Project Aether — Operations & Verification Guide

All commands run from the repo root: `/home/charan/air-gapped-mpls-copilot/`

---

## How to start the application

Open **three separate terminals**.

### Terminal 1 — NOC Dashboard

```bash
python3 phase5-dashboard/app.py
```

Open browser: **http://localhost:8080**

Dashboard shows "● Online" in the header when Ollama is reachable, "● Offline" otherwise. Both states work — offline uses structured fallback answers.

### Optional Terminal — NetFlow simulator (port 9995)

```bash
python3 phase2-telemetry/netflow_simulator.py
```

Serves synthetic L3VPN flow records: `http://localhost:9995/flows`  
Inject flow faults: `curl 'http://localhost:9995/inject?fault=loss'`

### Optional Terminal — Application traffic generator

```bash
python3 phase2-telemetry/traffic_generator.py
```

Generates VoIP, database, and bulk traffic between CE nodes every 45s.  
Uses real `iperf3` inside containers when Containerlab is running; falls back to synthetic metrics otherwise.

### Terminal 2 — Fault streamer (natural mode)

```bash
python3 phase3-models/fault_streamer.py
```

Generates realistic fault traffic using a state machine:
- **Quiet**: Healthy heartbeat every 35–80s
- **Fault burst**: 2–5 consecutive alerts of one fault type, 10–20s apart
- **Resolve**: 1–2 Healthy events, then back to quiet

Use `--mode cycle` for the old fast-rotation mode (every 4s), `--inject flap` to fire one specific fault.

### Terminal 3 — Ollama LLM (for full Q1/Q2/Q3 answers)

```bash
ollama serve          # starts the Ollama server on port 11434
ollama pull mistral:7b-instruct-q4_K_M   # only needed once (~4GB download)
```

Without Ollama running, the dashboard uses structured fallback answers (still correct, just template-based not LLM-generated).

---

## Feature verification checklist

Each feature maps to a specific place in the UI or a terminal command. Verify each one:

---

### Phase 1 — 7-node MPLS topology

**Where to check:** Network view (first nav item, default on load)

**Verify:**
- Topology SVG shows 7 nodes: pe1, p1, pe2, ce-hub, ce-branch1, ce-branch2, ce-dc
- Links between them (pe1–p1–pe2 core, CE nodes hanging off PEs)
- Link utilization labels update every 15 seconds (green/yellow/red based on %)

```bash
# Verify container topology is running
docker ps | grep clab-aether
# Verify FRR is running inside pe1
docker exec clab-aether-pe1 vtysh -c 'show version'
```

**What to see:** Small utilization labels on each link (e.g., `12% · 120M`) updating in real time.

---

### Phase 2 — Telemetry pipeline

**Where to check:** `phase2-telemetry/` directory and `/api/status` response

**Verify:**
```bash
# Check status endpoint — shows model status, copilot availability
curl -s http://localhost:8080/api/status | python3 -m json.tool

# Run the telemetry exporter manually (polls containers, writes metrics)
python3 phase2-telemetry/exporter.py

# Test syslog parser — both native FRR format and syslog ADJCHANGE
python3 -c "
import sys; sys.path.insert(0,'phase2-telemetry')
from syslog_parser import SyslogParser
p = SyslogParser()
print(p.parse_line('2026/06/28 12:00:00 BGP: 10.0.0.2 went from Established to Idle'))
print(p.parse_line('Jun 28 12:00:02 frr bgpd: %BGP-5-ADJCHANGE: neighbor 10.0.0.1 Down'))
"

# Start the NetFlow/IPFIX simulator (port 9995)
python3 phase2-telemetry/netflow_simulator.py

# In another terminal — check flow records
curl -s http://localhost:9995/summary | python3 -m json.tool
curl -s http://localhost:9995/flows | python3 -m json.tool | head -40

# Inject a fault into flows: loss/latency/rate/flap/corrupt
curl -s 'http://localhost:9995/inject?fault=loss'
# Restore
curl -s 'http://localhost:9995/inject?fault=clear'

# Run application traffic generator (synthetic if Containerlab not running)
python3 phase2-telemetry/traffic_generator.py --once
```

---

### Phase 3 — Predictive modelling (BiLSTM + ACP + EPE)

**Where to check:** Alert Feed and alert details modal

**Verify the full ML pipeline:**
```bash
# Run inference directly — generates one ACP per fault class
python3 phase3-models/fault_streamer.py --mode cycle --once

# Check ACP files written
ls -lt phase3-models/acp_logs/ | head -10
cat phase3-models/acp_logs/$(ls -t phase3-models/acp_logs/ | head -1) | python3 -m json.tool

# Verify model integrity (Ed25519 signature check)
python3 phase3-models/model_integrity.py --verify

# Run lead-time benchmark — prints detection lead time per scenario
python3 phase3-models/benchmark_harness.py

# Check EMA threshold calibration in inference engine
python3 -c "
import sys; sys.path.insert(0,'phase3-models')
from inference_engine import AetherInferenceEngine
e = AetherInferenceEngine(); e.load_models()
print('EMA threshold:', e.ema_threshold.threshold)
"
```

**In the dashboard:**
1. Go to **Alerts** view
2. Each alert card shows: fault class, confidence %, TTF, severity badge, execution mode
3. Alerts with `RECOMMEND_ONLY` show an orange notification banner at the top of the screen
4. `AUTO_EXECUTE` alerts appear in the feed but do not prompt for approval (already executed)

**Check the ACP schema:**
The ACP JSON in `acp_logs/` should have:
- `acp_id`, `timestamp`, `severity`
- `ml_analysis.predicted_fault_class`, `ml_analysis.confidence_score`, `ml_analysis.estimated_time_to_failure_sec`
- `top_features` (list of 5 feature names from attention heatmap)
- `corroboration.recommended_action`, `corroboration.execution_mode`
- `digital_twin_divergence` (float or null)
- `service_sla_tag` (voip / database / bulk / default)

---

### Phase 3 — Digital twin divergence

**Where to check:** ACP JSON files and the alert modal

```bash
# Verify digital twin module loads
python3 -c "import sys; sys.path.insert(0,'phase3-models'); from digital_twin import DigitalTwin; print('OK')"

# Check that recent ACPs have digital_twin_divergence field
cat phase3-models/acp_logs/$(ls -t phase3-models/acp_logs/ | head -1) | python3 -c "
import sys,json; d=json.load(sys.stdin)
print('digital_twin_divergence:', d.get('digital_twin_divergence'))
"
```

---

### Phase 3 — Trend forecaster (Holt-Winters)

```bash
python3 -c "
import sys; sys.path.insert(0,'phase3-models')
from trend_forecaster import TrendForecaster
fc = TrendForecaster()
for v in [10,12,15,18,22,27]:
    fc.update('test_channel', v)
print('forecast:', fc.forecast('test_channel'))
"
```

---

### Phase 3 — Operator feedback loop (IKB)

**Where to check:** Incident modal — Approve/Reject buttons

**In the dashboard:**
1. Click any alert card in the Alerts view
2. Modal opens with fault details, Q1/Q2/Q3 analysis, and remediation commands
3. For RECOMMEND_ONLY alerts, the modal shows Approve / Reject buttons at the bottom
4. Click **Approve** → logged as `accepted` in `ikb/incidents.jsonl`, commands section highlighted
5. Click **Reject** → logged as `rejected` (feeds false-positive rate)

**Verify via CLI:**
```bash
# List all ACPs with feedback status
python3 phase3-models/feedback_cli.py --list

# View false-positive rates per fault class
python3 phase3-models/feedback_cli.py --stats
```

---

### Phase 3 — Model integrity check (Ed25519 signing)

```bash
# Verify all model weight files against their Ed25519 signatures
python3 phase3-models/model_integrity.py --verify

# Sign model files (done after training — private key is required)
python3 phase3-models/model_integrity.py --sign
```

**Where to check:** `/api/status` response includes model integrity status.

---

### Phase 3 — Air-gap compliance report

```bash
# Run compliance check (attempts outbound to 8.8.8.8, pypi.org, etc. — all must FAIL)
python3 phase3-models/airgap_compliance.py

# Save a signed compliance report
python3 phase3-models/airgap_compliance.py --out compliance_$(date +%Y%m%d).json

# Via dashboard API
curl -s http://localhost:8080/api/compliance | python3 -m json.tool
```

**Expected output:** All probes show `"reachable": false`. The report is Ed25519-signed.

---

### Phase 4 — Offline LLM (Mistral 7B + ChromaDB RAG)

**Where to check:** Ask Aether panel (third nav item)

**Verify Ollama is running:**
```bash
curl -s http://localhost:11434/api/tags | python3 -m json.tool
```

**Test NLQ via dashboard:**
1. Click **Ask Aether** in the sidebar
2. Type: `How do I fix a BGP flap on pe1?`
3. Click **Ask** (or press Enter)
4. Response should reference pe1, BGP, and remediation steps from the runbooks

**Test NLQ via API:**
```bash
curl -s -X POST http://localhost:8080/api/nlq \
  -H 'Content-Type: application/json' \
  -d '{"question":"What will fail next on the pe1-p1 link?"}' | python3 -m json.tool
```

**Verify RAG / IKB:**
```bash
python3 -c "
import sys; sys.path.insert(0,'phase3-models')
from ikb_manager import query_all, format_context, seed_runbooks
seed_runbooks()
results = query_all('BGP flap pe1', top_k=3)
print(format_context(results)[:500])
"
```

---

### Phase 5 — NOC Dashboard features

#### Live Network view
- **Verify:** Topology SVG renders all 7 nodes and links
- **Verify:** Link utilization labels (green/yellow/red %) appear and update every 15s
- **Verify:** When a fault ACP arrives, the affected links turn red
- **Verify:** Status bar at bottom says "DEGRADED: ... affected links: ..." or "All links nominal"

#### Alert Feed view
- **Verify:** Alert cards appear within seconds of fault_streamer emitting an ACP
- **Verify:** Each card shows fault class, confidence, TTF, severity badge, timestamp
- **Verify:** RECOMMEND_ONLY alerts trigger an orange "Action pending" banner at the top
- **Verify:** Clicking the banner opens the full incident modal

#### Incident modal (click any alert)
- **Verify:** Modal shows Q1 / Q2 / Q3 analysis (LLM or structured fallback)
- **Verify:** Below Q3, a "Remediation commands" section shows node-specific CLI commands
- **Verify:** Clicking any command copies it to clipboard (border flashes green)
- **Verify:** For RECOMMEND_ONLY: Approve / Reject buttons appear at the bottom
- **Verify:** Clicking Approve logs the decision and highlights the command section (does NOT auto-close)
- **Verify:** Clicking Reject closes the modal

#### Ask Aether (NLQ) view
- **Verify:** Text input accepts questions, Enter key submits
- **Verify:** Quick query buttons pre-fill the input
- **Verify:** Response appears (LLM or RAG fallback)

#### History (Time-Travel) view
- **Verify:** Topology snapshot list shows past ACPs
- **Verify:** Clicking a snapshot renders the topology state at that moment
- **Verify:** Fault class and timestamp shown below the topology

#### Policy Matrix view
- **Verify:** Table shows all 5 action classes with min_conf and auto_execute values
- **Verify:** REROUTE_BRANCH and QOS_SHAPE_QUEUE rows have editable inputs
- **Verify:** CORE_PATH_FAILOVER and NODE_ISOLATION are locked (greyed out, cannot be set to auto)
- **Verify:** Changing a value and clicking Save updates the policy (persisted to `policy_overrides.json`)

#### MPLS tunnel health
```bash
curl -s http://localhost:8080/api/tunnel-health | python3 -m json.tool
# Should show 4 LSP entries with state=UP and health_score close to 1.0
```

#### NetFlow summary
```bash
curl -s http://localhost:8080/api/netflow | python3 -m json.tool
# Shows total_flows, total_bytes — "unavailable" if netflow_simulator not running
```

---

### Phase 6 — Scenario validation

Run all 4 PS-13 scenarios with the automated suite:

```bash
# Run all 4 scenarios (synthetic injection, no Containerlab required)
python3 phase6-validation/run_scenarios.py --no-containerlab

# Run all 4 scenarios with real Containerlab tc-netem + vtysh
python3 phase6-validation/run_scenarios.py

# Run a single scenario
python3 phase6-validation/run_scenarios.py --scenario 1 --no-containerlab
python3 phase6-validation/run_scenarios.py --scenario 4 --no-containerlab
```

The suite produces a timestamped JSON report in `phase6-validation/`.

**Individual scenario commands (manual):**

**Scenario 1 — Gradual link degradation:**
```bash
# Add escalating latency on pe1 (run 30s apart)
docker exec clab-aether-pe1 tc qdisc add dev eth0 root netem delay 50ms
sleep 30
docker exec clab-aether-pe1 tc qdisc change dev eth0 root netem delay 200ms
sleep 30
docker exec clab-aether-pe1 tc qdisc change dev eth0 root netem delay 500ms
# Measure lead time:
python3 phase3-models/benchmark_harness.py
# Cleanup:
docker exec clab-aether-pe1 tc qdisc del dev eth0 root 2>/dev/null
```

**Scenario 2 — BGP route flap:**
```bash
python3 phase3-models/fault_streamer.py --inject flap
# Observe: CORE_PATH_FAILOVER RECOMMEND_ONLY alert appears in dashboard
# Click alert → Q1/Q2/Q3 + remediation commands for pe1 appear
```

**Scenario 3 — Telemetry collector failure:**
```bash
pkill -f fault_streamer.py
# Dashboard shows last known state — still responds to all API calls
curl -s http://localhost:8080/api/status | python3 -m json.tool
# Restart:
python3 phase3-models/fault_streamer.py &
```

**Scenario 4 — Controller misconfiguration (policy drift):**
```bash
# Inject: lower REROUTE_BRANCH threshold to unsafe level
curl -s -X PUT http://localhost:8080/api/policy \
  -H 'Content-Type: application/json' \
  -d '{"action":"REROUTE_BRANCH","min_conf":0.10,"auto_execute":true}'
# Verify drift:
curl -s http://localhost:8080/api/policy | python3 -m json.tool
# Restore:
curl -s -X PUT http://localhost:8080/api/policy \
  -H 'Content-Type: application/json' \
  -d '{"action":"REROUTE_BRANCH","min_conf":0.82,"auto_execute":true}'
```

---

## Useful diagnostic commands

```bash
# Check dashboard is running
curl -s http://localhost:8080/api/status

# Count ACPs in acp_logs/
ls phase3-models/acp_logs/ | wc -l

# View 5 most recent ACPs (compact)
ls -t phase3-models/acp_logs/ | head -5 | while read f; do
  python3 -c "import json; d=json.load(open('phase3-models/acp_logs/$f'))
print(d['acp_id'][:8], d['severity'], d['ml_analysis']['predicted_fault_class'], d['corroboration']['recommended_action'])"
done

# View IKB incident log
cat phase3-models/ikb/incidents.jsonl | tail -5 | python3 -c "
import sys,json
for line in sys.stdin:
    d=json.loads(line)
    print(d.get('acp_id','?')[:8], d.get('severity','?'), d.get('operator_feedback','pending'))"

# Restart dashboard (kills old process first)
pkill -f "phase5-dashboard/app.py" 2>/dev/null; sleep 1
python3 phase5-dashboard/app.py &

# Check what port 8080 is doing
lsof -i :8080

# Check Ollama model is loaded
curl -s http://localhost:11434/api/tags | python3 -c "
import sys,json; tags=json.load(sys.stdin)
for m in tags.get('models',[]): print(m['name'])"
```

---

## What is and isn't implemented

| Feature | Status | Location |
|---------|--------|----------|
| 7-node MPLS Containerlab topology | ✅ | `phase1-simulation/topology/` |
| Custom Prometheus exporter (interface + FRR + RTT/jitter) | ✅ | `phase2-telemetry/exporter.py` |
| RTT and jitter per-link measurement (ICMP ping) | ✅ | `phase2-telemetry/exporter.py` → `collect_latency_jitter()` |
| NetFlow/IPFIX synthetic flow simulator | ✅ | `phase2-telemetry/netflow_simulator.py` (port 9995) |
| Application traffic generator (iperf3 + synthetic) | ✅ | `phase2-telemetry/traffic_generator.py` |
| FRR syslog parser (native + syslog ADJCHANGE) | ✅ | `phase2-telemetry/syslog_parser.py` |
| BiLSTM fault classifier (5 classes) | ✅ | `phase3-models/predictive_engine.py` |
| Autoencoder anomaly detector | ✅ | `phase3-models/predictive_engine.py` |
| Attention heatmap → top_features | ✅ | `phase3-models/predictive_engine.py` |
| EMA self-calibrating threshold | ✅ | `phase3-models/inference_engine.py` → `EMAThreshold` |
| TTF regressor | ✅ | `phase3-models/predictive_engine.py` |
| Holt-Winters trend forecaster | ✅ | `phase3-models/trend_forecaster.py` |
| Predictive digital twin (API wired correctly) | ✅ | `phase3-models/digital_twin.py` |
| ACP schema with all v4 fields | ✅ | `phase3-models/acp_manager.py` |
| NetworkX graph corroboration + tunnel health | ✅ | `phase3-models/graph_model.py` |
| Per-service SLA tags | ✅ | `phase3-models/taxonomy.py` |
| Edge Policy Engine (EPE) | ✅ | `phase3-models/inference_engine.py` |
| Operator-configurable autonomy matrix | ✅ | Dashboard Policy Matrix view |
| Ed25519 model integrity signing | ✅ | `phase3-models/model_integrity.py` |
| Air-gap compliance report (signed) | ✅ | `phase3-models/airgap_compliance.py` |
| Lead-time benchmark harness (5/5, avg 503s) | ✅ | `phase3-models/benchmark_harness.py` |
| Natural fault timing state machine | ✅ | `phase3-models/fault_streamer.py` |
| Operator feedback CLI | ✅ | `phase3-models/feedback_cli.py` |
| IKB auto-logging (every ACP) | ✅ | `phase3-models/inference_engine.py` → `log_acp()` |
| Ollama + Mistral 7B (offline LLM) | ✅ | `phase4-llm/llm_copilot.py` |
| ChromaDB RAG over runbooks + ACPs | ✅ | `phase3-models/ikb_manager.py` |
| Q1/Q2/Q3 structured incident answers | ✅ | `/api/explain/{acp_id}` |
| NLQ natural language interface | ✅ | Dashboard Ask Aether view |
| FastAPI NOC Dashboard v5.0 | ✅ | `phase5-dashboard/app.py` |
| WebSocket live alert feed | ✅ | `/ws/alerts` |
| Time-Travel topology playback | ✅ | Dashboard History view |
| Live link utilization overlay (15s) | ✅ | `/api/metrics/live` |
| Remediation CLI commands (click-to-copy) | ✅ | `/api/explain/{acp_id}` → `remediation` field |
| MPLS tunnel health endpoint | ✅ | `/api/tunnel-health` |
| NetFlow summary endpoint | ✅ | `/api/netflow` |
| Scenario validation suite (all 4 PS-13) | ✅ | `phase6-validation/run_scenarios.py` |
| Data-plane links on containers | ❌ | eth1/eth2 veth pairs not created — only eth0 (management) exists |
| Prophet seasonality forecaster | ⚠️ | Code exists, needs hours of cyclical history to activate seasonal model |
| Real router API execution | ⚠️ | Remediation commands shown to operator but not auto-applied (air-gapped safety) |
