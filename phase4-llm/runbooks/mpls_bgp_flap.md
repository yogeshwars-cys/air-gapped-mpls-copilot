# Runbook: BGP Neighbor Flap / Control-Plane Instability

## Symptoms
- BGP neighbor transitions to Idle/Active state
- OSPF adjacency drops
- Route withdrawals visible in `show bgp summary`
- Packet loss spike on affected paths

## Diagnosis Steps
1. `show bgp ipv4 vpn summary` — check peer state and uptime
2. `show ip ospf neighbor` — verify OSPF adjacency state
3. `show mpls ldp neighbor` — verify LDP sessions
4. Check interface error counters: `show interface eth1`
5. Check syslog for BGP NOTIFICATION messages

## Root Causes (in order of likelihood)
1. Physical link flap — check `link flap` events in syslog
2. MTU mismatch — BGP OPEN rejected; check interface MTU
3. Hold-timer expiry — BGP keepalive not received in time (router overload)
4. Route policy change — inbound/outbound filter removing valid prefixes

## Mitigation
- **Auto-execute (high confidence):** Reroute traffic via backup SD-WAN tunnel (pe1↔pe2 overlay)
  ```
  router bgp 65000 vrf CUST
   neighbor 10.1.11.2 route-map PREPEND_OUT out
  ```
- **Recommend-only (core links):** Notify operator, do not auto-reconfigure hub/DC paths
- **Recovery:** After BGP re-establishes, verify prefix counts match baseline

## SLA Impact
- VoIP: degraded within 5s of adjacency loss
- Database: degraded within 10s
- Bulk transfer: tolerant up to 60s
