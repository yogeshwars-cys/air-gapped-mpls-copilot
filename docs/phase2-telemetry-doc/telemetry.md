# Phase 2 — Telemetry Pipeline

**Goal:** Collect high-frequency (1 s interval) metrics from all 7 simulated nodes without modifying the FRRouting containers, label them with active fault state in real time, and store them in a local Prometheus instance and flat CSV dataset for ML training.

**Status:** ✅ Done — exporter verified scraping OSPF/LDP/BGP/VRF state + all interface counters from live chunk3 lab. Prometheus and Grafana running via Docker Compose. `data_collector.py` appending labeled rows to `dataset.csv` at 1 s intervals.

---

## Architecture Overview

```
                      +---------------------------------------+
                      |                 HOST                  |
                      |                                       |
  +--------------+    |  +-------------+     +-------------+  |
  | Containerlab |    |  | Exporter    |     | Prometheus  |  |
  |   7 Nodes    |--->|  | (port 8000) |<----| (port 9090) |  |
  +--------------+    |  +-------------+     +-------------+  |
                      |                             |         |
                      |                             v         |
                      |                      +-------------+  |
                      |                      |   Grafana   |  |
                      |                      | (port 3000) |  |
                      |                      +-------------+  |
                      +---------------------------------------+
```

1. **`exporter.py`** runs on the host, uses `docker exec` commands to run `vtysh` and `cat /proc/net/dev` inside the container namespaces, and formats them in Prometheus exposition format.
2. **Prometheus** runs in Docker Compose and scrapes `exporter.py` on the host via `host.docker.internal:8000`.
3. **Grafana** is provisioned to automatically load Prometheus as a datasource for immediate charting.

---

## File Structure

```
phase2-telemetry/
├── exporter.py                            # Custom standard-library python exporter
├── prometheus.yml                         # 1-second scrape interval config
├── docker-compose.yml                     # Prometheus & Grafana stack
└── grafana-provisioning/
    └── datasources/
        └── prometheus.yaml                # Auto-datasource configurations
```

---

## Running the Telemetry Pipeline

### Step 1: Start the Custom Exporter
```bash
python3 phase2-telemetry/exporter.py &
```
Binds on `0.0.0.0:8000`. Verify:
```bash
curl http://127.0.0.1:8000/metrics | head -5
```
> **Note:** Use `127.0.0.1`, not `localhost`. On Fedora/modern Linux, `localhost` resolves to `::1` (IPv6) but the exporter binds IPv4-only. Both the data collector and curl use `127.0.0.1` explicitly.

### Step 2: Start Prometheus + Grafana
```bash
cd phase2-telemetry && docker compose up -d
```
- **Prometheus:** http://localhost:9090 → Status → Targets → `mpls_simulation_exporter` should be **UP**
- **Grafana:** http://localhost:3000 (admin / admin) — Prometheus pre-configured as default datasource

### Step 3: Collect Labeled Dataset
```bash
# From project root — uses absolute path resolution for faults_log.csv
python3 phase3-models/data_collector.py --duration 600 --interval 1 --output phase3-models/dataset.csv
```
Run while fault_injector.py or scenario_runner.sh is active to capture labeled fault samples.

### Step 4: Access Panels
* **Prometheus Targets:** http://localhost:9090/targets — `mpls_simulation_exporter` should show **UP**
* **Grafana:** http://localhost:3000 (admin / admin) — Prometheus pre-configured as default datasource

---

## Key Metrics Exposed

### 1. Interface Statistics (All 7 Nodes)
Exposed for every active interface (e.g., `eth1`, `eth2`, `eth3`):
* `net_rx_bytes{node, interface}` / `net_tx_bytes{node, interface}`
* `net_rx_packets{node, interface}` / `net_tx_packets{node, interface}`
* `net_rx_errors{node, interface}` / `net_tx_errors{node, interface}`
* `net_rx_drops{node, interface}` / `net_tx_drops{node, interface}`

### 2. OSPF Session Metrics (`pe1`, `p1`, `pe2`)
* `frr_ospf_neighbors_total{node}` — Total configured neighbors.
* `frr_ospf_neighbor_full{node, neighbor}` — Evaluates to `1` if neighbor state is `Full`, and `0` otherwise.

### 3. MPLS LDP Metrics (`pe1`, `p1`, `pe2`)
* `frr_ldp_neighbors_total{node}` — Total LDP discovery sessions.
* `frr_ldp_session_operational{node, peer}` — Evaluates to `1` if LDP state is `OPERATIONAL`, and `0` otherwise.

### 4. BGP VPNv4 & VRF Metrics (PEs Only)
* `frr_bgp_vpn_established{node, peer}` — Evaluates to `1` if established, `0` otherwise.
* `frr_bgp_vpn_prefixes_received{node, peer}` — Number of customer route prefixes received.
* `frr_bgp_vrf_established{node, vrf, peer}` — PE-CE eBGP neighbor state.
* `frr_bgp_vrf_prefixes_received{node, vrf, peer}` — Prefix count from the CE node.

---

## Fault Label Merging

`data_collector.py` reads `phase1-simulation/topology/faults_log.csv` on every scrape and returns the currently active fault type (or `Healthy`). Priority when multiple faults overlap:

```
flap > loss > corrupt > latency > rate
```

The merged label is written as the `fault_label` column in `dataset.csv`, providing the supervised training signal for the LSTM Classifier and TTF Regressor.

---

## dataset.csv Schema

```
timestamp, fault_label, fault_location,
  <node>_<iface>_net_rx_bytes, ...,         # raw cumulative counters
  <node>_frr_ospf_neighbor_full, ...,       # control-plane state
  <node>_<iface>_net_rx_bytes_rate, ...     # per-second deltas (primary ML features)
```

The `_rate` columns are the primary ML input features. Raw counters are for debugging and autoencoder reconstruction scoring.

---

## Architecture Details

The exporter is **threaded** (`ThreadingMixIn`) so Prometheus scrapes every 1 s and the data collector can query simultaneously without blocking. Two bugs fixed from the original agy-generated code:

1. OSPF JSON: FRR returns `{"neighbors": {"1.1.1.1": [<list>]}}` — each value is a list, not a dict. Fixed to iterate the list.
2. LDP JSON: FRR uses `neighborId` (not `peerId`) and `state` (not `connectionState`). Fixed field names.

---

## Known Gaps & Future Work

| Gap | Impact | Fix |
|---|---|---|
| No RTT / jitter measurement | Latency faults visible only via counter change rates, not direct delay | Add `ping` RTT scraping or TWAMP-lite probe |
| No SNMP | Problem statement mentions SNMP ifInOctets etc; we use a custom exporter | Add `snmpwalk` scraping for parity |
| No NetFlow/IPFIX | Flow-level visibility absent | Add softflowd + ntopng inside containers |
| Exporter is single-process | If it crashes, collection stops | Add supervisord or systemd watchdog |
