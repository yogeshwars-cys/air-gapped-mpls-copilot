#!/usr/bin/env python3
# =============================================================================
# data_collector.py — Telemetry-to-Dataset Bridge
#
# Continuously polls the Prometheus exporter and writes time-series data
# into a structured CSV dataset for ML model training.
# Also merges fault injection labels from faults_log.csv to create a
# supervised training set.
#
# Usage:
#   python3 data_collector.py --duration 300 --interval 1 --output dataset.csv
#
# Zero external dependencies — uses stdlib only (urllib, csv, json).
# =============================================================================
import urllib.request
import csv
import json
import time
import argparse
import os
import re
from datetime import datetime

EXPORTER_URL = "http://127.0.0.1:8000/metrics"
FAULT_LOG = os.path.join(os.path.dirname(__file__), "..", "phase1-simulation", "topology", "faults_log.csv")

# Metrics we care about for ML features
TARGET_METRICS = [
    "net_rx_bytes", "net_tx_bytes",
    "net_rx_packets", "net_tx_packets",
    "net_rx_errors", "net_tx_errors",
    "net_rx_drops", "net_tx_drops",
    "frr_ospf_neighbors_total",
    "frr_ospf_neighbor_full",
    "frr_ldp_neighbors_total",
    "frr_ldp_session_operational",
    "frr_bgp_vpn_established",
    "frr_bgp_vpn_prefixes_received",
    "frr_bgp_vrf_established",
    "frr_bgp_vrf_prefixes_received",
    "container_running"
]

# Nodes and interfaces we track
CORE_NODES = ["pe1", "p1", "pe2"]
ALL_NODES = ["pe1", "p1", "pe2", "ce-branch1", "ce-branch2", "ce-hub", "ce-dc"]


def parse_prometheus_text(text):
    """
    Parses Prometheus exposition format text into a dict of
    {metric_name{labels}: value}
    """
    metrics = {}
    for line in text.strip().split('\n'):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        # Match: metric_name{label="val",...} value
        match = re.match(r'^(\w+)(\{[^}]*\})?\s+(.+)$', line)
        if match:
            name = match.group(1)
            labels = match.group(2) or ""
            value = match.group(3)
            key = f"{name}{labels}"
            try:
                metrics[key] = float(value)
            except ValueError:
                metrics[key] = value
    return metrics


def flatten_metrics(raw_metrics):
    """
    Converts raw Prometheus metrics dict into a flat feature vector dict
    with predictable column names like: pe1_eth1_rx_bytes, p1_ospf_neighbors_total, etc.
    """
    features = {}

    for key, value in raw_metrics.items():
        # Extract metric name and labels
        match = re.match(r'^(\w+)\{([^}]*)\}$', key)
        if not match:
            # No labels (e.g., standalone metric)
            continue

        metric_name = match.group(1)
        label_str = match.group(2)

        # Parse labels
        labels = {}
        for lm in re.finditer(r'(\w+)="([^"]*)"', label_str):
            labels[lm.group(1)] = lm.group(2)

        node = labels.get("node", "unknown")
        iface = labels.get("interface", "")
        peer = labels.get("peer", "")
        neighbor = labels.get("neighbor", "")

        # Build a flat column name
        if iface:
            col = f"{node}_{iface}_{metric_name}"
        elif peer:
            col = f"{node}_peer{peer}_{metric_name}"
        elif neighbor:
            col = f"{node}_nbr{neighbor}_{metric_name}"
        else:
            col = f"{node}_{metric_name}"

        # Sanitize column name
        col = col.replace(".", "_").replace("-", "_").replace(":", "_")
        features[col] = value

    return features


def get_active_faults(fault_log_path):
    """
    Reads the fault injection log and returns any currently active faults
    (injected but not yet recovered).
    """
    if not os.path.exists(fault_log_path):
        return "Healthy", ""

    active_faults = {}
    try:
        with open(fault_log_path, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                key = f"{row['Node']}_{row['Interface']}"
                if row['FaultType'] == 'recovery':
                    active_faults.pop(key, None)
                else:
                    active_faults[key] = row['FaultType']
    except Exception:
        return "Healthy", ""

    if not active_faults:
        return "Healthy", ""

    # Return the most severe active fault as the label
    fault_types = list(active_faults.values())
    fault_locations = list(active_faults.keys())

    # Priority ordering
    for ft in ["flap", "loss", "corrupt", "latency", "rate"]:
        if ft in fault_types:
            return ft, ",".join(fault_locations)

    return fault_types[0], ",".join(fault_locations)


def scrape_once():
    """
    Scrapes the exporter once and returns a flat feature dict.
    """
    try:
        req = urllib.request.Request(EXPORTER_URL)
        with urllib.request.urlopen(req, timeout=2) as resp:
            text = resp.read().decode('utf-8')
        raw = parse_prometheus_text(text)
        return flatten_metrics(raw)
    except Exception as e:
        return {"error": str(e)}


def compute_deltas(current, previous):
    """
    Computes per-second rate of change for counter metrics (bytes, packets).
    """
    deltas = {}
    for key in current:
        if any(x in key for x in ["bytes", "packets", "errors", "drops"]):
            prev_val = previous.get(key, current[key])
            delta = current[key] - prev_val
            deltas[f"{key}_rate"] = max(0, delta)  # Rate per interval
    return deltas


def collect_dataset(duration_sec, interval_sec, output_path):
    """
    Main collection loop. Scrapes metrics, computes deltas, labels with fault state,
    and writes a CSV dataset.
    """
    print(f"[*] Collecting data for {duration_sec}s at {interval_sec}s intervals → {output_path}")
    print(f"[*] Exporter URL: {EXPORTER_URL}")
    print(f"[*] Fault log: {FAULT_LOG}")

    # First scrape to establish columns
    first = scrape_once()
    if "error" in first:
        print(f"[-] Cannot reach exporter: {first['error']}")
        print("[!] Start the exporter first: cd phase2-telemetry && python3 exporter.py")
        return False

    all_columns = sorted(first.keys())
    # Add delta columns
    delta_columns = [f"{k}_rate" for k in all_columns if any(x in k for x in ["bytes", "packets", "errors", "drops"])]
    header = ["timestamp", "fault_label", "fault_location"] + all_columns + delta_columns

    file_exists = os.path.exists(output_path)
    mode = 'a' if file_exists else 'w'

    with open(output_path, mode, newline='') as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(header)

        previous = first
        samples = 0
        start = time.time()

        while (time.time() - start) < duration_sec:
            current = scrape_once()
            if "error" in current:
                print(f"  [!] Scrape failed: {current['error']}, retrying...")
                time.sleep(interval_sec)
                continue

            deltas = compute_deltas(current, previous)
            fault_label, fault_loc = get_active_faults(FAULT_LOG)
            ts = datetime.now().isoformat()

            row = [ts, fault_label, fault_loc]
            for col in all_columns:
                row.append(current.get(col, 0))
            for col in delta_columns:
                row.append(deltas.get(col, 0))

            writer.writerow(row)
            f.flush()

            previous = current
            samples += 1

            if samples % 30 == 0:
                print(f"  [{samples}] {ts} | label={fault_label} | features={len(current)}")

            time.sleep(interval_sec)

    print(f"[+] Collection complete: {samples} samples written to {output_path}")
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Aether Telemetry Data Collector")
    parser.add_argument("--duration", type=int, default=300, help="Collection duration in seconds (default: 300)")
    parser.add_argument("--interval", type=float, default=1.0, help="Scrape interval in seconds (default: 1.0)")
    parser.add_argument("--output", default="dataset.csv", help="Output CSV file path (default: dataset.csv)")
    args = parser.parse_args()

    collect_dataset(args.duration, args.interval, args.output)
