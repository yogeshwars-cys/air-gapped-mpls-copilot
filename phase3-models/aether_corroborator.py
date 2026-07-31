#!/usr/bin/env python3
# =============================================================================
# aether_corroborator.py — Core Prediction Corroborator & Edge Policy Engine (EPE)
#
# Enforces the safety protocol of Project Aether:
#   - Stochastic prediction (LSTM) must corroborate with Deterministic projection (Graph).
#   - Confirms the Autonomy Policy Matrix before executing mitigation commands.
#   - Generates the Anomaly Context Packet (ACP) with exact router mitigation CLI commands.
# =============================================================================
import os
import sys
from acp_manager import AnomalyContextPacket
from graph_model import ClonalGraphEngine
from taxonomy import DISPLAY_TO_INFO, policy_for_action

# Containerlab lab name the generated mitigation commands target — must match
# the deployed topology (run.sh exports LAB=aether). Same convention as
# phase2-telemetry/exporter.py and phase1 fault_injector.py.
LAB_NAME = os.environ.get("LAB", os.environ.get("LAB_NAME", "aether"))

# Digital twin divergence is a PSF (Predictive Safety Factor) — it widens
# the rationale string for operator visibility but never gates auto-execute.
try:
    from digital_twin import DigitalTwin
    from trend_forecaster import TrendForecaster
    HAS_TWIN = True
except ImportError:
    HAS_TWIN = False

# The operator-configurable autonomy matrix lives in taxonomy.ACTION_POLICY so
# the corroborator, the live inference engine and the ACP layer all share one
# definition. Use policy_for_action(action_class) to read it.


class AetherCorroborator:
    def __init__(self):
        self.graph_engine = ClonalGraphEngine()
        
    def generate_mitigation_commands(self, winner_clone, target_node="pe1", target_iface="eth2"):
        """
        Generates the exact vtysh/tc network configuration commands to execute the mitigation.
        """
        if winner_clone == "CLONE_REROUTED_OVERLAY":
            return [
                f"# mitigation: Reroute target traffic to backup SD-WAN tunnel overlay",
                f"docker exec clab-{LAB_NAME}-{target_node} vtysh -c '",
                f"  configure terminal",
                f"  router bgp 65000 vrf CUST",
                f"   neighbor 10.1.11.2 route-map PREPEND_OUT out",
                f"  exit",
                f"  route-map PREPEND_OUT permit 10",
                f"   set as-path prepend 65000 65000",
                f"  exit",
                f"  end'",
                f"# Verification:",
                f"docker exec clab-{LAB_NAME}-{target_node} vtysh -c 'show bgp vrf CUST ipv4 unicast summary'"
            ]
        elif winner_clone == "CLONE_QOS_THROTTLED":
            return [
                f"# mitigation: Apply Traffic Control (tc) Token Bucket Filter rate limit to non-critical bulk transfer ports",
                f"# Limits bulk transfer (port 5002) to 1Mbps, leaving priority VoIP (port 5001) untouched",
                f"docker exec clab-{LAB_NAME}-{target_node} tc qdisc add dev {target_iface} root handle 1: htb default 10",
                f"docker exec clab-{LAB_NAME}-{target_node} tc class add dev {target_iface} parent 1: classid 1:1 htb rate 5mbit",
                f"docker exec clab-{LAB_NAME}-{target_node} tc class add dev {target_iface} parent 1:1 classid 1:10 htb rate 4mbit ceil 5mbit prio 1",
                f"docker exec clab-{LAB_NAME}-{target_node} tc class add dev {target_iface} parent 1:1 classid 1:20 htb rate 1mbit ceil 1mbit prio 2",
                f"docker exec clab-{LAB_NAME}-{target_node} tc filter add dev {target_iface} protocol ip parent 1:0 prio 1 u32 match ip dport 5002 0xffff flowid 1:20"
            ]
        else:
            return ["# No mitigation commands generated - Baseline state optimal."]

    def corroborate(self, telemetry_updates, ml_fault_class, ml_confidence, ml_ttf,
                    digital_twin_divergence: float | None = None):
        """
        Executes Dual-Model Corroboration loop.
        Combines stochastic LSTM classifier output with deterministic NetworkX simulations.
        """
        acp = AnomalyContextPacket(trigger_source="ML_ENGINE")
        
        # 1. Apply metrics to graph model
        self.graph_engine.apply_telemetry_state(telemetry_updates)
        
        # Extract node and link telemetry snapshot for the ACP
        nodes_snapshot = {}
        for (u, v), metrics in telemetry_updates.items():
            if u not in nodes_snapshot:
                nodes_snapshot[u] = {"interfaces": {}}
            nodes_snapshot[u]["interfaces"][v] = {
                "rx_bytes_sec": 0,
                "tx_bytes_sec": metrics.get("capacity", 10_000_000) * 0.85, # mock load
                "rx_drops": metrics.get("loss", 0),
                "tx_drops": 0,
                "rx_errors": 0,
                "tx_errors": 0
            }
        acp.set_telemetry(nodes_snapshot)

        # 2. LSTM Stochastic Prediction input
        ml_detected = ml_fault_class != "Healthy"
        acp.set_ml_results(
            detected=ml_detected,
            loss=0.04 if ml_detected else 0.0,
            fault_class=ml_fault_class,
            confidence=ml_confidence,
            ttf=ml_ttf
        )
        
        # 3. Graph-Analytical Deterministic Search
        degraded_link = list(telemetry_updates.keys())[0] if telemetry_updates else None
        winner_clone, all_results = self.graph_engine.run_clonal_search(degraded_link)
        
        # Populate graph results in ACP
        bottlenecks = all_results[winner_clone]["bottlenecks"]
        impacted_paths = list(all_results[winner_clone]["delays"].keys())
        acp.set_graph_results(bottlenecks, impacted_paths)
        
        # 4. Enforce Corroboration and Policy Gate
        baseline_delays = all_results["CLONE_BASELINE"]["delays"]
        graph_detects_bottleneck = (
            len(all_results["CLONE_BASELINE"]["bottlenecks"]) > 0
            or (bool(baseline_delays) and max(baseline_delays.values()) > 50)
        )
        
        engines_agree = ml_detected == graph_detects_bottleneck
        
        # Map fault class to its autonomy action category via the shared taxonomy.
        info = DISPLAY_TO_INFO.get(ml_fault_class, {"action": "NO_ACTION"})
        action_class = info["action"]
        recommended_action = action_class if ml_detected else "NO_ACTION"

        # Default safety mode: Recommend Only
        execution_mode = "RECOMMEND_ONLY"
        rationale = ""
        
        if not ml_detected:
            rationale = "System healthy. ML and Graph engines agree."
            recommended_action = "NO_ACTION"
        elif not engines_agree:
            rationale = "SAFETY TRIPPED: Disagreement. Stochastic model predicts fault but Deterministic graph model projects zero queue/SLA violations."
            recommended_action = f"OPERATOR_INVESTIGATE ({ml_fault_class})"
        else:
            # Engines Agree anomaly is active. Check Autonomy Matrix
            policy = policy_for_action(action_class)

            if ml_confidence >= policy["min_conf"]:
                if policy["auto_execute"]:
                    execution_mode = "AUTO_EXECUTE"
                    rationale = f"ML Engine ({ml_fault_class}, conf={ml_confidence}) and Graph Engine corroborate queue saturation. Autonomy policy authorizes autonomous resolution."
                else:
                    rationale = f"Models corroborate, but Autonomy policy forces verification for {action_class} (Auto-execute disabled)."
            else:
                rationale = f"Models corroborate, but ML confidence ({ml_confidence:.2f}) is below the required autonomy threshold ({policy['min_conf']:.2f})."
                
        # Append digital twin PSF to rationale (observability — never blocks action)
        if digital_twin_divergence is not None:
            twin_flag = " ⚠ TWIN HIGH" if digital_twin_divergence > 0.3 else ""
            rationale += (f" | Digital twin divergence: {digital_twin_divergence:.3f}{twin_flag}")
            acp.set_digital_twin_divergence(digital_twin_divergence)

        # Generate the commands and append them to the corroboration packet
        mitigation_cmds = self.generate_mitigation_commands(winner_clone)
        acp.set_corroboration(engines_agree, rationale, recommended_action, execution_mode)
        acp.mitigation_commands = mitigation_cmds
        
        return acp

if __name__ == "__main__":
    corroborator = AetherCorroborator()
    
    # -------------------------------------------------------------------------
    # TEST SCENARIO 1: Models Corroborate & Policy Authorizes Action
    # -------------------------------------------------------------------------
    print("=== Scenario 1: Core Link Saturation (ML and Graph Agree) ===")
    # Degrade pe1->p1 on the ce-branch1->ce-dc forwarding path: drop its capacity
    # below the 4.5 Mbps demand so the graph genuinely projects saturation and
    # corroborates the ML congestion prediction.
    telemetry_scenario_1 = {
        ("pe1", "p1"): {"delay": 60, "capacity": 4_000_000, "loss": 8}
    }
    
    acp1 = corroborator.corroborate(
        telemetry_updates=telemetry_scenario_1,
        ml_fault_class="Congestion / Saturation",
        ml_confidence=0.88,
        ml_ttf=45.0
    )
    
    print(f"[*] Rationale: {acp1.corroboration['rationale']}")
    print(f"[*] Recommended Action: {acp1.corroboration['recommended_action']}")
    print(f"[*] Execution Mode: {acp1.corroboration['execution_mode']}")
    print("[*] Generated Mitigation Commands:")
    for cmd in acp1.mitigation_commands:
        print(f"    {cmd}")
        
    print("\n" + "="*80 + "\n")
    
    # -------------------------------------------------------------------------
    # TEST SCENARIO 2: Safety Floor Triggered (Models Disagree)
    # -------------------------------------------------------------------------
    print("=== Scenario 2: Safety Trip (ML Predicts Anomaly, Graph Projects Healthy) ===")
    telemetry_scenario_2 = {
        ("pe1", "p1"): {"delay": 5, "capacity": 10_000_000, "loss": 0} # Physically clean
    }
    
    acp2 = corroborator.corroborate(
        telemetry_updates=telemetry_scenario_2,
        ml_fault_class="Packet Loss",
        ml_confidence=0.92,
        ml_ttf=120.0
    )
    
    print(f"[*] Rationale: {acp2.corroboration['rationale']}")
    print(f"[*] Recommended Action: {acp2.corroboration['recommended_action']}")
    print(f"[*] Execution Mode: {acp2.corroboration['execution_mode']}")
