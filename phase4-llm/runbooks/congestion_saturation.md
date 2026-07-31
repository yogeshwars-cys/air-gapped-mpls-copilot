# Runbook: Link Congestion / Queue Saturation

## Symptoms
- Rising RTT on pe1→p1 or p1→pe2 links
- Interface tx_drops increasing
- BiLSTM classifier: fault_class = "rate" or "latency"
- Graph model: CLONE_BASELINE saturated, CLONE_QOS_THROTTLED wins

## Diagnosis Steps
1. Check link utilization: `tc -s qdisc show dev eth1`
2. Identify top traffic flows: check traffic_generator logs
3. Verify queue depth via interface counters
4. Run graph clonal search to quantify saturation ratio

## Root Causes
1. Traffic spike (VTC video + DB replication simultaneous)
2. Bandwidth policy misconfiguration (no QoS shaping applied)
3. Gradual traffic growth exceeding link capacity over time

## Mitigation
- **QoS Throttle (auto-execute at ≥75% confidence):**
  Throttle DB bulk transfer to 1Mbps, preserve VoIP headroom:
  ```
  tc qdisc add dev eth1 root handle 1: htb default 10
  tc class add dev eth1 parent 1: classid 1:1 htb rate 5mbit
  tc class add dev eth1 parent 1:1 classid 1:10 htb rate 4mbit ceil 5mbit prio 1
  tc class add dev eth1 parent 1:1 classid 1:20 htb rate 1mbit ceil 1mbit prio 2
  tc filter add dev eth1 protocol ip parent 1:0 prio 1 u32 match ip dport 5002 0xffff flowid 1:20
  ```
- **Reroute (if QoS insufficient):** Shift DB traffic to backup overlay tunnel

## SLA Impact
- VoIP breach: RTT > 150ms or loss > 0.5%
- Database breach: RTT > 50ms or loss > 0.01%
- Prediction lead time: typically 30–300s before breach (gradual onset)
