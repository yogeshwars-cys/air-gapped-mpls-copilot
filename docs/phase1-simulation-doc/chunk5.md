# Chunk 5 — Fault Injection, Ground-Truth Labelling & Continuous Dataset Collection

**Goal:** Inject reproducible network impairments into the live lab using Linux `tc netem` and write timestamped fault labels to `faults_log.csv` so the ML training pipeline has accurate ground truth.  
**Status:** ✅ Done — `fault_injector.py` supports 5 fault types with auto-recovery and CSV logging. Enhanced in v4 with `continuous_fault_loop.sh` for hours-long parameterized dataset collection.

---

## File

```
phase1-simulation/topology/fault_injector.py
```

The log it writes is read by `phase3-models/data_collector.py` to label each telemetry sample in real time.

---

## Supported Fault Types

| `--fault` | Mechanism | `--value` example | What it simulates |
|---|---|---|---|
| `latency` | `tc netem delay` | `100ms 20ms` | WAN link propagation delay / jitter |
| `loss` | `tc netem loss` | `15%` | Packet loss from congestion or physical error |
| `corrupt` | `tc netem corrupt` | `3%` | Bit errors from a degraded physical medium |
| `rate` | `tc tbf rate` | `500kbit` | Bandwidth saturation / policing |
| `flap` | `ip link set down/up` | _(no value)_ | Interface reset, link failure |
| `none` | clears all qdisc | _(no value)_ | Manual recovery |

---

## Usage

```bash
# Inject 15% packet loss on the pe1→p1 core link for 60 seconds then auto-recover
python3 fault_injector.py --node pe1 --interface eth1 --fault loss --value "15%" --duration 60

# Permanent latency (stays until cleared)
python3 fault_injector.py --node p1 --interface eth2 --fault latency --value "80ms 10ms"

# Clear manually
python3 fault_injector.py --node p1 --interface eth2 --fault none
```

---

## Fault Log Format

`faults_log.csv` is appended on every inject and every recovery:

```
Timestamp,Node,Interface,FaultType,Value,DurationSeconds
2026-06-27T20:09:43,pe1,eth1,latency,100ms 20ms,60
2026-06-27T20:10:43,pe1,eth1,recovery,,0
```

`data_collector.py` reads this log in real time and writes the most-severe active fault as the `fault_label` column in `dataset.csv`, producing the supervised training signal for the LSTM Classifier and TTF Regressor.

---

## Architecture

```
fault_injector.py
    │
    ├── apply_fault()
    │       └── docker exec clab-chunk3-<node> tc qdisc add dev <iface> root netem ...
    │
    ├── log_fault()  → faults_log.csv  ←── read by data_collector.py
    │
    └── recover_fault()  (auto after --duration seconds, or manual)
```

The `tc netem` and `tc tbf` disciplines are applied **inside the container's network namespace** via `docker exec`, which is identical to running them on a real router's interface. No kernel modules need loading on the host (they are already present from `chunk3-setup.sh`).

---

## Validation Scenarios

These scenarios map directly to the **Phase 6** validation requirements from the problem statement:

| Scenario | Fault sequence | Expected LSTM class | Graph model response |
|---|---|---|---|
| Gradual congestion | `rate` gradually tightened from 5M → 500k | `Congestion / Saturation` | Bottleneck detected, QOS_SHAPE_QUEUE |
| BGP route flap | `flap` on pe1:eth1 (core link) | `Control-Plane Flap` | CORE_PATH_FAILOVER recommended |
| MPLS underlay degradation | `latency` + `loss` stacked on p1:eth1 | `Latency Drift` → `Packet Loss` | REROUTE_BRANCH |
| Corruption | `corrupt 5%` on pe2:eth2 | `Frame Corruption` | REROUTE_BRANCH |

---

## scenario_runner.sh

`scenario_runner.sh` wraps the fault injector to run all four scenarios end-to-end with timing:

```bash
cd phase1-simulation/topology
bash scenario_runner.sh all
```

This is used during **Phase 6 validation** to reproduce benchmark conditions with consistent timing for measuring prediction lead time.

---

## Taxonomy Alignment

Fault labels written to `faults_log.csv` match exactly the `label` field in `phase3-models/taxonomy.py`:

| faults_log value | taxonomy class | display name |
|---|---|---|
| `latency` | 1 | Latency Drift |
| `loss` | 2 | Packet Loss |
| `corrupt` | 3 | Frame Corruption |
| `rate` | 4 | Congestion / Saturation |
| `flap` | 5 | Control-Plane Flap |

---

## `continuous_fault_loop.sh` — Expanded Multi-Pattern Fault Simulation

Added in v4 (`phase1-simulation/topology/continuous_fault_loop.sh`), this script runs indefinitely and cycles through all six fault classes with 30+ parameter variants.

### Fault Parameter Tables

| Fault Type | Variants | Range |
|---|---|---|
| Latency spike | 8 | 20ms–400ms, with jitter; step and burst patterns |
| Packet loss | 6 | 0.5%–30% (random, burst, correlated models) |
| Frame corruption | 5 | 0.01%–2.0% corruption ratios |
| Rate limiting | 6 | 256kbps–1Mbps with burst headroom |
| Link flap (BGP) | 4 | 3s–20s reconvergence windows |

### Special Fault Patterns

**Gradual Latency Ramp** (`gradual_latency_ramp()`):
Steps through 6 latency levels (20→50→100→200→300→400ms), each held ~12s.
Designed to exercise the TTF Regressor's ability to detect early-stage degradation before SLA breach.

**Cascade Core Fault** (`cascade_core_fault()`):
Simultaneous injection: pe1 latency spike (200ms) + p1 packet loss (15%).
Represents realistic multi-node failure scenario; duration 60s with staggered recovery.

**BGP Flap Cascade** (`bgp_flap_cascade()`):
5× rapid link flap cycles with 10s reconvergence windows.
Tests control-plane fault class detection under oscillating BGP state.

### Healthy Windows

Between each fault, a 30–90s healthy window (random interval) teaches the autoencoder what "normal" looks like and prevents ML models from training exclusively on fault data.

### Dataset Collection (v2)

Run alongside `traffic_generator.sh` to produce a realistic mixed-traffic dataset:

```bash
# Terminal 1 — fault loop
bash phase1-simulation/topology/continuous_fault_loop.sh &

# Terminal 2 — traffic
bash phase1-simulation/topology/traffic_generator.sh &

# Terminal 3 — collector (4 hours)
python3 phase3-models/data_collector.py --duration 14400 --output phase3-models/dataset_v2.csv
```

Expected output: ~7,200 rows covering thousands of fault transitions with 30+ fault variants.

### Retraining on Dataset v2

```bash
cd phase3-models
python3 train_models.py --data dataset_v2.csv --epochs 100 --hidden-dim 128 --seq-len 30
```

Dataset v1 vs v2 comparison:

| Metric | v1 (dataset.csv) | v2 (dataset_v2.csv) |
|---|---|---|
| Rows | 721 | ~14,400 (4h) |
| Fault variants per class | 1–2 | 4–8 |
| Cascade faults | None | Yes |
| Gradual ramp | None | Yes |
| BGP flap patterns | Simple | 5× cascade reconvergence |
