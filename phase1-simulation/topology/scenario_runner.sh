#!/usr/bin/env bash
# =============================================================================
# scenario_runner.sh — Automated Validation Scenario Execution
#
# Runs the 4 evaluation scenarios from the ISRO hackathon problem statement:
#   Scenario 1: Progressive congestion buildup on a hub-spoke link
#   Scenario 2: BGP route flap with downstream path reroute cascade
#   Scenario 3: Intermittent MPLS underlay failure with tunnel degradation
#   Scenario 4: Controller misconfiguration leading to policy drift
#
# Prerequisites:
#   - Chunk3 lab deployed and configured (./chunk3-setup.sh)
#   - Traffic generator running (./traffic_generator.sh)
#   - Telemetry exporter running (python3 exporter.py)
#   - Data collector running (python3 data_collector.py --duration 600)
#
# Usage:
#   ./scenario_runner.sh [scenario_number]
#   ./scenario_runner.sh all     # Run all scenarios sequentially
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TOPOLOGY_DIR="${SCRIPT_DIR}"
INJECTOR="${TOPOLOGY_DIR}/fault_injector.py"
LAB="${LAB:-aether}"

P() { echo "clab-${LAB}-$1"; }

scenario_1() {
    echo ""
    echo "╔══════════════════════════════════════════════════════════════╗"
    echo "║  SCENARIO 1: Progressive Congestion Buildup (Hub-Spoke)    ║"
    echo "║  Expected: ML predicts saturation ~30s before SLA breach   ║"
    echo "╚══════════════════════════════════════════════════════════════╝"
    echo ""

    echo "[Phase 1] Baseline: 30s of clean traffic..."
    sleep 30

    echo "[Phase 2] Mild congestion: 50ms delay + 2% loss on pe1-p1 for 30s..."
    python3 "$INJECTOR" --node pe1 --interface eth1 --fault latency --value "50ms 5ms" --duration 30 &
    PID1=$!

    echo "[Phase 3] Moderate congestion: 100ms delay + 5% loss for 30s..."
    wait $PID1 2>/dev/null || true
    python3 "$INJECTOR" --node pe1 --interface eth1 --fault latency --value "100ms 15ms" --duration 30 &
    PID2=$!

    echo "[Phase 4] Severe congestion: 200ms delay + 10% loss for 30s..."
    wait $PID2 2>/dev/null || true
    python3 "$INJECTOR" --node pe1 --interface eth1 --fault loss --value "10%" --duration 30 &
    PID3=$!

    echo "[Phase 5] Recovery: clearing all faults..."
    wait $PID3 2>/dev/null || true
    python3 "$INJECTOR" --node pe1 --interface eth1 --fault none

    echo "[Phase 6] Post-recovery monitoring: 30s..."
    sleep 30
    echo "[✓] Scenario 1 complete. Check data_collector output for labeled transitions."
}

scenario_2() {
    echo ""
    echo "╔══════════════════════════════════════════════════════════════╗"
    echo "║  SCENARIO 2: BGP Route Flap with Reroute Cascade           ║"
    echo "║  Expected: ML detects control-plane instability pattern     ║"
    echo "╚══════════════════════════════════════════════════════════════╝"
    echo ""

    echo "[Phase 1] Baseline: 20s of stable routing..."
    sleep 20

    echo "[Phase 2] Injecting BGP flap: toggling pe1-ce-branch1 link 5 times..."
    for i in $(seq 1 5); do
        echo "    Flap #$i: link DOWN..."
        python3 "$INJECTOR" --node pe1 --interface eth2 --fault flap --value "" --duration 5 &
        wait $! 2>/dev/null || true
        echo "    Flap #$i: link UP, waiting for reconvergence..."
        sleep 8
    done

    echo "[Phase 3] Stabilizing: 20s recovery monitoring..."
    python3 "$INJECTOR" --node pe1 --interface eth2 --fault none
    sleep 20
    echo "[✓] Scenario 2 complete. BGP sessions should have re-established."
}

scenario_3() {
    echo ""
    echo "╔══════════════════════════════════════════════════════════════╗"
    echo "║  SCENARIO 3: Intermittent MPLS Underlay Failure            ║"
    echo "║  Expected: ML detects tunnel degradation before full loss   ║"
    echo "╚══════════════════════════════════════════════════════════════╝"
    echo ""

    echo "[Phase 1] Baseline: 20s of clean MPLS forwarding..."
    sleep 20

    echo "[Phase 2] Intermittent core packet corruption: 3% for 20s..."
    python3 "$INJECTOR" --node p1 --interface eth1 --fault corrupt --value "3%" --duration 20 &
    wait $! 2>/dev/null || true

    echo "[Phase 3] Core bandwidth throttle: 500kbit for 20s..."
    python3 "$INJECTOR" --node p1 --interface eth1 --fault rate --value "500kbit" --duration 20 &
    wait $! 2>/dev/null || true

    echo "[Phase 4] Core link flap (simulating fiber issue)..."
    python3 "$INJECTOR" --node p1 --interface eth1 --fault flap --value "" --duration 10 &
    wait $! 2>/dev/null || true

    echo "[Phase 5] Recovery..."
    python3 "$INJECTOR" --node p1 --interface eth1 --fault none
    sleep 20
    echo "[✓] Scenario 3 complete."
}

scenario_4() {
    echo ""
    echo "╔══════════════════════════════════════════════════════════════╗"
    echo "║  SCENARIO 4: Policy Drift / Misconfiguration               ║"
    echo "║  Expected: ML detects asymmetric routing / prefix anomaly   ║"
    echo "╚══════════════════════════════════════════════════════════════╝"
    echo ""

    echo "[Phase 1] Baseline: 20s of normal VPN operation..."
    sleep 20

    echo "[Phase 2] Injecting asymmetric degradation: latency on pe2→p1 only..."
    python3 "$INJECTOR" --node pe2 --interface eth1 --fault latency --value "150ms 20ms" --duration 30 &
    PID1=$!

    echo "[Phase 3] Simultaneously: packet loss on ce-dc access link..."
    python3 "$INJECTOR" --node ce-dc --interface eth1 --fault loss --value "8%" --duration 30 &
    PID2=$!

    wait $PID1 2>/dev/null || true
    wait $PID2 2>/dev/null || true

    echo "[Phase 4] Recovery..."
    python3 "$INJECTOR" --node pe2 --interface eth1 --fault none
    python3 "$INJECTOR" --node ce-dc --interface eth1 --fault none
    sleep 20
    echo "[✓] Scenario 4 complete."
}

# ── Main ────────────────────────────────────────────────────────────────
SCENARIO="${1:-all}"

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║     Project Aether — Validation Scenario Runner             ║"
echo "║     Bharatiya Antariksh Hackathon 2026 (ISRO)               ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
echo "  Lab: $LAB"
echo "  Injector: $INJECTOR"
echo "  Target: Scenario $SCENARIO"
echo ""

case "$SCENARIO" in
    1) scenario_1 ;;
    2) scenario_2 ;;
    3) scenario_3 ;;
    4) scenario_4 ;;
    all)
        scenario_1
        echo ""
        echo "━━━ Cooldown: 15s between scenarios ━━━"
        sleep 15
        scenario_2
        sleep 15
        scenario_3
        sleep 15
        scenario_4
        echo ""
        echo "╔══════════════════════════════════════════════════════════════╗"
        echo "║  ALL 4 SCENARIOS COMPLETE                                  ║"
        echo "║  Check dataset.csv for labeled training data               ║"
        echo "║  Run: python3 phase3-models/train_models.py                ║"
        echo "╚══════════════════════════════════════════════════════════════╝"
        ;;
    *)
        echo "Usage: $0 [1|2|3|4|all]"
        exit 1
        ;;
esac
