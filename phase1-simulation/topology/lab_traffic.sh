#!/usr/bin/env bash
# =============================================================================
# lab_traffic.sh — Air-gap-safe traffic load for the real Containerlab lab
#
# Generates genuine data-plane traffic across the MPLS L3VPN using only
# 'ping' between CE loopbacks (always present in the FRR image), so it works
# with ZERO internet egress — unlike traffic_generator.sh, which needs to
# apk-install iperf3. Runs fine from inside the --airgap namespace too:
# 'docker exec' talks to dockerd over its unix socket, not the network.
#
# Traffic pattern: rotating bursts between site pairs with varying packet
# sizes and rates, so link utilization on the dashboard breathes instead of
# sitting flat. Inter-site pairs traverse the full PE1—P1—PE2 core.
#
# Usage:  LAB=aether bash lab_traffic.sh     (run.sh starts this with --clab)
# =============================================================================
set -uo pipefail

LAB="${LAB:-aether}"
P() { echo "clab-${LAB}-$1"; }

# src_node src_loopback dst_loopback   (loopbacks are the VPN-advertised /32s)
PAIRS=(
  "ce-branch1 11.11.11.11 14.14.14.14"   # branch1 → dc      (full core path)
  "ce-branch2 12.12.12.12 13.13.13.13"   # branch2 → hub     (full core path)
  "ce-hub     13.13.13.13 14.14.14.14"   # hub → dc          (full core path)
  "ce-branch1 11.11.11.11 12.12.12.12"   # branch1 → branch2 (full core path)
  "ce-dc      14.14.14.14 11.11.11.11"   # dc → branch1      (reverse direction)
)

SIZES=(400 800 1200 1400)          # bytes — mixed frame sizes
INTERVALS=(0.002 0.003 0.006)      # s between packets — heavy/medium/light

if ! docker ps --format '{{.Names}}' | grep -q "$(P pe1)"; then
  echo "[lab_traffic] lab '$LAB' not running — nothing to do"; exit 1
fi

echo "[lab_traffic] generating VPN traffic across clab-${LAB}-* (ping-only, air-gap safe)"

# Three always-on flows so the exporter's ~15 s sampling window always sees
# sustained load (short bursts average out to 0 between scrapes). Each ping is
# bounded (-c) so strays die on their own if the parent is killed.
flow_loop() {
  while true; do
    pair=${PAIRS[$((RANDOM % ${#PAIRS[@]}))]}
    read -r node src dst <<< "$pair"
    size=${SIZES[$((RANDOM % ${#SIZES[@]}))]}
    ivl=${INTERVALS[$((RANDOM % ${#INTERVALS[@]}))]}
    docker exec "$(P "$node")" \
      ping -q -c 3000 -i "$ivl" -s "$size" -I "$src" "$dst" >/dev/null 2>&1
    sleep 1
  done
}

flow_loop & flow_loop & flow_loop &
wait
