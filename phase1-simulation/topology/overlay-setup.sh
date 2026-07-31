#!/usr/bin/env bash
# =============================================================================
# overlay-setup.sh — SD-WAN overlay tunnel for Project Aether (PS-13 Objective 1)
#
# Builds a REAL GRE overlay tunnel between PE1 and PE2 that rides over the MPLS
# provider core (PE1 → P1 → PE2). This is the physical backing for the SD-WAN
# backup path that graph_model.py models as a high-cost overlay edge, and that
# the REROUTE_BRANCH remediation shifts traffic onto.
#
# Why GRE (and how IPSec fits):
#   The tunnel endpoints are the PE loopbacks (1.1.1.1 / 3.3.3.3), which are
#   reachable through the OSPF-routed core. GRE gives a genuine overlay interface
#   with its own /30 and end-to-end reachability independent of the L3VPN path.
#   In a production air-gapped deployment this GRE would be wrapped in an IPSec
#   transport SA (kernel xfrm / strongSwan) for confidentiality — the overlay
#   topology and routing are identical; only an ESP header is added. We keep the
#   tunnel GRE-only here so it runs in the stock frrouting/frr container without
#   pulling in an IKE daemon.
#
# Run AFTER the MPLS/VRF setup, once OSPF has the loopbacks reachable:
#     sudo containerlab deploy -t aether-lab.clab.yml
#     ./chunk3-setup.sh            # (or the unified setup)
#     LAB=aether ./overlay-setup.sh
#
# Override the lab name with LAB=... (default: aether).
# =============================================================================
set -euo pipefail

LAB="${LAB:-aether}"
P() { echo "clab-${LAB}-$1"; }       # $(P pe1) -> clab-aether-pe1

PE1_LO=1.1.1.1
PE2_LO=3.3.3.3
PE1_OVL=172.16.99.1
PE2_OVL=172.16.99.2
OVL_MASK=30
TUN=gre-sdwan

echo "==> [1/4] Host: load GRE kernel module"
sudo modprobe ip_gre 2>/dev/null || true

build_tunnel() {
  local box="$1" local_lo="$2" remote_lo="$3" ovl_ip="$4"
  # Remove any stale tunnel first so re-runs are idempotent
  docker exec "$box" ip link del "$TUN" 2>/dev/null || true
  docker exec "$box" ip tunnel add "$TUN" mode gre local "$local_lo" remote "$remote_lo" ttl 64
  docker exec "$box" ip addr add "${ovl_ip}/${OVL_MASK}" dev "$TUN"
  docker exec "$box" ip link set "$TUN" up
  # MTU headroom for the GRE (and a future ESP) header
  docker exec "$box" ip link set "$TUN" mtu 1400
}

echo "==> [2/4] PE1: build GRE overlay endpoint ($PE1_OVL → $PE2_LO)"
build_tunnel "$(P pe1)" "$PE1_LO" "$PE2_LO" "$PE1_OVL"

echo "==> [3/4] PE2: build GRE overlay endpoint ($PE2_OVL → $PE1_LO)"
build_tunnel "$(P pe2)" "$PE2_LO" "$PE1_LO" "$PE2_OVL"

echo "==> [4/4] Verify overlay reachability across the core (PE1 → PE2 via tunnel)"
if docker exec "$(P pe1)" ping -c 3 -W 2 "$PE2_OVL" >/dev/null 2>&1; then
  echo "    ✓ SD-WAN overlay UP — $PE1_OVL <-> $PE2_OVL reachable over PE1→P1→PE2"
else
  echo "    ✗ overlay ping failed — check that OSPF has $PE1_LO/$PE2_LO reachable"
  echo "      (run chunk3-setup.sh first so the core loopbacks are advertised)"
  exit 1
fi

echo
echo "Overlay summary:"
echo "  PE1 $TUN  $PE1_OVL/$OVL_MASK  ->  $PE2_LO"
echo "  PE2 $TUN  $PE2_OVL/$OVL_MASK  ->  $PE1_LO"
echo "  Transport: GRE over OSPF-routed core (PE1 → P1 → PE2)"
echo "  REROUTE_BRANCH can pin branch traffic onto this overlay when the"
echo "  primary L3VPN path degrades."
