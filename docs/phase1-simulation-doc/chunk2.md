# Chunk 2 — MPLS Core (OSPF + LDP)

**Goal:** 3 routers (2 PE + 1 P), OSPF for IP reachability, LDP for label distribution, verified label-switched path pe1 → pe2.
**Status:** ✅ Done — pushed to `charan/chunk2-mpls-core`

---

## Topology

```
        10.0.1.0/24              10.0.2.0/24
[pe1]eth1 ←——→ eth1[p1]eth2 ←——→ eth1[pe2]
1.1.1.1                2.2.2.2               3.3.3.3
(lo)                   (lo)                  (lo)
```

| Router | Role | eth1 | eth2 | Loopback |
|---|---|---|---|---|
| pe1 | Provider Edge | 10.0.1.1/24 | — | 1.1.1.1/32 |
| p1  | Provider Core | 10.0.1.2/24 | 10.0.2.1/24 | 2.2.2.2/32 |
| pe2 | Provider Edge | 10.0.2.2/24 | — | 3.3.3.3/32 |

Loopbacks are stable router IDs used by OSPF and LDP. PEs are the entry/exit points; P does pure label switching in the middle.

---

## What Each Protocol Does

```
zebra   →  base FRR, interfaces + IPs
ospfd   →  OSPF: routers discover each other, build IP map, advertise loopbacks
ldpd    →  LDP: assigns labels to reach each loopback destination
MPLS    →  packets travel by label swap, not IP lookup at each hop
```

Dependency order: **IPs → OSPF → LDP → MPLS forwarding.** Each layer needs the one below it working first.

---

## File Structure

```
phase1-simulation/
├── configs/
│   ├── pe1/  { daemons, frr.conf, vtysh.conf }
│   ├── p1/   { daemons, frr.conf, vtysh.conf }
│   └── pe2/  { daemons, frr.conf, vtysh.conf }
└── topology/
    ├── chunk2.clab.yml      # 3-node blueprint
    └── chunk2-setup.sh      # post-deploy automation
```

**daemons** — enables `zebra`, `ospfd`, `ldpd` on all three routers.
**frr.conf** — interface IPs, OSPF, and LDP config per router.
**vtysh.conf** — `service integrated-vtysh-config` so all daemons read one config file.

---

## Deploy

```bash
cd phase1-simulation/topology

# 1. Build the lab
sudo clab deploy -t chunk2.clab.yml

# 2. Run setup (modules, MPLS sysctls, OSPF-on-loopback, LDP, self-verify)
./chunk2-setup.sh
```

The setup script handles everything that doesn't persist across a fresh boot, then prints LDP neighbor state and a pe1 → pe2 ping at the end.

```bash
# Tear down when done
sudo clab destroy -t chunk2.clab.yml
```

---

## Verification

```bash
# OSPF neighbors — expect "Full"
docker exec clab-chunk2-pe1 vtysh -c "show ip ospf neighbor"

# LDP neighbors — expect "OPERATIONAL"
docker exec clab-chunk2-pe1 vtysh -c "show mpls ldp neighbor"

# Label bindings — 3.3.3.3 should show a label "In Use: yes"
docker exec clab-chunk2-pe1 vtysh -c "show mpls ldp binding"

# Ping across the LSP — expect 0% loss, ttl=63 (proves it went through p1)
docker exec clab-chunk2-pe1 ping -c 3 3.3.3.3
```

**Result:** LDP OPERATIONAL on all links, label-switched path active (pe1 pushes label, p1 swaps, pe2 pops), ping 0% packet loss.

---

## Gotchas (things that bit us, documented so the team avoids them)

1. **MPLS kernel modules** — `mpls_router` and `mpls_iptunnel` must be loaded on the host. Made persistent via `/etc/modules-load.d/mpls.conf`.
2. **MPLS sysctls** — `net.mpls.platform_labels` and `net.mpls.conf.<iface>.input=1` must be set inside each container after every deploy. Handled by the setup script.
3. **frr.conf doesn't fully load at boot** — the `mpls ldp` block and `ip ospf area 0` on loopback don't reliably parse from frr.conf at container start. The setup script applies them live via `vtysh`.
4. **Loopbacks must be in OSPF** — without `ip ospf area 0` on the `lo` interface, loopbacks aren't reachable, so LDP can't form its TCP session even though discovery (hello packets) works.
5. **Convergence timing** — LDP needs OSPF to converge first. The script waits between steps; don't check neighbor state too early.

---

## Team Checkpoint

Every teammate reproduces the MPLS core before Chunk 3:

```bash
git pull origin master
cd phase1-simulation/topology
docker pull frrouting/frr:latest
sudo clab deploy -t chunk2.clab.yml
./chunk2-setup.sh
```

The script self-verifies — if it ends with LDP OPERATIONAL and 0% packet loss, that machine is good.

---

## What's Next

→ **Chunk 3 — CE sites + L3VPN:** 2 branch + 1 hub + 1 datacenter, VRF segmentation, BGP between PE and CE.
