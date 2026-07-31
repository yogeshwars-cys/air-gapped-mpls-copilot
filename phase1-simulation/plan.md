# Phase 1 — Network Simulation: Progress Log

## Goal
Build a reproducible, air-gapped Containerlab topology simulating SD-WAN over
MPLS that boots cleanly, routes traffic, and supports fault injection. Phase 1
produces realistic telemetry; analysis is deferred to Phase 2/3.

## Tooling Decisions (locked)
- **OS:** Bare-metal Fedora Linux (MPLS, namespaces, tc netem are Linux-native)
- **Simulator:** Containerlab — declarative YAML, reproducible, lightweight (no VMs)
- **Router software:** FRRouting (FRR) — free, air-gap-friendly, supports MPLS/OSPF/BGP/VRF
- **Runtime:** Docker CE

## Environment Status: WORKING
- Docker CE installed (resolved Fedora moby-engine conflict)
- Containerlab 0.76.1 installed
- User in clab_admins group
- MPLS kernel modules (`mpls_router`, `mpls_iptunnel`) load on boot via `/etc/modules-load.d/mpls.conf`
- `docker run hello-world` ✓

## Team Git Workflow (rules)
- No direct commits to `master`
- Work on a branch → commit → push → PR (base: master ← compare: branch) → merge
- `git pull origin master` before starting work each day
- `.gitignore` excludes Containerlab runtime dirs (clab-*/)

## Phase 1 Chunk Breakdown
- [x] **Chunk 1 — Minimal topology** (2 FRR routers, 1 link, ping works)
- [x] **Chunk 2 — MPLS core** (2 PE + 1 P, OSPF reachability, LDP labels, verified LSP)
- [x] **Chunk 3 — CE sites + VPN** (2 branch + 1 hub + 1 DC, L3VPN/VRF, PE-CE BGP)
- [x] **Chunk 4 — Traffic generation** (iperf3 → VoIP/bulk/web traffic profiles)
- [x] **Chunk 5 — Fault injection** (Python + tc netem, gradual degradation, labeled)

**Demoable Phase 1 = Chunks 1–5 working. The core platform simulation, traffic, and fault engine are now fully built.**

## Progress

### Chunk 1 — DONE (merged PR #1)
- Created `phase1-simulation/topology/step1.clab.yml`: nodes r1, r2 (FRR), one link
- FRR configs: static IPs 10.0.0.1/24 and 10.0.0.2/24 on eth1
- `daemons` files: zebra + ospfd + staticd enabled (ready for Chunk 2)
- `sudo clab deploy -t step1.clab.yml` → both containers running
- `docker exec clab-step1-r1 ping -c 3 10.0.0.2` → **0% packet loss** ✓
- Committed, pushed, PR #1 merged to master

### Chunk 2 — DONE (branch charan/chunk2-mpls-core)
3-router MPLS core: pe1 ←→ p1 ←→ pe2.

- Topology: `phase1-simulation/topology/chunk2.clab.yml` — 3 FRR nodes, 2 links
  (pe1:eth1↔p1:eth1, p1:eth2↔pe2:eth1)
- Configs per router (`configs/pe1`, `configs/p1`, `configs/pe2`):
  - `daemons` — zebra + ospfd + ldpd enabled
  - `frr.conf` — interface IPs, loopbacks (1.1.1.1 / 2.2.2.2 / 3.3.3.3), OSPF, LDP
  - `vtysh.conf` — `service integrated-vtysh-config` (all daemons read one frr.conf)
- Automation: `phase1-simulation/topology/chunk2-setup.sh` — loads MPLS modules,
  sets MPLS sysctls per container, applies OSPF-on-loopback + LDP live, self-verifies
- **Verified:** OSPF neighbors Full, LDP sessions OPERATIONAL, label-switched path
  pe1→pe2 active (label 18→17), `ping pe1→pe2` **0% loss** with ttl=63 (proves
  traffic transits p1 via MPLS, not plain IP)
- Reproducible end-to-end: clean destroy → deploy → setup script passes hands-off

### Chunk 3 — DONE (branch charan/chunk3-l3vpn)
4 CE sites on the Chunk 2 core, one customer VPN (vrf CUST, RD/RT 65000:1).
- Configs: pe1/pe2 gained vrf CUST + VPNv4 iBGP + PE-CE eBGP; 4 new CE configs; p1 unchanged
- Automation: chunk3-setup.sh — creates VRF + enslaves, polls OSPF→Full, re-applies LDP/VPN live, self-verifies
- **Verified:** VPNv4 PfxRcd 2, two-label stack live (label 16/80), ping ce-branch1→ce-dc 0% loss ttl=62, p1 zero customer routes
- Gotchas fixed: RFC8212 ebgp-requires-policy def

**IP / addressing plan:**

| Router | Role | eth1 | eth2 | Loopback |
|---|---|---|---|---|
| pe1 | Provider Edge | 10.0.1.1/24 | — | 1.1.1.1/32 |
| p1  | Provider Core | 10.0.1.2/24 | 10.0.2.1/24 | 2.2.2.2/32 |
| pe2 | Provider Edge | 10.0.2.2/24 | — | 3.3.3.3/32 |

**Key gotchas hit + fixed (documented in docs/phase1-simulation-doc/chunk2.md):**
1. MPLS kernel modules must be loaded on host (`modprobe mpls_router mpls_iptunnel`)
2. MPLS sysctls (`platform_labels`, `conf.<iface>.input=1`) needed per container, every deploy
3. `mpls ldp` block + `ip ospf area 0` on loopback don't reliably load from frr.conf
   at boot → applied live via vtysh in the setup script
4. Loopbacks MUST be in OSPF or LDP can't form its TCP session (discovery works
   link-local, but the session needs the loopback to be routable)
5. LDP needs OSPF to converge first → setup script waits between steps

### Team Checkpoint — Chunk 1: IN PROGRESS
All 3 teammates must clone + reproduce Chunk 1 ping (0% loss) on their machines.

    git clone https://github.com/ankamteja/air-gapped-mpls-copilot
    cd air-gapped-mpls-copilot/phase1-simulation/topology
    docker pull frrouting/frr:latest
    sudo clab deploy -t step1.clab.yml
    docker exec clab-step1-r1 ping -c 3 10.0.0.2
    sudo clab destroy -t step1.clab.yml

### Team Checkpoint — Chunk 2: PENDING
After Chunk 2 merges, all 3 teammates reproduce the MPLS core before Chunk 3.

    git pull origin master
    cd phase1-simulation/topology
    docker pull frrouting/frr:latest
    sudo clab deploy -t chunk2.clab.yml
    ./chunk2-setup.sh

Script self-verifies — ends with LDP OPERATIONAL + 0% packet loss if the machine is good.

## Chunk 3 Reproduction — L3VPN (do this before Chunk 4)

Pull the merged branch and run the clean cycle:

    git pull origin master
    cd phase1-simulation/topology
    docker pull frrouting/frr:latest
    sudo clab destroy -t chunk3.clab.yml 2>/dev/null   # in case an old lab is up
    sudo clab deploy  -t chunk3.clab.yml
    ./chunk3-setup.sh

The script is hands-off and self-verifies. Watch for:
  [4] "OSPF Full on both core links (after Nx2s)"   ← it polls, may take ~40-60s
  [6] "LDP up + VPNv4 established (after Nx2s)"

Your machine PASSES if the seven checks show:
  [1] OSPF neighbors: 2x Full
  [2] LDP: OPERATIONAL
  [3] VPNv4 (3.3.3.3): Established, PfxRcd 2   (not Active/Idle)
  [4] PE-CE eBGP: both neighbors, PfxRcd 1 each
  [5] VRF CUST: 12.12.12.12/32 + 14.14.14.14/32 via 3.3.3.3, label X/80
  [6] "OK: p1 has no customer routes"
  [7] ping ce-branch1 -> ce-dc: 0% packet loss, ttl=62

The money line is [5] "label X/80" — that's the two-label stack
(outer LDP transport + inner VPN label). [7] at ttl=62 proves the
packet transited the core (4 hops) and [6] proves p1 never saw a
customer route. That combination = working MPLS L3VPN.

Tear down when done:
    sudo clab destroy -t chunk3.clab.yml

If [7] fails but [1]-[6] pass, it's almost always the cold-boot timing —
just run ./chunk3-setup.sh once more on the same lab. If it still fails,
flag it (don't change config) and we'll debug the kernel MPLS dataplane.

### Chunk 4 — DONE (traffic_generator.sh)
- Simulates realistic government & enterprise traffic profiles across the L3VPN topology.
- Runs:
  - **VoIP/VTC:** Continuous UDP stream (150kbps to 1.5Mbps) on port 5001.
  - **Database Backup:** Bursty TCP transfers (5Mbps) every 30 seconds on port 5002.
  - **Intranet HTTP Web Traffic:** Randomized curl loop query on port 8080.
  - **Administrative Session Control:** Emulated SSH low-bandwidth TCP activity.
- Usage:
  ```bash
  ./traffic_generator.sh
  ```

### Chunk 5 — DONE (fault_injector.py)
- Python-based fault injection manager executing network emulation (`tc qdisc`) inside FRR containers.
- Supported faults: `latency` (with optional jitter), packet `loss`, packet `corruption`, bandwidth `rate` limits, and link `flaps`.
- Writes a timestamped log to `faults_log.csv` for downstream supervised ML dataset labeling.
- Usage:
  ```bash
  # Inject 15% packet loss on pe1 eth1 for 60 seconds
  python3 fault_injector.py --node pe1 --interface eth1 --fault loss --value 15% --duration 60
  
  # Inject 100ms delay with 10ms jitter on ce-branch1 eth1
  python3 fault_injector.py --node ce-branch1 --interface eth1 --fault latency --value "100ms 10ms" --duration 120
  ```

