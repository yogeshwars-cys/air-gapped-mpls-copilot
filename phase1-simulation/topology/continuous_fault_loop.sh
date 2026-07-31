#!/usr/bin/env bash
# =============================================================================
# continuous_fault_loop.sh — Hours-long fault injection loop for dataset expansion
#
# Cycles through all 6 fault classes with:
#   - Varied intensities (mild / moderate / severe)
#   - Varied durations (15s / 30s / 60s)
#   - Multiple target nodes (pe1, p1, pe2)
#   - Multi-fault combinations (latency + loss simultaneously)
#   - Gradual onset (stepped ramps, not instant spikes)
#   - Recovery windows between faults (healthy baseline for classifier)
#
# NOTE: Targets eth0 (management interface) which is the only interface
# available when containerlab is deployed without data-plane links.
# This injects real tc netem faults that affect the eth0 rx/tx counters
# collected by the exporter, creating genuine feature variation.
#
# Run alongside data_collector.py --duration 14400 (4 hours)
# =============================================================================
set -euo pipefail

INJECTOR="$(dirname "$0")/fault_injector.py"
LAB="${LAB:-aether}"

P() { echo "clab-${LAB}-$1"; }

# Fault parameter tables — each row: node iface fault value duration_s
LATENCY_PARAMS=(
  "pe1 eth0 latency '20ms 2ms'  15"
  "pe1 eth0 latency '50ms 5ms'  30"
  "pe1 eth0 latency '100ms 15ms' 30"
  "pe1 eth0 latency '200ms 30ms' 30"
  "pe1 eth0 latency '350ms 50ms' 20"
  "p1  eth0 latency '30ms 5ms'  20"
  "p1  eth0 latency '120ms 20ms' 25"
  "pe2 eth0 latency '80ms 10ms' 25"
)

LOSS_PARAMS=(
  "pe1 eth0 loss '1%'  20"
  "pe1 eth0 loss '3%'  25"
  "pe1 eth0 loss '8%'  30"
  "pe1 eth0 loss '15%' 20"
  "p1  eth0 loss '5%'  20"
  "pe2 eth0 loss '10%' 25"
)

CORRUPT_PARAMS=(
  "pe1 eth0 corrupt '1%'  20"
  "pe1 eth0 corrupt '3%'  20"
  "pe1 eth0 corrupt '7%'  25"
  "p1  eth0 corrupt '2%'  20"
  "pe2 eth0 corrupt '5%'  20"
)

RATE_PARAMS=(
  "pe1 eth0 rate '5mbit'   20"
  "pe1 eth0 rate '2mbit'   25"
  "pe1 eth0 rate '1mbit'   25"
  "pe1 eth0 rate '500kbit' 20"
  "p1  eth0 rate '3mbit'   20"
  "pe2 eth0 rate '1mbit'   20"
)

FLAP_PARAMS=(
  "pe1 eth0 flap '' 10"
  "p1  eth0 flap '' 8"
  "pe2 eth0 flap '' 10"
  "pe1 eth0 flap '' 8"
)

inject() {
  local node="$1" iface="$2" fault="$3" value="$4" duration="$5"
  echo "  → inject $fault on $node/$iface val='$value' for ${duration}s"
  python3 "$INJECTOR" --node "$node" --interface "$iface" --fault "$fault" \
    ${value:+--value "$value"} --duration "$duration" 2>/dev/null || true
}

clear_all() {
  for node in pe1 p1 pe2; do
    python3 "$INJECTOR" --node "$node" --interface eth0 --fault none 2>/dev/null || true
  done
}

healthy_window() {
  local secs="${1:-20}"
  echo "  ── healthy baseline ${secs}s ──"
  clear_all
  sleep "$secs"
}

# ── Gradual latency ramp (mimics Scenario 1 closely) ─────────────────────────
gradual_latency_ramp() {
  local node="${1:-pe1}" iface="${2:-eth0}"
  echo "[RAMP] Gradual latency creep on $node/$iface..."
  for val in "10ms 1ms" "30ms 3ms" "60ms 8ms" "100ms 15ms" "180ms 25ms" "300ms 40ms"; do
    python3 "$INJECTOR" --node "$node" --interface "$iface" --fault latency --value "$val" 2>/dev/null || true
    sleep 12
  done
  clear_all
  sleep 15
}

# ── Cascade: both core links degraded ────────────────────────────────────────
cascade_core_fault() {
  echo "[CASCADE] Multi-node latency+loss..."
  python3 "$INJECTOR" --node pe1 --interface eth0 --fault latency --value "100ms 20ms" --duration 30 2>/dev/null &
  sleep 5
  python3 "$INJECTOR" --node p1  --interface eth0 --fault loss    --value "5%"         --duration 25 2>/dev/null &
  wait
  clear_all; sleep 20
}

# ── BGP flap cascade ─────────────────────────────────────────────────────────
bgp_flap_cascade() {
  echo "[FLAP] BGP oscillation on pe1..."
  for i in 1 2 3 4 5; do
    python3 "$INJECTOR" --node pe1 --interface eth0 --fault flap --value "" --duration 5 2>/dev/null || true
    sleep 10
  done
  clear_all; sleep 15
}

ITERATION=0
echo "╔══════════════════════════════════════════════════════════╗"
echo "║  Continuous Fault Loop — Project Aether Dataset Builder  ║"
echo "║  Ctrl+C to stop (data_collector will keep its file)      ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""

while true; do
  ITERATION=$(( ITERATION + 1 ))
  echo ""
  echo "══ Iteration ${ITERATION} — $(date '+%H:%M:%S') ══"

  healthy_window 25

  # Latency faults (varied)
  idx=$(( (RANDOM % ${#LATENCY_PARAMS[@]}) ))
  eval inject ${LATENCY_PARAMS[$idx]}
  sleep ${LATENCY_PARAMS[$idx]##* }
  healthy_window 18

  # Loss faults
  idx=$(( RANDOM % ${#LOSS_PARAMS[@]} ))
  eval inject ${LOSS_PARAMS[$idx]}
  sleep ${LOSS_PARAMS[$idx]##* }
  healthy_window 18

  # Gradual ramp every 3rd iteration
  if (( ITERATION % 3 == 0 )); then
    healthy_window 15
    gradual_latency_ramp pe1 eth0
  fi

  # Corrupt
  idx=$(( RANDOM % ${#CORRUPT_PARAMS[@]} ))
  eval inject ${CORRUPT_PARAMS[$idx]}
  sleep ${CORRUPT_PARAMS[$idx]##* }
  healthy_window 18

  # Rate throttle
  idx=$(( RANDOM % ${#RATE_PARAMS[@]} ))
  eval inject ${RATE_PARAMS[$idx]}
  sleep ${RATE_PARAMS[$idx]##* }
  healthy_window 18

  # Flap
  idx=$(( RANDOM % ${#FLAP_PARAMS[@]} ))
  eval inject ${FLAP_PARAMS[$idx]}
  sleep ${FLAP_PARAMS[$idx]##* }

  # Cascade every 4th iteration
  if (( ITERATION % 4 == 0 )); then
    healthy_window 15
    cascade_core_fault
  fi

  # BGP flap cascade every 5th iteration
  if (( ITERATION % 5 == 0 )); then
    healthy_window 15
    bgp_flap_cascade
  fi

  healthy_window 20

done
