#!/usr/bin/env python3
# =============================================================================
# exporter.py — Multi-Node Prometheus Exporter for FRR & Container Interfaces
#
# This script runs on the HOST. It collects telemetry from all 7 simulation
# containers using 'docker exec' and parses interface statistics + FRR state.
# Exposes a Prometheus metrics endpoint on port 8000.
#
# Zero External Dependencies: Uses python's built-in http.server module.
# =============================================================================
import os
import subprocess
import json
import re
import sys
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """Handle each request in a separate thread so Prometheus scrapes don't block the collector."""
    daemon_threads = True

PORT = 8000
# Lab name comes from the LAB env var so the exporter targets the same
# Containerlab topology the rest of the stack uses (default: aether). Falls back
# to the legacy LAB_NAME var for compatibility.
LAB_NAME = os.environ.get("LAB", os.environ.get("LAB_NAME", "aether"))
NODES = ["pe1", "p1", "pe2", "ce-branch1", "ce-branch2", "ce-hub", "ce-dc"]

def get_container_name(node):
    return f"clab-{LAB_NAME}-{node}"

def exec_cmd(container, cmd):
    try:
        res = subprocess.run(
            f"docker exec {container} {cmd}",
            shell=True,
            capture_output=True,
            text=True,
            timeout=2
        )
        if res.returncode == 0:
            return res.stdout
        return ""
    except Exception:
        return ""

def collect_interface_metrics(node, container):
    """
    Parses /proc/net/dev inside the container to get interface statistics.
    """
    metrics = []
    output = exec_cmd(container, "cat /proc/net/dev")
    if not output:
        return metrics

    # Format of /proc/net/dev lines:
    # face |bytes packets errs drop fifo frame compressed multicast|bytes packets errs drop fifo colls carrier compressed
    lines = output.split('\n')
    for line in lines:
        if ':' not in line:
            continue
        parts = line.split(':')
        iface = parts[0].strip()
        # Skip loopback
        if iface == "lo":
            continue
            
        stats = parts[1].split()
        if len(stats) >= 16:
            rx_bytes = stats[0]
            rx_packets = stats[1]
            rx_errors = stats[2]
            rx_drops = stats[3]
            tx_bytes = stats[8]
            tx_packets = stats[9]
            tx_errors = stats[10]
            tx_drops = stats[11]
            
            metrics.extend([
                f'net_rx_bytes{{node="{node}",interface="{iface}"}} {rx_bytes}',
                f'net_rx_packets{{node="{node}",interface="{iface}"}} {rx_packets}',
                f'net_rx_errors{{node="{node}",interface="{iface}"}} {rx_errors}',
                f'net_rx_drops{{node="{node}",interface="{iface}"}} {rx_drops}',
                f'net_tx_bytes{{node="{node}",interface="{iface}"}} {tx_bytes}',
                f'net_tx_packets{{node="{node}",interface="{iface}"}} {tx_packets}',
                f'net_tx_errors{{node="{node}",interface="{iface}"}} {tx_errors}',
                f'net_tx_drops{{node="{node}",interface="{iface}"}} {tx_drops}'
            ])
    return metrics

def collect_latency_jitter(node, container, peer_ip: str) -> list:
    """
    Measures RTT and jitter to a peer using ping from inside the container.
    Jitter = standard deviation of inter-packet latency (mean of |rtt[i] - rtt[i-1]|).
    Returns Prometheus metric lines.
    """
    metrics = []
    # Send 10 small ICMP packets; parse rtt min/avg/max/mdev from ping output
    output = exec_cmd(container, f"ping -c 10 -i 0.2 -W 1 -q {peer_ip}")
    if not output:
        return metrics
    import re
    # "rtt min/avg/max/mdev = 0.123/0.456/0.789/0.111 ms"
    m = re.search(r"rtt min/avg/max/mdev = ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)\s+ms", output)
    if m:
        rtt_min, rtt_avg, rtt_max, rtt_mdev = (float(x) for x in m.groups())
        metrics.extend([
            f'link_rtt_ms_avg{{node="{node}",peer="{peer_ip}"}} {rtt_avg:.3f}',
            f'link_rtt_ms_min{{node="{node}",peer="{peer_ip}"}} {rtt_min:.3f}',
            f'link_rtt_ms_max{{node="{node}",peer="{peer_ip}"}} {rtt_max:.3f}',
            f'link_jitter_ms{{node="{node}",peer="{peer_ip}"}} {rtt_mdev:.3f}',
        ])
    return metrics


# Management-plane IP of each peer reachable from each node (eth0 subnet)
_LATENCY_PEERS = {
    "pe1": ["172.20.20.3", "172.20.20.4"],  # pe1 → pe2, p1
    "pe2": ["172.20.20.2", "172.20.20.4"],  # pe2 → pe1, p1
    "p1":  ["172.20.20.2", "172.20.20.3"],  # p1  → pe1, pe2
}


def collect_frr_metrics(node, container):
    """
    Queries vtysh inside FRR nodes to get routing and neighbor details in JSON.
    """
    metrics = []
    
    # 1. OSPF Neighbors
    ospf_json_str = exec_cmd(container, "vtysh -c 'show ip ospf neighbor json'")
    if ospf_json_str:
        try:
            ospf_data = json.loads(ospf_json_str)
            # FRR JSON structure can vary slightly; handle dictionary or list of neighbors
            neighbors = ospf_data.get("neighbors", {})
            total_neighbors = len(neighbors)
            metrics.append(f'frr_ospf_neighbors_total{{node="{node}"}} {total_neighbors}')

            # FRR OSPF JSON: each key maps to a list of adjacency objects
            for nbr_ip, nbr_entries in neighbors.items():
                entries = nbr_entries if isinstance(nbr_entries, list) else [nbr_entries]
                for nbr_info in entries:
                    state = nbr_info.get("state", nbr_info.get("nbrState", ""))
                    is_full = 1 if "Full" in state else 0
                    metrics.append(f'frr_ospf_neighbor_full{{node="{node}",neighbor="{nbr_ip}"}} {is_full}')
        except json.JSONDecodeError:
            pass

    # 2. BGP VPNv4 Summary (Only for PEs: pe1, pe2)
    if node in ["pe1", "pe2"]:
        bgp_json_str = exec_cmd(container, "vtysh -c 'show bgp ipv4 vpn summary json'")
        if bgp_json_str:
            try:
                bgp_data = json.loads(bgp_json_str)
                peers = bgp_data.get("peers", {})
                for peer_ip, peer_info in peers.items():
                    state = peer_info.get("state", "")
                    is_est = 1 if state.lower() == "established" else 0
                    pfx_rcd = peer_info.get("pfxRcd", 0)
                    metrics.extend([
                        f'frr_bgp_vpn_established{{node="{node}",peer="{peer_ip}"}} {is_est}',
                        f'frr_bgp_vpn_prefixes_received{{node="{node}",peer="{peer_ip}"}} {pfx_rcd}'
                    ])
            except json.JSONDecodeError:
                pass

        # 3. VRF BGP Summary (PE-CE Peerings)
        bgp_vrf_json = exec_cmd(container, "vtysh -c 'show bgp vrf CUST ipv4 unicast summary json'")
        if bgp_vrf_json:
            try:
                bgp_vrf_data = json.loads(bgp_vrf_json)
                peers = bgp_vrf_data.get("peers", {})
                for peer_ip, peer_info in peers.items():
                    state = peer_info.get("state", "")
                    is_est = 1 if state.lower() == "established" else 0
                    pfx_rcd = peer_info.get("pfxRcd", 0)
                    metrics.extend([
                        f'frr_bgp_vrf_established{{node="{node}",vrf="CUST",peer="{peer_ip}"}} {is_est}',
                        f'frr_bgp_vrf_prefixes_received{{node="{node}",vrf="CUST",peer="{peer_ip}"}} {pfx_rcd}'
                    ])
            except json.JSONDecodeError:
                pass

    # 4. LDP Status
    ldp_json_str = exec_cmd(container, "vtysh -c 'show mpls ldp neighbor json'")
    if ldp_json_str:
        try:
            ldp_data = json.loads(ldp_json_str)
            # FRR LDP JSON: {"neighbors": [{neighborId, state, ...}]}
            neighbors = ldp_data if isinstance(ldp_data, list) else ldp_data.get("neighbors", [])
            metrics.append(f'frr_ldp_neighbors_total{{node="{node}"}} {len(neighbors)}')
            for nbr in neighbors:
                # FRR uses "neighborId" (not "peerId") and "state" (not "connectionState")
                peer_id = nbr.get("neighborId", nbr.get("peerId", "unknown"))
                state = nbr.get("state", nbr.get("connectionState", ""))
                is_oper = 1 if "OPERATIONAL" in state.upper() else 0
                metrics.append(f'frr_ldp_session_operational{{node="{node}",peer="{peer_id}"}} {is_oper}')
        except json.JSONDecodeError:
            pass

    return metrics

def collect_all() -> str:
    """Scrape every node once and render the Prometheus exposition text.
    Slow (sequential docker exec + in-container ping), so it runs in a background
    thread and the result is cached — see _collector_loop()."""
    # Scrape timestamp first: rate consumers must delta over THIS value, not
    # their own poll interval — the cache serves identical counters for
    # ~REFRESH_S + scrape_time, so wall-clock deltas between polls read 0.
    output_metrics = [f'exporter_scrape_ts {time.time():.3f}']
    for node in NODES:
        container = get_container_name(node)
        # Verify container is running
        status = exec_cmd(container, "echo running")
        if not status:
            output_metrics.append(f'container_running{{node="{node}"}} 0')
            continue

        output_metrics.append(f'container_running{{node="{node}"}} 1')

        # Collect Interface telemetry
        output_metrics.extend(collect_interface_metrics(node, container))

        # Collect Routing telemetry (only core routers run OSPF/LDP/BGP)
        if node in ["pe1", "p1", "pe2"]:
            output_metrics.extend(collect_frr_metrics(node, container))

        # RTT/jitter measurement (core routers only — CE nodes have no reachable peer IPs)
        for peer_ip in _LATENCY_PEERS.get(node, []):
            output_metrics.extend(collect_latency_jitter(node, container, peer_ip))

    return "\n".join(output_metrics) + "\n"


# Cached exposition text, refreshed continuously by a background thread so that
# /metrics always answers in <10 ms (a full scrape takes ~10 s of docker exec).
_CACHE = {"text": "# telemetry warming up\n", "ts": 0.0}
REFRESH_S = 3.0  # min gap between full scrapes (actual cadence ≈ scrape time)

def _collector_loop():
    import time
    while True:
        try:
            text = collect_all()
            _CACHE["text"] = text
            _CACHE["ts"] = time.time()
        except Exception as e:
            sys.stderr.write(f"[exporter] collect error: {e}\n")
        time.sleep(REFRESH_S)


class MetricsHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # suppress per-request stdout noise at 1 s scrape rate

    def do_GET(self):
        if self.path == '/metrics':
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; version=0.0.4; charset=utf-8')
            self.end_headers()
            self.wfile.write(_CACHE["text"].encode('utf-8'))   # instant: serve cache
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not Found")

def run():
    import threading
    print(f"[*] Starting Air-Gapped Telemetry Exporter on port {PORT} (lab={LAB_NAME})...", flush=True)
    t = threading.Thread(target=_collector_loop, daemon=True)
    t.start()
    server = ThreadedHTTPServer(('0.0.0.0', PORT), MetricsHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[*] Shutting down telemetry exporter.")
        server.server_close()

if __name__ == "__main__":
    run()
