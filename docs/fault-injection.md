# Fault Injection — Project Aether

All fault injection methods write ACPs to `phase3-models/acp_logs/` and push them to the dashboard via WebSocket in real time.

---

## 1. Run the fault streamer (continuous)

The fault streamer is the primary way to generate live alert traffic for the dashboard.

```bash
# Natural mode (default) — state machine: long quiet periods + realistic fault bursts
python3 phase3-models/fault_streamer.py

# Fixed-cycle mode — rotates through all 6 fault classes every 4 seconds (fast, good for testing)
python3 phase3-models/fault_streamer.py --mode cycle

# Faster cycle for demo (2s interval)
python3 phase3-models/fault_streamer.py --mode cycle --interval 2

# Run one full pass through all fault classes then exit
python3 phase3-models/fault_streamer.py --mode cycle --once
```

Natural mode timing:
- **Quiet period**: Healthy heartbeat every 35–80s (2–4 events before first fault)
- **Fault burst**: 2–5 consecutive fault events, 10–20s apart — same fault type persists
- **Resolve**: 1–2 Healthy events (8–15s each) before returning to quiet

---

## 2. Inject a single fault class immediately

Writes exactly one ACP and exits. Use this to test a specific alert type without running the full streamer.

```bash
# Control-Plane Flap (BGP) — triggers CORE_PATH_FAILOVER (RECOMMEND_ONLY)
python3 phase3-models/fault_streamer.py --inject flap

# Packet Loss — triggers REROUTE_BRANCH (auto-execute if confidence ≥ 0.82)
python3 phase3-models/fault_streamer.py --inject loss

# Congestion / Queue Saturation — triggers QOS_SHAPE_QUEUE
python3 phase3-models/fault_streamer.py --inject rate

# Latency Drift — triggers REROUTE_BRANCH
python3 phase3-models/fault_streamer.py --inject latency

# Frame Corruption — triggers REROUTE_BRANCH
python3 phase3-models/fault_streamer.py --inject corrupt

# Healthy heartbeat (clears degraded state in dashboard)
python3 phase3-models/fault_streamer.py --inject Healthy
```

---

## 3. Inject via the inference engine directly (Python)

For custom scenarios or scripted testing:

```python
import sys
sys.path.insert(0, 'phase3-models')
from inference_engine import AetherInferenceEngine
import pandas as pd, random

engine = AetherInferenceEngine()
engine.load_models()

df = pd.read_csv('phase3-models/dataset_large.csv')
feat_cols = [c for c in df.columns if c not in ('fault_label', 'fault_location', 'timestamp')]
if engine.columns:
    feat_cols = [c for c in engine.columns if c in feat_cols]

# Pick a fault label: Healthy / flap / loss / corrupt / rate / latency
label = 'flap'
rows  = df[df['fault_label'] == label][feat_cols]
start = random.randint(0, len(rows) - 21)
window = rows.iloc[start:start + 20]

engine.sliding_window.clear()
for _, row in window.iterrows():
    engine.ingest_sample({c: float(row[c]) for c in feat_cols})

acp = engine.run_inference()
if acp:
    engine.log_acp(acp)
    engine.print_acp_summary(acp)
```

---

## 4. Inject via the API (curl)

The benchmark endpoint runs a full scenario pass and returns metrics:

```bash
# Run lead-time benchmark (replays dataset_small.csv through all fault classes)
curl -s http://localhost:8080/api/benchmark | python3 -m json.tool

# Check air-gap compliance (attempts outbound, confirms all fail, returns signed report)
curl -s http://localhost:8080/api/compliance | python3 -m json.tool

# Get current autonomy policy matrix
curl -s http://localhost:8080/api/policy | python3 -m json.tool

# Update a policy threshold (e.g. lower REROUTE_BRANCH confidence to 0.78)
curl -s -X PUT http://localhost:8080/api/policy \
  -H 'Content-Type: application/json' \
  -d '{"action":"REROUTE_BRANCH","field":"min_conf","value":0.78}'

# Mark an ACP as accepted (closes the recommendation loop)
curl -s -X POST http://localhost:8080/api/feedback \
  -H 'Content-Type: application/json' \
  -d '{"acp_id":"<paste-acp-id>","feedback":"accepted"}'

# Mark an ACP as rejected (feeds false-positive rate in IKB)
curl -s -X POST http://localhost:8080/api/feedback \
  -H 'Content-Type: application/json' \
  -d '{"acp_id":"<paste-acp-id>","feedback":"rejected"}'

# Get full Q1/Q2/Q3 LLM analysis for a specific ACP
curl -s http://localhost:8080/api/explain/<acp-id> | python3 -m json.tool
```

---

## 5. Inject at the Containerlab network layer (real tc-netem)

These commands target the actual FRR containers in the running topology. Requires the Containerlab topology to be active (`clab deploy`).

```bash
# Add 200ms latency + 10% jitter on pe1 eth0 (Scenario 1: gradual link degradation)
docker exec clab-aether-pe1 tc qdisc add dev eth0 root netem delay 200ms 10ms distribution normal

# Increase to 500ms latency (escalation)
docker exec clab-aether-pe1 tc qdisc change dev eth0 root netem delay 500ms 50ms

# Add 20% packet loss on p1 (Scenario 2: packet loss)
docker exec clab-aether-p1 tc qdisc add dev eth0 root netem loss 20%

# Add packet corruption on pe2 (Scenario: frame corruption)
docker exec clab-aether-pe2 tc qdisc add dev eth0 root netem corrupt 5%

# Limit bandwidth to 10 Mbps on pe1 (Scenario: congestion)
docker exec clab-aether-pe1 tc qdisc add dev eth0 root tbf rate 10mbit burst 32kbit latency 400ms

# Remove all tc disciplines (restore normal)
docker exec clab-aether-pe1 tc qdisc del dev eth0 root 2>/dev/null || true
docker exec clab-aether-p1  tc qdisc del dev eth0 root 2>/dev/null || true
docker exec clab-aether-pe2 tc qdisc del dev eth0 root 2>/dev/null || true

# Trigger BGP flap by shutting then restoring a neighbor (Scenario 3: control-plane flap)
docker exec clab-aether-pe1 vtysh -c 'conf t' -c 'router bgp 65001' -c 'neighbor 192.168.12.2 shutdown' -c 'end'
sleep 10
docker exec clab-aether-pe1 vtysh -c 'conf t' -c 'router bgp 65001' -c 'no neighbor 192.168.12.2 shutdown' -c 'end'
```

---

## 6. Feedback CLI (operator accept/reject loop)

```bash
# List the 20 most recent ACPs with their feedback status
python3 phase3-models/feedback_cli.py --list

# Interactive mode — prompts to accept/reject each pending ACP
python3 phase3-models/feedback_cli.py

# Mark a specific ACP directly (non-interactive)
python3 phase3-models/feedback_cli.py --acp-id <id> --feedback accepted
python3 phase3-models/feedback_cli.py --acp-id <id> --feedback rejected

# Show false-positive rate per fault class (feeds IKB PSF recalibration)
python3 phase3-models/feedback_cli.py --stats
```

---

## Fault class → action mapping

| Fault label | Display name           | Action              | Default mode    |
|-------------|------------------------|---------------------|-----------------|
| `flap`      | Control-Plane Flap     | CORE_PATH_FAILOVER  | RECOMMEND_ONLY  |
| `loss`      | Packet Loss            | REROUTE_BRANCH      | auto ≥ 0.82     |
| `corrupt`   | Frame Corruption       | REROUTE_BRANCH      | auto ≥ 0.82     |
| `rate`      | Congestion / Saturation| QOS_SHAPE_QUEUE     | auto ≥ 0.75     |
| `latency`   | Latency Drift          | REROUTE_BRANCH      | auto ≥ 0.82     |
| `Healthy`   | Nominal                | NO_ACTION           | —               |

Thresholds are operator-editable via the Policy Matrix panel in the dashboard or `PUT /api/policy`.
