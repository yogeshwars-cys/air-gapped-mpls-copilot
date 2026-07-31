#!/usr/bin/env bash
# =============================================================================
# chunk3-setup.sh  —  post-deploy live configuration for the L3VPN lab.
#
# Run from phase1-simulation/topology, AFTER deploying:
#     sudo clab deploy -t chunk3.clab.yml
#     ./chunk3-setup.sh
#
# This is a SUPERSET of chunk2-setup.sh. On top of the Chunk 2 work (MPLS
# modules, sysctls, live OSPF-on-loopback + LDP) it adds the two things that
# make L3VPN go: (a) create the Linux 'CUST' VRF on each PE and enslave the
# PE-CE interfaces, (b) re-apply the VPNv4 / VRF BGP config live, because none
# of it survives a fresh container boot (the VRF device doesn't exist yet when
# FRR first reads frr.conf).
# =============================================================================
set -euo pipefail

LAB="${LAB:-aether}"               # canonical topology; override with LAB=chunk3 for the legacy lab
P() { echo "clab-${LAB}-$1"; }     # container-name helper:  $(P pe1) -> clab-${LAB}-pe1

# -----------------------------------------------------------------------------
echo "==> [1/6] Host: load MPLS kernel modules"
sudo modprobe mpls_router mpls_iptunnel 2>/dev/null || true

# -----------------------------------------------------------------------------
echo "==> [2/6] Core: MPLS label space + per-interface MPLS input"
# platform_labels must be non-zero before LDP/VPN can allocate labels.
for n in pe1 p1 pe2; do
  docker exec "$(P "$n")" sysctl -w net.mpls.platform_labels=1048575 >/dev/null
done
# 'input=1' on every interface that RECEIVES labeled packets (core-facing only;
# PE-CE links carry plain IP, so eth2/eth3 are left alone).
docker exec "$(P pe1)" sysctl -w net.mpls.conf.eth1.input=1 >/dev/null
docker exec "$(P p1)"  sysctl -w net.mpls.conf.eth1.input=1 >/dev/null
docker exec "$(P p1)"  sysctl -w net.mpls.conf.eth2.input=1 >/dev/null
docker exec "$(P pe2)" sysctl -w net.mpls.conf.eth1.input=1 >/dev/null
# Make sure forwarding is on (FRR image usually sets this; belt-and-suspenders).
for n in pe1 p1 pe2 ce-branch1 ce-hub ce-branch2 ce-dc; do
  docker exec "$(P "$n")" sysctl -w net.ipv4.ip_forward=1 >/dev/null
done

# -----------------------------------------------------------------------------
echo "==> [3/6] PEs: create customer VRF 'CUST' and enslave PE-CE interfaces"
# IMPORTANT: moving an interface into a VRF FLUSHES its IP address. We re-add the
# IPs (and the rest of the VPN config) live in step [5].
setup_vrf() {
  local box="$1"; shift
  docker exec "$box" ip link add CUST type vrf table 10 2>/dev/null || true
  docker exec "$box" ip link set CUST up
  for ifc in "$@"; do
    docker exec "$box" ip link set "$ifc" master CUST
    docker exec "$box" ip link set "$ifc" up
  done
}
setup_vrf "$(P pe1)" eth2 eth3
setup_vrf "$(P pe2)" eth2 eth3

# -----------------------------------------------------------------------------
echo "==> [4/6] Wait for core OSPF to reach Full before layering LDP + BGP"
# A cold boot needs DR/BDR election to finish. Poll instead of guessing a sleep:
# wait until p1 sees BOTH neighbors (pe1, pe2) in Full state.
for i in $(seq 1 30); do
  fulls=$(docker exec "$(P p1)" vtysh -c "show ip ospf neighbor" 2>/dev/null | grep -c "Full" || true)
  if [ "${fulls:-0}" -ge 2 ]; then echo "    OSPF Full on both core links (after ${i}x2s)"; break; fi
  sleep 2
done

# -----------------------------------------------------------------------------
echo "==> [5/6] Re-apply config that doesn't survive a fresh boot (live via vtysh)"

# ---- pe1 ----
docker exec -i "$(P pe1)" vtysh >/dev/null <<'EOF'
configure terminal
interface lo
 ip ospf area 0
exit
mpls ldp
 router-id 1.1.1.1
 address-family ipv4
  discovery transport-address 1.1.1.1
  interface eth1
 exit-address-family
exit
interface eth2 vrf CUST
 ip address 10.1.11.1/30
exit
interface eth3 vrf CUST
 ip address 10.1.13.1/30
exit
router bgp 65000
 bgp router-id 1.1.1.1
 no bgp default ipv4-unicast
 neighbor 3.3.3.3 remote-as 65000
 neighbor 3.3.3.3 update-source lo
 address-family ipv4 vpn
  neighbor 3.3.3.3 activate
 exit-address-family
exit
router bgp 65000 vrf CUST
 bgp router-id 10.1.11.1
 no bgp ebgp-requires-policy
 neighbor 10.1.11.2 remote-as 65101
 neighbor 10.1.13.2 remote-as 65103
 address-family ipv4 unicast
  neighbor 10.1.11.2 activate
  neighbor 10.1.13.2 activate
  label vpn export auto
  rd vpn export 65000:1
  rt vpn both 65000:1
  export vpn
  import vpn
 exit-address-family
exit
end
EOF

# ---- pe2 ----
docker exec -i "$(P pe2)" vtysh >/dev/null <<'EOF'
configure terminal
interface lo
 ip ospf area 0
exit
mpls ldp
 router-id 3.3.3.3
 address-family ipv4
  discovery transport-address 3.3.3.3
  interface eth1
 exit-address-family
exit
interface eth2 vrf CUST
 ip address 10.2.12.1/30
exit
interface eth3 vrf CUST
 ip address 10.2.14.1/30
exit
router bgp 65000
 bgp router-id 3.3.3.3
 no bgp default ipv4-unicast
 neighbor 1.1.1.1 remote-as 65000
 neighbor 1.1.1.1 update-source lo
 address-family ipv4 vpn
  neighbor 1.1.1.1 activate
 exit-address-family
exit
router bgp 65000 vrf CUST
 bgp router-id 10.2.12.1
 no bgp ebgp-requires-policy
 neighbor 10.2.12.2 remote-as 65102
 neighbor 10.2.14.2 remote-as 65104
 address-family ipv4 unicast
  neighbor 10.2.12.2 activate
  neighbor 10.2.14.2 activate
  label vpn export auto
  rd vpn export 65000:1
  rt vpn both 65000:1
  export vpn
  import vpn
 exit-address-family
exit
end
EOF

# ---- p1 (pure P): just make sure loopback + LDP are live, like Chunk 2 ----
docker exec -i "$(P p1)" vtysh >/dev/null <<'EOF'
configure terminal
interface lo
 ip ospf area 0
exit
mpls ldp
 router-id 2.2.2.2
 address-family ipv4
  discovery transport-address 2.2.2.2
  interface eth1
  interface eth2
 exit-address-family
exit
end
EOF

# -----------------------------------------------------------------------------
echo "==> [6/6] Nudge BGP to re-establish now (skip backoff), then converge..."
docker exec "$(P pe1)" vtysh -c "clear bgp *"            >/dev/null 2>&1 || true
docker exec "$(P pe2)" vtysh -c "clear bgp *"            >/dev/null 2>&1 || true
for ce in ce-branch1 ce-hub ce-branch2 ce-dc; do
  docker exec "$(P "$ce")" vtysh -c "clear bgp *"        >/dev/null 2>&1 || true
done
# Poll for LDP OPERATIONAL and the VPNv4 session leaving Active/Connect/Idle.
for i in $(seq 1 30); do
  ldp_ok=$(docker exec "$(P pe1)" vtysh -c "show mpls ldp neighbor" 2>/dev/null | grep -c "OPERATIONAL" || true)
  vpn_up=$(docker exec "$(P pe1)" vtysh -c "show bgp ipv4 vpn summary" 2>/dev/null | grep -E "^3\.3\.3\.3" | grep -Ecv "Active|Connect|Idle|never" || true)
  if [ "${ldp_ok:-0}" -ge 1 ] && [ "${vpn_up:-0}" -ge 1 ]; then echo "    LDP up + VPNv4 established (after ${i}x2s)"; break; fi
  sleep 2
done
sleep 3

set +e   # from here on, keep going even if a check returns non-zero
echo
echo "=================== VERIFY ==================="

echo "--- [1] OSPF neighbors on p1 (expect 2x Full) ---"
docker exec "$(P p1)" vtysh -c "show ip ospf neighbor"

echo "--- [2] LDP neighbors on pe1 (expect OPERATIONAL) ---"
docker exec "$(P pe1)" vtysh -c "show mpls ldp neighbor"

echo "--- [3] VPNv4 iBGP session pe1<->pe2 (expect 3.3.3.3 Established, prefixes received) ---"
docker exec "$(P pe1)" vtysh -c "show bgp ipv4 vpn summary"

echo "--- [4] PE-CE eBGP on pe1 (expect 10.1.11.2 + 10.1.13.2 Established) ---"
docker exec "$(P pe1)" vtysh -c "show bgp vrf CUST ipv4 unicast summary"

echo "--- [5] VRF CUST routing table on pe1 (expect remote sites 12/13/14 .x learned via VPN) ---"
docker exec "$(P pe1)" vtysh -c "show ip route vrf CUST"

echo "--- [6] Core stays clean: p1 must hold ZERO customer routes ---"
if docker exec "$(P p1)" vtysh -c "show ip route" | grep -qE '1[1-4]\.1[1-4]\.1[1-4]\.1[1-4]'; then
  echo "  !! FAIL: customer routes leaked into the core (p1)"
else
  echo "  OK: p1 has no customer routes — pure label switching, as designed"
fi

echo "--- [7] DATAPLANE: ce-branch1 (11.11.11.11) -> ce-dc (14.14.14.14) across the L3VPN ---"
docker exec "$(P ce-branch1)" ping -c 3 -I 11.11.11.11 14.14.14.14

echo "=============================================="
echo "If [3] shows the session Established with prefixes, [5] lists 12/13/14,"
echo "[6] says OK, and [7] is 0% loss — Chunk 3 is up and the VPN forwards."
