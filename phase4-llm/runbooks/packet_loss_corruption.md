# Runbook: Packet Loss / Frame Corruption

## Symptoms
- rx_drops / tx_drops rising on core interfaces
- BiLSTM: fault_class = "loss" or "corrupt"
- Ping tests show > 1% loss on ce-branch1 → ce-dc path
- TCP retransmissions visible in flow records

## Diagnosis
1. Isolate which link: compare drops on pe1-eth1 vs p1-eth1
2. Check for CRC errors: rx_errors counter rising = physical layer
3. If drops without errors = queue overflow (QoS issue)
4. tc netem status: `tc qdisc show dev eth1` on suspect node

## Root Causes
1. Physical layer degradation (fiber, connector issue)
2. Deliberate tc netem injection (test/fault scenario)
3. MTU mismatch causing fragmentation drops
4. Queue overflow from traffic burst

## Mitigation
- **Reroute (auto-execute if confidence ≥ 80%):** Avoid the lossy link
  ```
  router bgp 65000 vrf CUST
   neighbor <pe2-ip> route-map PREPEND_OUT out
  ```
- **For corruption specifically:** Frame corruption indicates physical layer issue —
  reroute is the only safe automated action; do NOT attempt link reset autonomously

## SLA Impact
- Database replication: extremely sensitive — 0.01% loss causes TCP stalls
- VoIP: up to 0.5% loss tolerable; above = audible degradation
- Frame corruption (corrupt fault) = effective total loss for affected frames
