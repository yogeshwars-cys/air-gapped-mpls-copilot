# Aether Network Topology Reference

## Site Layout
```
ce-branch1 ── pe1 ──┐
ce-hub     ── pe1   │
                    p1 (MPLS core)
ce-branch2 ── pe2 ──┘
ce-dc      ── pe2

pe1 ↔ pe2 : SD-WAN backup overlay (40ms, 2Mbps)
```

## Node Roles
| Node | Role | AS | VRF |
|---|---|---|---|
| pe1 | Provider Edge | 65000 | CUST |
| pe2 | Provider Edge | 65000 | CUST |
| p1  | Provider Core | 65000 | — |
| ce-branch1 | Customer Edge | 65001 | — |
| ce-branch2 | Customer Edge | 65002 | — |
| ce-hub | Customer Edge Hub | 65003 | — |
| ce-dc | Customer Edge DC | 65004 | — |

## Critical Paths
- VoIP: ce-branch1 → pe1 → p1 → pe2 → ce-dc (SLA: <150ms, <0.5% loss)
- DB Replication: ce-branch1 → pe1 → p1 → pe2 → ce-dc (SLA: <50ms, <0.01% loss)
- Video: ce-branch2 → pe2 → p1 → pe1 → ce-hub (SLA: <200ms)

## Routing Protocols
- OSPF area 0 on all PE/P links (metric: 10 for core, 5 for access)
- LDP for MPLS label distribution
- MP-BGP VPNv4 between pe1 and pe2 (RD: 65000:1)
- iBGP between pe1 and ce-sites (VRF CUST)

## Fault Injection Points
| Node | Interface | Effect |
|---|---|---|
| pe1 | eth1 | Core pe1→p1 link degradation |
| pe1 | eth2 | Branch1 access link |
| p1  | eth1 | Core p1→pe2 link degradation |
| pe2 | eth1 | Core pe2→p1 link |
| ce-dc | eth1 | DC access link |

## Autonomy Policy
| Action | Confidence | Auto? |
|---|---|---|
| Reroute via backup tunnel | ≥0.80 | YES (branch links) |
| QoS throttle bulk traffic | ≥0.75 | YES (any link) |
| Core path failover | ≥0.90 | NO — recommend only |
| Node isolation | any | NO — notify only |
