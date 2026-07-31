#!/usr/bin/env python3
# =============================================================================
# fault_injector.py — Automated Fault Injection Engine & Label Logger
#
# This script injects network anomalies (latency, packet loss, jitter, link flaps)
# into container interfaces and writes a timestamped CSV log for ML training labels.
# =============================================================================
import argparse
import subprocess
import time
import csv
import os
import sys
from datetime import datetime

LAB_NAME = os.environ.get("LAB", os.environ.get("LAB_NAME", "aether"))
# Always write the label log next to this script (the topology dir), so it lands
# in the same place data_collector.py reads from regardless of the caller's CWD.
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "faults_log.csv")

def get_container_name(node):
    return f"clab-{LAB_NAME}-{node}"

def run_cmd(cmd):
    try:
        res = subprocess.run(cmd, shell=True, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        return True, res.stdout
    except subprocess.CalledProcessError as e:
        return False, e.stderr

def ensure_iproute2(container):
    # Check if tc is already available before trying to install anything.
    ok, _ = run_cmd(f"docker exec {container} which tc")
    if ok:
        return
    # Not available — try both Alpine and Debian package managers.
    run_cmd(f"docker exec {container} apk add --no-cache iproute2-tc 2>/dev/null")
    run_cmd(f"docker exec {container} apt-get install -y iproute2 2>/dev/null")

def apply_fault(node, interface, fault_type, value):
    container = get_container_name(node)
    ensure_iproute2(container)
    
    # Clear any existing tc rules first
    run_cmd(f"docker exec {container} tc qdisc del dev {interface} root 2>/dev/null")
    
    if fault_type == "none":
        print(f"[*] Cleared all faults on {node}:{interface}")
        return True
        
    print(f"[*] Injecting {fault_type}={value} on {node}:{interface}...")
    
    if fault_type == "latency":
        # Value format: e.g. "100ms" or "100ms 10ms" (with jitter)
        cmd = f"docker exec {container} tc qdisc add dev {interface} root netem delay {value}"
    elif fault_type == "loss":
        # Value format: e.g. "5%" or "10% 25%" (with correlation)
        cmd = f"docker exec {container} tc qdisc add dev {interface} root netem loss {value}"
    elif fault_type == "corrupt":
        # Value format: e.g. "2%"
        cmd = f"docker exec {container} tc qdisc add dev {interface} root netem corrupt {value}"
    elif fault_type == "rate":
        # Limit bandwidth, value format: e.g. "1mbit"
        # Using tbf (token bucket filter)
        cmd = f"docker exec {container} tc qdisc add dev {interface} root tbf rate {value} latency 50ms burst 1540"
    elif fault_type == "flap":
        # Administrative interface down
        cmd = f"docker exec {container} ip link set dev {interface} down"
    else:
        print(f"[-] Unknown fault type: {fault_type}")
        return False
        
    success, err = run_cmd(cmd)
    if not success:
        print(f"[-] Failed to apply fault: {err.strip()}")
    return success

def recover_fault(node, interface, fault_type):
    container = get_container_name(node)
    if fault_type == "flap":
        cmd = f"docker exec {container} ip link set dev {interface} up"
    else:
        cmd = f"docker exec {container} tc qdisc del dev {interface} root 2>/dev/null"
    
    success, err = run_cmd(cmd)
    if success:
        print(f"[*] Recovered {node}:{interface} from fault.")
    else:
        print(f"[-] Recovery error: {err.strip()}")

def log_fault(timestamp, node, interface, fault_type, value, duration):
    # Log format: Timestamp, Node, Interface, FaultType, Value, Duration(s)
    fields = [timestamp, node, interface, fault_type, value, duration]
    with open(LOG_FILE, mode='a', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(fields)
    print(f"[+] Fault logged to {LOG_FILE}")

def main():
    parser = argparse.ArgumentParser(description="Air-Gapped MPLS Fault Injector")
    parser.add_argument("--node", required=True, help="Target node (e.g. pe1, p1, ce-branch1)")
    parser.add_argument("--interface", required=True, help="Target interface (e.g. eth1, eth2)")
    parser.add_argument("--fault", choices=["latency", "loss", "corrupt", "rate", "flap", "none"], required=True, help="Fault type")
    parser.add_argument("--value", default="", help="Fault parameter value (e.g. 100ms, 5%%, 500kbit)")
    parser.add_argument("--duration", type=int, default=0, help="Duration in seconds. If 0, stays applied until manually cleared.")
    
    args = parser.parse_args()
    
    start_time = datetime.now().isoformat()
    success = apply_fault(args.node, args.interface, args.fault, args.value)
    
    if not success:
        sys.exit(1)
        
    if args.fault != "none":
        log_fault(start_time, args.node, args.interface, args.fault, args.value, args.duration)
        
        if args.duration > 0:
            print(f"[*] Sleeping for {args.duration} seconds before auto-recovery...")
            try:
                time.sleep(args.duration)
            except KeyboardInterrupt:
                print("\n[*] Interrupted! Recovering interface immediately...")
            finally:
                recover_fault(args.node, args.interface, args.fault)
                log_fault(datetime.now().isoformat(), args.node, args.interface, "recovery", "", 0)

if __name__ == "__main__":
    # Create log header if it doesn't exist
    try:
        with open(LOG_FILE, 'r') as f:
            pass
    except FileNotFoundError:
        with open(LOG_FILE, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(["Timestamp", "Node", "Interface", "FaultType", "Value", "DurationSeconds"])
            
    main()
