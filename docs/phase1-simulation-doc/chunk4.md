# Chunk 4 — Traffic Generation

**Goal:** Simulate realistic government and enterprise application traffic across the L3VPN topology so the telemetry pipeline captures meaningful load patterns for ML training.  
**Status:** ✅ Done — `traffic_generator.sh` generates four traffic classes continuously across all CE sites.

---

## Traffic Profiles

| Class | Tool | Protocol | Ports | Bandwidth | Government / Enterprise Analogue |
|---|---|---|---|---|---|
| VoIP / VTC | iperf3 | UDP | 5001 | 150 kbps – 1.5 Mbps | Secure voice & video teleconferencing |
| Database replication | iperf3 | TCP | 5002 | 5 Mbps bursts | Oracle / PostgreSQL sync, backup jobs |
| HTTP Intranet | curl loop + python HTTP | TCP | 8080 | Bursty | LDAP, internal portals, HTTPS proxies |
| Admin / SSH | iperf3 low-BW TCP | TCP | 22 | ~50 kbps | Network management, syslog, SNMP traps |

---

## File

```
phase1-simulation/topology/traffic_generator.sh
```

---

## How It Works

```
ce-branch1 (11.11.11.11) ──[VoIP UDP]──────────────────> ce-dc (14.14.14.14)
ce-branch2 (12.12.12.12) ──[VTC UDP]───────────────────> ce-hub (13.13.13.13)
ce-hub     (13.13.13.13) ──[DB-backup TCP]─────────────> ce-dc (14.14.14.14)
All sites                ──[HTTP curl]──────────────────> ce-dc HTTP server
ce-branch1              ──[SSH emulation]───────────────> ce-hub
```

Traffic flows through the full MPLS L3VPN path: CE → PE → P → PE → CE, creating realistic label-forwarded load that the telemetry exporter captures as per-interface byte/packet counters.

### iperf3 Server Setup
The script first checks if `iperf3` servers are running inside each container and starts them if absent:
```bash
docker exec -d clab-chunk3-ce-dc   iperf3 -s -p 5001 -D
docker exec -d clab-chunk3-ce-dc   iperf3 -s -p 5002 -D
docker exec -d clab-chunk3-ce-hub  iperf3 -s -p 5001 -D
```

### Continuous Client Loop
Each traffic class runs in an infinite loop with randomised sleep intervals:
```bash
# VoIP — 150kbps UDP to ce-dc
while true; do
  docker exec clab-chunk3-ce-branch1 iperf3 -c 14.14.14.14 -p 5001 -u -b 150k -t 10 ...
  sleep $((RANDOM % 5 + 1))
done &
```

---

## Running

```bash
cd phase1-simulation/topology
bash traffic_generator.sh
```

The script backgrounds all loops and prints PIDs. Press Ctrl-C to stop all traffic (the script traps SIGINT and kills its children).

---

## Dependency

Requires `iperf3` installed inside the FRR containers. The script installs it on first run:
```bash
docker exec clab-chunk3-ce-dc apk add --no-cache iperf3
```

---

## Impact on ML Dataset

The traffic generator creates the **healthy baseline** the LSTM Autoencoder learns from. Without realistic traffic, the autoencoder learns silence, and any network activity appears anomalous. With varied traffic profiles running, the reconstruction threshold is calibrated to real-world load variance.

Key counters that move during traffic generation:
- `net_rx_bytes`, `net_tx_bytes` on `eth1`/`eth2`/`eth3` of all PE/CE nodes
- `net_rx_packets`, `net_tx_packets` — distinct from bytes, useful for detecting small-packet floods

---

## Relationship to Fault Injection (Chunk 5)

Chunk 4 (traffic) must be running **before** Chunk 5 (faults) so that:
1. The `data_collector.py` captures non-zero baseline traffic rates.
2. Fault effects (e.g., `rate` throttling, `loss`) are measurable as deviations from baseline.
3. The TTF regressor can learn the magnitude of degradation, not just its presence.
