#!/usr/bin/env python3
"""
netflow_simulator.py — Synthetic NetFlow/IPFIX record generator for Project Aether.

Generates realistic Layer-3 flow records between the 7-node MPLS topology nodes,
including MPLS-encapsulated VPN flows (VRF CUST) and infrastructure flows.
Records are exposed as a JSON endpoint that the inference engine and dashboard
can poll for flow-level telemetry — no actual hardware IPFIX export needed.

Usage:
    python3 netflow_simulator.py            # start HTTP server on port 9995
    python3 netflow_simulator.py --dump     # print one batch of flow records and exit
    python3 netflow_simulator.py --port N   # alternate port
"""
import argparse
import json
import math
import random
import time
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timezone

# --- Topology -------------------------------------------------------------------

# L3 VPN customer flows (VRF CUST): CE to CE
CE_FLOWS = [
    {"src_prefix": "10.10.1.0/24", "dst_prefix": "10.10.2.0/24",  "service": "voip",     "pe_ingress": "pe1", "pe_egress": "pe2"},
    {"src_prefix": "10.10.2.0/24", "dst_prefix": "10.10.1.0/24",  "service": "voip",     "pe_ingress": "pe2", "pe_egress": "pe1"},
    {"src_prefix": "10.10.1.0/24", "dst_prefix": "10.10.3.0/24",  "service": "database",  "pe_ingress": "pe1", "pe_egress": "pe2"},
    {"src_prefix": "10.10.3.0/24", "dst_prefix": "10.10.1.0/24",  "service": "database",  "pe_ingress": "pe2", "pe_egress": "pe1"},
    {"src_prefix": "10.10.4.0/24", "dst_prefix": "10.10.1.0/24",  "service": "bulk",      "pe_ingress": "pe2", "pe_egress": "pe1"},
    {"src_prefix": "10.10.1.0/24", "dst_prefix": "10.10.4.0/24",  "service": "bulk",      "pe_ingress": "pe1", "pe_egress": "pe2"},
]

# Infrastructure flows: OSPF hellos, BGP keepalives, LDP
INFRA_FLOWS = [
    {"src": "172.20.20.2", "dst": "172.20.20.4", "proto": 89,  "label": "ospf_hello",   "node": "pe1"},
    {"src": "172.20.20.4", "dst": "172.20.20.3", "proto": 89,  "label": "ospf_hello",   "node": "p1"},
    {"src": "172.20.20.2", "dst": "172.20.20.3", "proto": 6,   "label": "bgp_keepalive","node": "pe1"},
    {"src": "172.20.20.2", "dst": "172.20.20.4", "proto": 17,  "label": "ldp_hello",    "node": "pe1"},
    {"src": "172.20.20.4", "dst": "172.20.20.3", "proto": 17,  "label": "ldp_hello",    "node": "p1"},
]

# Service port mapping
_SVC_PORTS = {"voip": 5060, "database": 5432, "bulk": 443, "infra": 0}

# --- Flow state -----------------------------------------------------------------

class FlowSimulator:
    """Maintains per-flow rate state with optional fault injection."""

    def __init__(self):
        self._rng = random.Random()
        self._lock = threading.Lock()
        self._fault_class: str | None = None   # injected fault type
        self._fault_flow: str | None = None    # which flow is affected
        self._sequence = 0

    def inject_fault(self, fault_class: str, flow_key: str | None = None):
        with self._lock:
            self._fault_class = fault_class
            self._fault_flow = flow_key

    def clear_fault(self):
        with self._lock:
            self._fault_class = None
            self._fault_flow = None

    def _base_rate_bps(self, service: str) -> float:
        """Baseline throughput per service type."""
        base = {"voip": 1_500_000, "database": 8_000_000, "bulk": 25_000_000}.get(service, 500_000)
        # Diurnal: mild sine wave (peak at midday)
        hour = datetime.now(timezone.utc).hour
        diurnal = 1.0 + 0.35 * math.sin(math.pi * (hour - 6) / 12)
        jitter  = self._rng.gauss(1.0, 0.08)
        return base * diurnal * max(0.1, jitter)

    def _apply_fault(self, record: dict, fault_class: str) -> dict:
        """Mutate a flow record to reflect the injected fault."""
        if fault_class == "loss":
            record["pkt_loss_pct"] = round(self._rng.uniform(8, 35), 2)
            record["bytes"] = int(record["bytes"] * (1 - record["pkt_loss_pct"] / 100))
        elif fault_class == "latency":
            record["rtt_ms"] = round(self._rng.uniform(200, 800), 1)
            record["jitter_ms"] = round(self._rng.uniform(40, 150), 1)
        elif fault_class == "rate":
            record["bytes"] = int(record["bytes"] * self._rng.uniform(0.05, 0.15))  # congestion
            record["queue_drops"] = self._rng.randint(50, 500)
        elif fault_class == "flap":
            record["flow_state"] = "INTERRUPTED"
            record["bgp_withdraw"] = True
        elif fault_class == "corrupt":
            record["checksum_errors"] = self._rng.randint(5, 80)
            record["pkt_loss_pct"] = round(self._rng.uniform(1, 10), 2)
        return record

    def generate_batch(self) -> list[dict]:
        records = []
        now_ts  = datetime.now(timezone.utc).isoformat()
        duration = 60  # IPFIX export interval (60s active timeout)

        with self._lock:
            fault_class = self._fault_class
            fault_flow  = self._fault_flow
            self._sequence += 1
            seq = self._sequence

        for flow in CE_FLOWS:
            service = flow["service"]
            rate    = self._base_rate_bps(service)
            packets = int(rate * duration / 1500)  # assume 1500-byte MTU
            flow_key = f"{flow['src_prefix']}->{flow['dst_prefix']}"

            record = {
                "sequence":       seq,
                "timestamp":      now_ts,
                "flow_type":      "L3VPN",
                "src_prefix":     flow["src_prefix"],
                "dst_prefix":     flow["dst_prefix"],
                "dst_port":       _SVC_PORTS.get(service, 0),
                "proto":          6,  # TCP
                "service":        service,
                "pe_ingress":     flow["pe_ingress"],
                "pe_egress":      flow["pe_egress"],
                "mpls_label_in":  3001 if flow["pe_ingress"] == "pe1" else 3002,
                "mpls_label_out": 3002 if flow["pe_egress"] == "pe2" else 3001,
                "bytes":          int(rate * duration / 8),
                "packets":        packets,
                "duration_s":     duration,
                "rtt_ms":         round(self._rng.gauss(12, 2), 1),
                "jitter_ms":      round(abs(self._rng.gauss(0.5, 0.3)), 2),
                "pkt_loss_pct":   0.0,
                "queue_drops":    0,
                "checksum_errors":0,
                "flow_state":     "ACTIVE",
                "bgp_withdraw":   False,
            }

            if fault_class and (fault_flow is None or fault_flow == flow_key):
                record = self._apply_fault(record, fault_class)

            records.append(record)

        for iflow in INFRA_FLOWS:
            records.append({
                "sequence":   seq,
                "timestamp":  now_ts,
                "flow_type":  "INFRA",
                "src":        iflow["src"],
                "dst":        iflow["dst"],
                "proto":      iflow["proto"],
                "label":      iflow["label"],
                "node":       iflow["node"],
                "bytes":      self._rng.randint(200, 2000),
                "packets":    self._rng.randint(1, 10),
                "duration_s": duration,
            })

        return records


_simulator = FlowSimulator()
_last_batch: list[dict] = []
_last_batch_ts: float   = 0.0
_BATCH_INTERVAL = 60.0  # seconds between generated batches


def _get_or_refresh_batch() -> list[dict]:
    global _last_batch, _last_batch_ts
    if time.time() - _last_batch_ts >= _BATCH_INTERVAL:
        _last_batch    = _simulator.generate_batch()
        _last_batch_ts = time.time()
    return _last_batch


# --- HTTP server ----------------------------------------------------------------

class NetFlowHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        if self.path.startswith("/flows"):
            records = _get_or_refresh_batch()
            body = json.dumps(records, indent=2).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif self.path == "/summary":
            records = _get_or_refresh_batch()
            total_bytes = sum(r.get("bytes", 0) for r in records)
            faults = [r for r in records if r.get("pkt_loss_pct", 0) > 5 or r.get("bgp_withdraw")]
            body = json.dumps({
                "total_flows": len(records),
                "total_bytes": total_bytes,
                "fault_flows": len(faults),
                "timestamp":   datetime.now(timezone.utc).isoformat(),
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif self.path.startswith("/inject"):
            # /inject?fault=loss or /inject?fault=clear
            import urllib.parse
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            fault = qs.get("fault", [None])[0]
            if fault == "clear":
                _simulator.clear_fault()
                msg = b'{"status":"cleared"}'
            elif fault:
                _simulator.inject_fault(fault)
                # Force fresh batch next poll
                global _last_batch_ts
                _last_batch_ts = 0.0
                msg = json.dumps({"status": "injected", "fault": fault}).encode()
            else:
                msg = b'{"error":"missing ?fault= param"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(msg)

        else:
            self.send_response(404)
            self.end_headers()


def run_server(port: int):
    print(f"[*] NetFlow/IPFIX Simulator — http://0.0.0.0:{port}/flows")
    print(f"    Endpoints: /flows  /summary  /inject?fault=<loss|latency|rate|flap|corrupt|clear>")
    srv = ThreadingHTTPServer(("0.0.0.0", port), NetFlowHandler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[*] NetFlow simulator stopped.")


def main():
    ap = argparse.ArgumentParser(description="Aether NetFlow/IPFIX Simulator")
    ap.add_argument("--port", type=int, default=9995)
    ap.add_argument("--dump", action="store_true", help="Print one batch of records and exit")
    args = ap.parse_args()

    if args.dump:
        records = _simulator.generate_batch()
        print(json.dumps(records, indent=2))
        return

    run_server(args.port)


if __name__ == "__main__":
    main()
