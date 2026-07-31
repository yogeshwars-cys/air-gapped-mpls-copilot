import os
import copy
import yaml
import networkx as nx

# =============================================================================
# graph_model.py — Graph-Analytical Engine & Clonal State-Space Search
#
# Simulates the network topology analytically using NetworkX. Evaluates
# projected traffic matrices against routing permutations ("clones") to
# detect queue saturation and select optimal SLA-compliant paths.
# Per-service SLA thresholds are loaded from config/sla_config.yaml.
# =============================================================================

_SLA_CONFIG_PATH = os.path.join(
    os.path.dirname(__file__), "..", "config", "sla_config.yaml"
)


def _load_sla_config():
    try:
        with open(_SLA_CONFIG_PATH) as f:
            cfg = yaml.safe_load(f)
        return cfg.get("services", {}), cfg.get("default", {"latency_ms_max": 200})
    except FileNotFoundError:
        return {}, {"latency_ms_max": 200}


# Traffic flow → service class mapping (source, dest) → service tag
_FLOW_SERVICE_MAP = {
    ("ce-branch1", "ce-dc"):   "voip",          # VoIP + DB replication
    ("ce-branch2", "ce-hub"):  "bulk_transfer",  # VTC video
    ("ce-branch2", "ce-dc"):   "database",       # SSH / admin
}


class ClonalGraphEngine:
    def __init__(self):
        self.sla_services, self.sla_default = _load_sla_config()

        # Create base physical topology
        self.base_graph = nx.DiGraph()
        
        # Add nodes with roles
        nodes_info = {
            "pe1": {"role": "provider_edge"},
            "p1": {"role": "provider_core"},
            "pe2": {"role": "provider_edge"},
            "ce-branch1": {"role": "customer_edge"},
            "ce-hub": {"role": "customer_edge"},
            "ce-branch2": {"role": "customer_edge"},
            "ce-dc": {"role": "customer_edge"}
        }
        for node, attr in nodes_info.items():
            self.base_graph.add_node(node, **attr)
            
        # Add bidirectional links with capacity (bps), current delay (ms), and cost
        # We model the primary MPLS core links and access links.
        # We also model the SD-WAN overlay as a high-cost backup edge between PE1
        # and PE2. This is physically backed by the GRE overlay tunnel built in
        # topology/overlay-setup.sh (gre-sdwan, 172.16.99.0/30), which rides over
        # the OSPF-routed core PE1→P1→PE2 — there is no direct PE1↔PE2 wire.
        links = [
            # Core primary paths
            ("pe1", "p1", {"capacity": 10_000_000, "delay": 5, "cost": 10, "is_backup": False}),
            ("p1", "pe1", {"capacity": 10_000_000, "delay": 5, "cost": 10, "is_backup": False}),
            ("p1", "pe2", {"capacity": 10_000_000, "delay": 5, "cost": 10, "is_backup": False}),
            ("pe2", "p1", {"capacity": 10_000_000, "delay": 5, "cost": 10, "is_backup": False}),
            
            # Access links
            ("pe1", "ce-branch1", {"capacity": 5_000_000, "delay": 2, "cost": 5, "is_backup": False}),
            ("ce-branch1", "pe1", {"capacity": 5_000_000, "delay": 2, "cost": 5, "is_backup": False}),
            
            ("pe1", "ce-hub", {"capacity": 5_000_000, "delay": 2, "cost": 5, "is_backup": False}),
            ("ce-hub", "pe1", {"capacity": 5_000_000, "delay": 2, "cost": 5, "is_backup": False}),
            
            ("pe2", "ce-branch2", {"capacity": 5_000_000, "delay": 2, "cost": 5, "is_backup": False}),
            ("ce-branch2", "pe2", {"capacity": 5_000_000, "delay": 2, "cost": 5, "is_backup": False}),
            
            ("pe2", "ce-dc", {"capacity": 5_000_000, "delay": 2, "cost": 5, "is_backup": False}),
            ("ce-dc", "pe2", {"capacity": 5_000_000, "delay": 2, "cost": 5, "is_backup": False}),
            
            # SD-WAN Backup Tunnel Overlay (deactive/high-cost by default)
            ("pe1", "pe2", {"capacity": 2_000_000, "delay": 40, "cost": 100, "is_backup": True}),
            ("pe2", "pe1", {"capacity": 2_000_000, "delay": 40, "cost": 100, "is_backup": True})
        ]
        
        for u, v, attr in links:
            self.base_graph.add_edge(u, v, **attr)

    def apply_telemetry_state(self, telemetry_updates):
        """
        Updates the graph link parameters (delays, capacities) from live metrics.
        telemetry_updates format: {("u", "v"): {"delay": x, "capacity": y, "loss": z}}
        """
        for (u, v), metrics in telemetry_updates.items():
            if self.base_graph.has_edge(u, v):
                for k, val in metrics.items():
                    self.base_graph[u][v][k] = val

    def get_traffic_matrix(self):
        """
        Returns the baseline traffic demand matrix based on the running traffic profiles.
        Format: {(source, destination): throughput_bps}
        """
        return {
            ("ce-branch1", "ce-dc"): 4_500_000,  # 150k VoIP + 4M db-replication + HTTP bursts
            ("ce-branch2", "ce-hub"): 1_500_000, # 1.5M VTC video stream
            ("ce-branch2", "ce-dc"): 50_000      # 50k SSH administrative
        }

    def generate_routing_clones(self, degraded_link=None):
        """
        Creates 'clones' of the graph topology representing different routing states:
          1. Default state (Primary paths active).
          2. Rerouted state (Backup tunnel active, primary disabled/avoided).
          3. QoS Shaded state (Primary path active but low-priority DB backup rate throttled by 50%).
        """
        clones = {}
        
        # Clone 1: Baseline (Unmodified)
        clones["CLONE_BASELINE"] = copy.deepcopy(self.base_graph)
        
        # Clone 2: Rerouted (Avoid the degraded link by spiking its routing cost to infinity)
        if degraded_link:
            u, v = degraded_link
            clone_reroute = copy.deepcopy(self.base_graph)
            if clone_reroute.has_edge(u, v):
                clone_reroute[u][v]["cost"] = 999999 # Render it unusable
                clone_reroute[u][v]["delay"] = 99999  
            clones["CLONE_REROUTED_OVERLAY"] = clone_reroute
            
        # Clone 3: QoS Throttling (Simulates policy engine applying bandwidth limiters on databases)
        clone_qos = copy.deepcopy(self.base_graph)
        clones["CLONE_QOS_THROTTLED"] = clone_qos
        
        return clones

    def evaluate_clone(self, G, traffic_matrix, qos_enabled=False):
        """
        Simulates routing traffic over the clone. Computes link loads, 
        detects saturation, and scores the clone based on SLA violations.
        """
        # Initialize link loads
        link_loads = {edge: 0.0 for edge in G.edges()}
        path_delays = {}
        sla_violations = 0
        total_delay = 0.0
        
        # For QoS clone, we throttle bulk database backups
        adjusted_traffic = copy.deepcopy(traffic_matrix)
        if qos_enabled:
            # Throttle DB backup (ce-branch1 -> ce-dc) from 4M to 1M
            if ("ce-branch1", "ce-dc") in adjusted_traffic:
                adjusted_traffic[("ce-branch1", "ce-dc")] = 1_150_000 # Keep VoIP, drop DB bulk
                
        # Route each traffic flow via shortest path based on cost
        for (src, dst), demand in adjusted_traffic.items():
            try:
                # Compute Dijkstra shortest path
                path = nx.shortest_path(G, source=src, target=dst, weight="cost")
                
                # Calculate path delay
                path_delay = sum(G[path[i]][path[i+1]]["delay"] for i in range(len(path)-1))
                path_delays[f"{src}->{dst}"] = path_delay
                total_delay += path_delay
                
                # Per-service SLA latency check
                svc_tag = _FLOW_SERVICE_MAP.get((src, dst), None)
                svc_sla = self.sla_services.get(svc_tag, self.sla_default) if svc_tag else self.sla_default
                lat_max = svc_sla.get("latency_ms_max", 200)
                priority = svc_sla.get("priority", 99)
                if path_delay > lat_max:
                    # Higher-priority services get proportionally larger penalty
                    sla_violations += (path_delay - lat_max) / lat_max * (10 / max(priority, 1))
                
                # Allocate load on edges along the path
                for i in range(len(path)-1):
                    u, v = path[i], path[i+1]
                    link_loads[(u, v)] += demand
                    
            except nx.NetworkXNoPath:
                # Infinite penalty for disconnected network
                sla_violations += 10
                path_delays[f"{src}->{dst}"] = float('inf')

        # Evaluate link saturation
        saturated_links = []
        for (u, v), load in link_loads.items():
            capacity = G[u][v]["capacity"]
            saturation_ratio = load / capacity
            if saturation_ratio > 1.0:
                # Over-saturation penalty
                sla_violations += (saturation_ratio - 1.0) * 5
                saturated_links.append({
                    "source": u,
                    "target": v,
                    "capacity_bps": capacity,
                    "projected_load_bps": load,
                    "saturation_ratio": saturation_ratio
                })

        score = sla_violations + (total_delay / 1000.0)
        return score, saturated_links, path_delays

    def get_tunnel_health(self) -> list[dict]:
        """
        Returns per-LSP tunnel health statistics derived from graph link state.
        Each record describes an MPLS LSP and its current health metrics.
        Used as synthetic tunnel telemetry when actual MPLS OAM is unavailable.
        """
        import random, math
        rng = random.Random(42 + int(sum(
            self.base_graph[u][v].get("delay", 0)
            for u, v in self.base_graph.edges()
        )))
        tunnels = [
            {"lsp_id": "LSP-PE1-PE2-PRIMARY",   "src": "pe1", "dst": "pe2", "transit": "p1",    "service": "voip",    "label_stack": [3001, 3002]},
            {"lsp_id": "LSP-PE2-PE1-PRIMARY",   "src": "pe2", "dst": "pe1", "transit": "p1",    "service": "voip",    "label_stack": [3002, 3001]},
            {"lsp_id": "LSP-PE1-PE2-DB",        "src": "pe1", "dst": "pe2", "transit": "p1",    "service": "database","label_stack": [3003, 3004]},
            {"lsp_id": "LSP-PE1-PE2-BULK",      "src": "pe1", "dst": "pe2", "transit": "p1",    "service": "bulk",    "label_stack": [3005, 3006]},
        ]
        health_records = []
        for t in tunnels:
            src, dst = t["src"], t["dst"]
            e = self.base_graph[src][t["transit"]] if self.base_graph.has_edge(src, t["transit"]) else {}
            delay    = e.get("delay", 5)
            capacity = e.get("capacity", 1_000_000_000)
            loss     = e.get("loss", 0)

            # Derive health score: 1.0 = perfect, 0.0 = completely degraded
            delay_penalty = min(1.0, delay / 500.0)
            loss_penalty  = min(1.0, loss  / 100.0)
            health_score  = round(max(0.0, 1.0 - delay_penalty - loss_penalty * 0.5), 3)
            state = "UP" if health_score > 0.4 else ("DEGRADED" if health_score > 0.1 else "DOWN")

            health_records.append({
                "lsp_id":       t["lsp_id"],
                "src_node":     src,
                "dst_node":     dst,
                "transit":      t["transit"],
                "service":      t["service"],
                "label_stack":  t["label_stack"],
                "state":        state,
                "health_score": health_score,
                "delay_ms":     delay,
                "loss_pct":     loss,
                "capacity_bps": capacity,
                "bfd_state":    "UP" if state == "UP" else "DOWN",
            })
        return health_records

    def run_clonal_search(self, degraded_link=None):
        """
        Executes clonal state search. Runs all clones and returns the winning
        permutation details.
        """
        traffic = self.get_traffic_matrix()
        clones = self.generate_routing_clones(degraded_link)
        results = {}
        
        for name, G in clones.items():
            qos = (name == "CLONE_QOS_THROTTLED")
            score, bottlenecks, delays = self.evaluate_clone(G, traffic, qos_enabled=qos)
            results[name] = {
                "score": score,
                "bottlenecks": bottlenecks,
                "delays": delays
            }
            
        # Select clone with minimum score (lowest SLA violations)
        best_clone = min(results, key=lambda k: results[k]["score"])
        
        return best_clone, results

if __name__ == "__main__":
    engine = ClonalGraphEngine()
    
    # Simulate a degraded link in the primary core (pe1 -> p1 spikes to 200ms delay and drops capacity)
    telemetry_updates = {
        ("pe1", "p1"): {"delay": 200, "capacity": 2_000_000, "loss": 15}
    }
    engine.apply_telemetry_state(telemetry_updates)
    
    # Run clonal search
    winner, all_results = engine.run_clonal_search(degraded_link=("pe1", "p1"))
    print(f"[*] Clonal search winner: {winner}")
    for name, res in all_results.items():
        print(f"    Clone: {name} | Score: {res['score']:.2f} | Delays: {res['delays']} | Saturated Links: {len(res['bottlenecks'])}")
