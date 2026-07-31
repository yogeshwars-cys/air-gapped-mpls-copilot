# Chunk 3 — CE Sites + L3VPN (VRF + MP-BGP VPNv4)

**Goal:** Layer an MPLS L3VPN on the Chunk 2 core. Four customer sites (2 branch + 1 hub + 1 datacenter) reach each other across the provider, isolated in `vrf CUST`, while the P router carries zero customer routes.
**Status:** ✅ Done — verified on `charan/chunk3-l3vpn` (cross-site ping 0% loss, two-label stack confirmed).

---

## Topology

```
   ce-branch1                                            ce-branch2
  (AS 65101)                                            (AS 65102)
  11.11.11.11 ─┐                                      ┌─ 12.12.12.12
               │   ┌──────── PROVIDER CORE ────────┐  │
   ce-hub ─────┼──[pe1]──────[p1]──────[pe2]──┼─────── ce-dc
  (AS 65103)   │  1.1.1.1    2.2.2.2    3.3.3.3   │  (AS 65104)
  13.13.13.13 ─┘   (Chunk 2 core — untouched)       └─ 14.14.14.14
```

| Router | Role | Core (global) | Customer (vrf CUST) | Loopback | AS |
|---|---|---|---|---|---|
| pe1 | Provider Edge | eth1 10.0.1.1/24 | eth2 10.1.11.1/30, eth3 10.1.13.1/30 | 1.1.1.1/32 | 65000 |
| p1  | Provider core | eth1/eth2 | — | 2.2.2.2/32 | — |
| pe2 | Provider Edge | eth1 10.0.2.2/24 | eth2 10.2.12.1/30, eth3 10.2.14.1/30 | 3.3.3.3/32 | 65000 |
| ce-branch1 | Customer | — | eth1 10.1.11.2/30 | 11.11.11.11/32 | 65101 |
| ce-hub | Customer | — | eth1 10.1.13.2/30 | 13.13.13.13/32 | 65103 |
| ce-branch2 | Customer | — | eth1 10.2.12.2/30 | 12.12.12.12/32 | 65102 |
| ce-dc | Customer | — | eth1 10.2.14.2/30 | 14.14.14.14/32 | 65104 |

VRF `CUST`: RD `65000:1`, RT `65000:1` import+export on both PEs → any-to-any reachability.

---

## What Chunk 3 adds on top of the core

```
Chunk 2 gave us:  OSPF + LDP  →  reach + label-switch the provider loopbacks
Chunk 3 adds:
  VRF CUST          →  a separate routing table per customer on each PE
  PE-CE eBGP        →  each CE hands its site prefix to its PE
  MP-BGP VPNv4      →  PEs swap customer routes, tagged with a VPN label + RT
  two-label stack   →  outer (LDP, transport) + inner (VPN, identity)
```

Dependency order (extends the Chunk 2 chain):
**IPs → OSPF → LDP → MPLS → BGP-VPNv4.** Each layer needs the one below converged first.

---

## File Structure

```
phase1-simulation/
├── configs/
│   ├── pe1/  pe2/   { daemons(+bgpd), frr.conf(VRF+VPNv4+PE-CE), vtysh.conf }
│   ├── p1/         { daemons, frr.conf, vtysh.conf }  # pure P, unchanged
│   └── ce-branch1/ ce-hub/ ce-branch2/ ce-dc/  { daemons(zebra+bgpd), frr.conf, vtysh.conf }
└── topology/
    ├── chunk3.clab.yml      # 7-node blueprint (3 core + 4 CE, 6 links)
    └── chunk3-setup.sh      # post-deploy automation + self-verify
```

**PE frr.conf** holds four blocks: core (global OSPF/LDP), customer interfaces (`vrf CUST`), VPNv4 iBGP to the far PE, and the VRF BGP (PE-CE neighbors + RD/RT/`label vpn export auto`).
**CE frr.conf** is trivial: link IP, loopback, one eBGP peer, advertise its `/32`.

---

## Deploy

```bash
cd phase1-simulation/topology
sudo clab deploy -t chunk3.clab.yml
./chunk3-setup.sh
```

`chunk3-setup.sh` is a superset of `chunk2-setup.sh`. It (1) loads MPLS modules + sets sysctls, (2) **creates the `CUST` VRF on each PE and enslaves the PE-CE interfaces**, (3) **waits for OSPF to reach Full** (polls, not a fixed sleep), (4) re-applies the boot-fragile config live via vtysh (loopback OSPF, the full LDP block, VRF interface IPs, VPNv4 + VRF BGP), (5) nudges BGP past backoff and polls for LDP/VPNv4, then self-verifies.

```bash
sudo clab destroy -t chunk3.clab.yml
```

---

## Verification

```bash
# Core still healthy
docker exec clab-chunk3-p1  vtysh -c "show ip ospf neighbor"      # 2x Full
docker exec clab-chunk3-pe1 vtysh -c "show mpls ldp neighbor"     # OPERATIONAL

# VPN control plane
docker exec clab-chunk3-pe1 vtysh -c "show bgp ipv4 vpn summary"            # 3.3.3.3 Established, PfxRcd 2
docker exec clab-chunk3-pe1 vtysh -c "show bgp vrf CUST ipv4 unicast summary"  # PE-CE neighbors, PfxRcd 1 each
docker exec clab-chunk3-pe1 vtysh -c "show ip route vrf CUST"     # 12/14 via 3.3.3.3, label X/80

# Core is clean (the whole point)
docker exec clab-chunk3-p1  vtysh -c "show ip route"             # NO 11/12/13/14

# Dataplane across the VPN
docker exec clab-chunk3-ce-branch1 ping -c 3 -I 11.11.11.11 14.14.14.14   # 0% loss, ttl=62
```

**Result (confirmed):** the VRF route for `14.14.14.14/32` resolves as
`via 3.3.3.3 (recursive), label 16/80` — outer LDP transport label + inner VPN label,
the **two-label stack**. Ping `ce-branch1 → ce-dc` is 0% loss at `ttl=62` (4 hops:
branch1 → pe1 → p1 → pe2 → dc), and p1 holds zero customer routes. That combination
*is* the MPLS L3VPN proof: customer traffic crosses a core that has no idea the
customer addresses exist.

---

## Gotchas (every one we actually hit, with the fix)

1. **RFC 8212 / `bgp ebgp-requires-policy` defaults ON (FRR 8.4).**
   PE-CE eBGP sessions came **up but exchanged zero prefixes** — `show bgp ... summary`
   showed `(Policy)` in the PfxRcd column. Modern FRR silently rejects all eBGP routes
   unless a route-map is attached. **Fix:** `no bgp ebgp-requires-policy` on both PE VRF
   instances and all four CEs (baked into the configs).

2. **VRF chicken-and-egg at boot.** The Linux `CUST` VRF device doesn't exist when FRR
   first reads `frr.conf`, so `interface ethX vrf CUST` and the entire `router bgp 65000
   vrf CUST` block don't load. **Fix:** the setup script creates the VRF device
   (`ip link add CUST type vrf table 10`), enslaves eth2/eth3, then re-applies the VRF
   config live via vtysh.

3. **Enslaving an interface flushes its IP.** `ip link set eth2 master CUST` wipes the
   address. **Fix:** the setup script re-adds the PE-CE interface IPs (via vtysh) *after*
   enslaving, not before.

4. **The LDP block must be re-applied in full.** Re-applying only `interface eth1` under
   `mpls ldp` (without `router-id` + `discovery transport-address`) leaves LDP unable to
   start discovery — `show mpls ldp neighbor` stays empty and VPNv4 sits `Active`.
   **Fix:** re-apply the complete `mpls ldp` block live (same lesson as Chunk 2 — the
   whole block, not a fragment).

5. **Cold-boot OSPF race.** On a *cold* deploy, DR/BDR election isn't finished after a
   short fixed sleep — OSPF is still `2-Way/DROther` when the script tries to layer
   LDP/BGP, so loopbacks aren't routable yet and everything downstream fails. (A warm
   lab masked this.) **Fix:** poll until OSPF shows `Full` on both core links before
   proceeding, and poll for LDP/VPNv4 afterward, instead of guessing a sleep.

6. **MPLS sysctls per container, every deploy** (carried from Chunk 2):
   `net.mpls.platform_labels` + `net.mpls.conf.<core-iface>.input=1`. Applied by the
   setup script; PE-CE links are plain IP and are left alone.

7. **`set -e`/`pipefail` + a `grep` readiness poll = silent instant abort.** The fix for
   gotcha #5 (poll until OSPF is Full) backfired at first: `fulls=$(... | grep -c "Full")`
   returns exit status **1** when there are zero matches (grep's "no match" signal), and
   under `set -euo pipefail` that killed the script on the very first poll — the exact
   opposite of waiting. **Fix:** guard the substitution with `|| true` and default the
   value (`${fulls:-0}`) so "not ready yet" reads as `0` instead of aborting.

---

## Team Checkpoint

Reproduce the L3VPN before Chunk 4 — clean cycle, hands-off:

```bash
git pull origin master
cd phase1-simulation/topology
docker pull frrouting/frr:latest
sudo clab destroy -t chunk3.clab.yml 2>/dev/null
sudo clab deploy  -t chunk3.clab.yml
./chunk3-setup.sh
```

The script self-verifies. If [3] is Established with prefixes, [5] lists 12/13/14,
[6] says the core is clean, and [7] is 0% loss — that machine is good.

---

## What's Next

→ **Chunk 4 — Traffic generation:** iperf3 traffic profiles (VoIP / bulk / web) across
the VPN, so later fault injection has realistic flows to degrade.

*(Future extension, noted: hub-and-spoke RT policy and SD-WAN IPsec overlay — both ride
on this same VRF/RT machinery without changing the core.)*
