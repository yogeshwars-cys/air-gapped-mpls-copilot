#!/usr/bin/env python3
# =============================================================================
# generate_dataset.py — Synthetic MPLS Telemetry Dataset Generator
#
# Produces a large, temporally-realistic labeled dataset for training all
# three Aether models (Autoencoder, Classifier, TTF Regressor).
#
# Design goals:
#   1. Exact column match to data_collector.py output (140 columns)
#   2. Monotonically-increasing cumulative counters (realistic byte/packet totals)
#   3. Derived rate columns (per-sample delta, not independently random)
#   4. Temporal continuity: fault episodes have gradual onset, sustained, recovery
#   5. Realistic traffic mix: VoIP (200kbps) + DB (1Mbps) + HTTP + management
#   6. All 6 fault classes with 20+ intensity variants per class
#   7. FRR state: BGP/OSPF/LDP sessions properly model reconvergence delays
#
# Usage:
#   python3 generate_dataset.py                        # 50k rows → dataset_large.csv
#   python3 generate_dataset.py --rows 100000 --out big.csv
# =============================================================================
import os
import sys
import csv
import math
import random
import argparse
import numpy as np
from datetime import datetime, timedelta

random.seed(42)
np.random.seed(42)

# ── Schema (must match data_collector.py header exactly) ─────────────────────

NODES = ["ce_branch1", "ce_branch2", "ce_dc", "ce_hub", "p1", "pe1", "pe2"]
IFACE = "eth0"

FRR_NODES = {
    "p1":  {"ldp": True,  "ospf": True, "bgp_vpn": [],   "bgp_vrf": []},
    "pe1": {"ldp": True,  "ospf": True,
            "bgp_vpn": ["3_3_3_3"],
            "bgp_vrf": ["10_1_11_2", "10_1_13_2"]},
    "pe2": {"ldp": True,  "ospf": True,
            "bgp_vpn": ["1_1_1_1"],
            "bgp_vrf": ["10_2_12_2", "10_2_14_2"]},
}

def _build_header():
    # time_to_breach (col index 3) is a regression target, NOT a feature.
    # It is the lead time in seconds until the upcoming SLA breach (fault plateau).
    cols = ["timestamp", "fault_label", "fault_location", "time_to_breach"]
    # cumulative counters
    for node in NODES:
        cols.append(f"{node}_container_running")
        for m in ["rx_bytes","rx_drops","rx_errors","rx_packets","tx_bytes","tx_drops","tx_errors","tx_packets"]:
            cols.append(f"{node}_{IFACE}_net_{m}")
        if node in FRR_NODES:
            info = FRR_NODES[node]
            if info["ldp"]:  cols.append(f"{node}_frr_ldp_neighbors_total")
            if info["ospf"]: cols.append(f"{node}_frr_ospf_neighbors_total")
            for peer in info["bgp_vpn"]:
                cols.append(f"{node}_peer{peer}_frr_bgp_vpn_established")
                cols.append(f"{node}_peer{peer}_frr_bgp_vpn_prefixes_received")
            for peer in info["bgp_vrf"]:
                cols.append(f"{node}_peer{peer}_frr_bgp_vrf_established")
                cols.append(f"{node}_peer{peer}_frr_bgp_vrf_prefixes_received")
    # rate columns (delta / 2 seconds)
    for node in NODES:
        for m in ["rx_bytes","rx_drops","rx_errors","rx_packets","tx_bytes","tx_drops","tx_errors","tx_packets"]:
            cols.append(f"{node}_{IFACE}_net_{m}_rate")
    return cols

HEADER = _build_header()

# ── Base traffic model (bytes/sec) ────────────────────────────────────────────
# Each node has a base rx/tx rate in healthy state.
# These represent: VoIP (200kbps) + DB (1Mbps) + HTTP (500kbps) + management (50kbps)

BASE_RATES = {
    "ce_branch1": {"rx":  80_000, "tx": 150_000, "rx_pkt": 80,  "tx_pkt": 120},
    "ce_branch2": {"rx":  60_000, "tx": 100_000, "rx_pkt": 60,  "tx_pkt": 90},
    "ce_dc":      {"rx": 250_000, "tx": 180_000, "rx_pkt": 200, "tx_pkt": 160},
    "ce_hub":     {"rx":  70_000, "tx": 120_000, "rx_pkt": 65,  "tx_pkt": 100},
    "p1":         {"rx": 380_000, "tx": 390_000, "rx_pkt": 330, "tx_pkt": 340},
    "pe1":        {"rx": 200_000, "tx": 210_000, "rx_pkt": 175, "tx_pkt": 180},
    "pe2":        {"rx": 190_000, "tx": 200_000, "rx_pkt": 165, "tx_pkt": 170},
}

# Normal drop/error rates (very low in healthy state)
HEALTHY_DROP_RATE  = 0.0002  # 0.02% of packets
HEALTHY_ERROR_RATE = 0.00005

# FRR neighbor counts in healthy state
FRR_HEALTHY = {
    "p1":  {"ldp": 2, "ospf": 2},
    "pe1": {"ldp": 1, "ospf": 1, "bgp_vpn": 12, "bgp_vrf": 4},
    "pe2": {"ldp": 1, "ospf": 1, "bgp_vpn": 10, "bgp_vrf": 4},
}

DT_SECONDS = 2  # sample interval matches real collector

# Time-to-breach (lead-time) regression target.
#
# An SLA breach is a *sustained* performance violation, not the instant a metric
# first moves. So "breach" is defined as the fault having been active through its
# onset ramp AND sustained for SLA_SUSTAIN_SAMPLES into the plateau. This means
# the onset ramp and the early plateau — where the fault is clearly detectable and
# classifiable — still carry a positive lead time counting down to the breach.
# time_to_breach hits 0 at the breach and stays 0 through the rest of the episode.
# Far from any fault it saturates at MAX_LEAD_SECONDS (sentinel: nothing imminent).
MAX_LEAD_SECONDS  = 300.0
SLA_SUSTAIN_SAMPLES = 20  # a fault must persist this many samples (×2s = 40s) to breach SLA

# ── Fault intensity profiles ──────────────────────────────────────────────────

FAULT_PROFILES = {
    # Profile tuples — index positions by fault class:
    #   latency : (node, label, byte_reduction, rx_drop_frac, dur_range)
    #   loss    : (node, label, rx_drop_frac,   byte_reduction, dur_range)
    #   corrupt : (node, label, rx_err_frac, rx_drop_frac, byte_reduction, dur_range)
    #   rate    : (node, label, tx_drop_frac,   byte_reduction, dur_range)
    #   flap    : (node, reconverge_samples, dur_range)
    #
    # All *_frac values are ABSOLUTE fractions (not multipliers):
    #   rx_drop_frac = fraction of rx packets that get dropped
    #   tx_drop_frac = fraction of tx packets that get dropped (rate shaping)
    #   rx_err_frac  = fraction of rx packets with bit errors (corrupt)
    #   byte_reduction = fraction bytes decrease from TCP backoff / rate cap
    "latency": [
        # Node, label, byte_reduction (DIRECT fraction), rx_drop_frac (queuing), dur
        # byte_reduction is applied directly: rx_bps *= (1 - br). No damping.
        # Latency main signal: bytes drop (TCP backoff). FRR stays healthy.
        ("pe1", "mild",      0.10, 0.000, (20, 50)),
        ("pe1", "moderate",  0.25, 0.003, (30, 80)),
        ("pe1", "severe",    0.48, 0.012, (25, 60)),
        ("pe1", "critical",  0.68, 0.030, (20, 50)),
        ("pe1", "extreme",   0.82, 0.055, (15, 35)),
        ("p1",  "mild",      0.12, 0.000, (20, 50)),
        ("p1",  "moderate",  0.30, 0.005, (30, 70)),
        ("p1",  "severe",    0.55, 0.018, (20, 50)),
        ("p1",  "critical",  0.72, 0.040, (15, 35)),
        ("pe2", "mild",      0.08, 0.000, (20, 40)),
        ("pe2", "moderate",  0.22, 0.003, (25, 60)),
        ("pe2", "severe",    0.50, 0.015, (20, 50)),
    ],
    "loss": [
        # Node, label, rx_drop_frac, byte_reduction, dur
        ("pe1", "0.5pct",  0.005, 0.003, (25, 60)),
        ("pe1", "1pct",    0.010, 0.007, (25, 60)),
        ("pe1", "3pct",    0.030, 0.020, (20, 50)),
        ("pe1", "8pct",    0.080, 0.055, (20, 45)),
        ("pe1", "15pct",   0.150, 0.100, (15, 40)),
        ("pe1", "25pct",   0.250, 0.170, (10, 30)),
        ("p1",  "1pct",    0.010, 0.007, (25, 55)),
        ("p1",  "5pct",    0.050, 0.035, (20, 50)),
        ("p1",  "12pct",   0.120, 0.080, (15, 40)),
        ("p1",  "20pct",   0.200, 0.140, (10, 30)),
        ("pe2", "3pct",    0.030, 0.020, (20, 50)),
        ("pe2", "10pct",   0.100, 0.068, (15, 40)),
        ("pe2", "18pct",   0.180, 0.120, (10, 30)),
    ],
    "corrupt": [
        # Node, label, rx_err_frac, rx_drop_frac (~0.8*err), byte_reduction, dur
        ("pe1", "0.1pct",  0.001, 0.0008, 0.001, (20, 50)),
        ("pe1", "0.5pct",  0.005, 0.004,  0.003, (25, 55)),
        ("pe1", "1pct",    0.010, 0.008,  0.007, (25, 55)),
        ("pe1", "3pct",    0.030, 0.024,  0.018, (20, 50)),
        ("pe1", "7pct",    0.070, 0.056,  0.040, (15, 40)),
        ("pe1", "12pct",   0.120, 0.096,  0.060, (10, 30)),
        ("p1",  "0.5pct",  0.005, 0.004,  0.003, (25, 55)),
        ("p1",  "2pct",    0.020, 0.016,  0.012, (20, 50)),
        ("p1",  "5pct",    0.050, 0.040,  0.028, (15, 40)),
        ("pe2", "1pct",    0.010, 0.008,  0.007, (25, 55)),
        ("pe2", "4pct",    0.040, 0.032,  0.022, (15, 40)),
    ],
    "rate": [
        # Node, label, tx_drop_frac (excess above cap), byte_reduction, dur
        # pe1 base ~210kbps; pe2 ~200kbps; p1 ~390kbps
        ("pe1", "5mbit",    0.00,  0.15, (20, 55)),  # cap >> base → minimal impact
        ("pe1", "2mbit",    0.10,  0.40, (25, 60)),  # moderate saturation
        ("pe1", "1mbit",    0.40,  0.65, (20, 50)),  # heavy saturation
        ("pe1", "500kbit",  0.70,  0.82, (15, 40)),  # extreme saturation
        ("pe1", "200kbit",  0.88,  0.92, (10, 30)),  # severe cap
        ("p1",  "5mbit",    0.00,  0.10, (20, 55)),
        ("p1",  "3mbit",    0.08,  0.30, (25, 55)),
        ("p1",  "1.5mbit",  0.35,  0.60, (20, 50)),
        ("p1",  "800kbit",  0.65,  0.78, (15, 40)),
        ("pe2", "2mbit",    0.12,  0.42, (20, 50)),
        ("pe2", "1mbit",    0.38,  0.65, (15, 40)),
        ("pe2", "500kbit",  0.68,  0.80, (10, 30)),
    ],
    "flap": [
        # (node, reconverge_seconds, dur_range_samples)
        ("pe1", 25, (10, 20)),
        ("pe1", 40, (8,  15)),
        ("pe2", 30, (10, 20)),
        ("p1",  20, (8,  15)),
        ("pe1", 60, (5,  12)),
        ("pe2", 50, (5,  12)),
        ("p1",  45, (5,  12)),
    ],
}

# ── Simulation state ──────────────────────────────────────────────────────────

class NodeState:
    """Tracks cumulative counters for one node."""
    def __init__(self, node: str):
        self.node = node
        r = BASE_RATES[node]
        # Cumulative counters — start at random offset to simulate uptime
        self.rx_bytes   = random.randint(50_000_000, 500_000_000)
        self.tx_bytes   = random.randint(50_000_000, 500_000_000)
        self.rx_packets = int(self.rx_bytes / random.uniform(400, 1200))
        self.tx_packets = int(self.tx_bytes / random.uniform(400, 1200))
        self.rx_drops   = random.randint(0, 500)
        self.tx_drops   = random.randint(0, 200)
        self.rx_errors  = random.randint(0, 100)
        self.tx_errors  = random.randint(0, 50)
        # Previous sample (for rate calc)
        self._prev = None

    def step(self, rx_bps: float, tx_bps: float, rx_pps: float, tx_pps: float,
             rx_drop_frac: float = 0.0, tx_drop_frac: float = 0.0,
             rx_err_frac: float = 0.0) -> dict:
        """
        Advance one time step and return both absolute and rate values.
        *_frac params are ABSOLUTE fractions (0.0–1.0) of packets that are
        dropped/errored — NOT multipliers of the base rate. This gives
        clear, ML-distinguishable signals at each fault severity.
        """
        prev_rx_bytes   = self.rx_bytes
        prev_tx_bytes   = self.tx_bytes
        prev_rx_packets = self.rx_packets
        prev_tx_packets = self.tx_packets
        prev_rx_drops   = self.rx_drops
        prev_tx_drops   = self.tx_drops
        prev_rx_errors  = self.rx_errors
        prev_tx_errors  = self.tx_errors

        # Add this sample's traffic (DT_SECONDS worth)
        rx_b = int(rx_bps * DT_SECONDS * _noise())
        tx_b = int(tx_bps * DT_SECONDS * _noise())
        rx_p = int(rx_pps * DT_SECONDS * _noise())
        tx_p = int(tx_pps * DT_SECONDS * _noise())

        # Drops and errors: base healthy + fault-specific absolute fraction
        total_rx_drop_frac = HEALTHY_DROP_RATE + rx_drop_frac
        total_tx_drop_frac = HEALTHY_DROP_RATE + tx_drop_frac
        total_rx_err_frac  = HEALTHY_ERROR_RATE + rx_err_frac

        rx_drop_this = max(0, int(rx_p * total_rx_drop_frac * _noise(0.25)))
        tx_drop_this = max(0, int(tx_p * total_tx_drop_frac * _noise(0.25)))
        rx_err_this  = max(0, int(rx_p * total_rx_err_frac  * _noise(0.25)))
        tx_err_this  = 0

        self.rx_bytes   += rx_b
        self.tx_bytes   += tx_b
        self.rx_packets += rx_p
        self.tx_packets += tx_p
        self.rx_drops   += max(0, rx_drop_this)
        self.tx_drops   += max(0, tx_drop_this)
        self.rx_errors  += max(0, rx_err_this)

        return {
            "abs": {
                "rx_bytes":   self.rx_bytes,
                "rx_drops":   self.rx_drops,
                "rx_errors":  self.rx_errors,
                "rx_packets": self.rx_packets,
                "tx_bytes":   self.tx_bytes,
                "tx_drops":   self.tx_drops,
                "tx_errors":  self.tx_errors,
                "tx_packets": self.tx_packets,
            },
            "rate": {
                "rx_bytes":   (self.rx_bytes   - prev_rx_bytes)   / DT_SECONDS,
                "rx_drops":   (self.rx_drops   - prev_rx_drops)   / DT_SECONDS,
                "rx_errors":  (self.rx_errors  - prev_rx_errors)  / DT_SECONDS,
                "rx_packets": (self.rx_packets - prev_rx_packets) / DT_SECONDS,
                "tx_bytes":   (self.tx_bytes   - prev_tx_bytes)   / DT_SECONDS,
                "tx_drops":   (self.tx_drops   - prev_tx_drops)   / DT_SECONDS,
                "tx_errors":  (self.tx_errors  - prev_tx_errors)  / DT_SECONDS,
                "tx_packets": (self.tx_packets - prev_tx_packets) / DT_SECONDS,
            }
        }


class FRRState:
    """Tracks BGP/OSPF/LDP state including reconvergence dynamics."""
    def __init__(self):
        self.reset()

    def reset(self):
        self.ldp_pe1  = FRR_HEALTHY["pe1"]["ldp"]
        self.ldp_pe2  = FRR_HEALTHY["pe2"]["ldp"]
        self.ldp_p1   = FRR_HEALTHY["p1"]["ldp"]
        self.ospf_pe1 = FRR_HEALTHY["pe1"]["ospf"]
        self.ospf_pe2 = FRR_HEALTHY["pe2"]["ospf"]
        self.ospf_p1  = FRR_HEALTHY["p1"]["ospf"]
        self.bgp_vpn_pe1 = FRR_HEALTHY["pe1"]["bgp_vpn"]
        self.bgp_vpn_pe2 = FRR_HEALTHY["pe2"]["bgp_vpn"]
        self.bgp_vrf_pe1 = FRR_HEALTHY["pe1"]["bgp_vrf"]
        self.bgp_vrf_pe2 = FRR_HEALTHY["pe2"]["bgp_vrf"]
        self._flap_sample = 0
        self._reconverge_at = 0
        self._flapping_node = None

    def trigger_flap(self, node: str, reconverge_samples: int, current_sample: int):
        self._flapping_node = node
        self._flap_sample   = current_sample
        self._reconverge_at = current_sample + reconverge_samples
        self._set_down(node)

    def step(self, current_sample: int):
        if self._flapping_node and current_sample >= self._reconverge_at:
            self._set_up(self._flapping_node)
            self._flapping_node = None

    def _set_down(self, node: str):
        if node == "pe1":
            # pe1 itself loses its sessions; pe2's VPN session TO pe1 also drops
            self.bgp_vpn_pe1 = 0; self.bgp_vrf_pe1 = 0
            self.ospf_pe1 = 0; self.ldp_pe1 = 0
            self.bgp_vpn_pe2 = 0  # pe2's view of the VPN peering drops too
        elif node == "pe2":
            # pe2 loses sessions; pe1's VPN session TO pe2 also drops
            self.bgp_vpn_pe2 = 0; self.bgp_vrf_pe2 = 0
            self.ospf_pe2 = 0; self.ldp_pe2 = 0
            self.bgp_vpn_pe1 = 0  # pe1's view of the VPN peering to pe2 drops too
        elif node == "p1":
            # p1 going down breaks the MPLS core — both PE nodes lose ALL sessions
            self.ldp_p1 = 0; self.ospf_p1 = 0
            self.ldp_pe1 = 0; self.ospf_pe1 = 0; self.bgp_vpn_pe1 = 0; self.bgp_vrf_pe1 = 0
            self.ldp_pe2 = 0; self.ospf_pe2 = 0; self.bgp_vpn_pe2 = 0; self.bgp_vrf_pe2 = 0

    def _set_up(self, node: str):
        if node == "pe1":
            self.bgp_vpn_pe1 = FRR_HEALTHY["pe1"]["bgp_vpn"]
            self.bgp_vrf_pe1 = FRR_HEALTHY["pe1"]["bgp_vrf"]
            self.ospf_pe1 = FRR_HEALTHY["pe1"]["ospf"]
            self.ldp_pe1  = FRR_HEALTHY["pe1"]["ldp"]
            self.bgp_vpn_pe2 = FRR_HEALTHY["pe2"]["bgp_vpn"]  # restore pe2's view
        elif node == "pe2":
            self.bgp_vpn_pe2 = FRR_HEALTHY["pe2"]["bgp_vpn"]
            self.bgp_vrf_pe2 = FRR_HEALTHY["pe2"]["bgp_vrf"]
            self.ospf_pe2 = FRR_HEALTHY["pe2"]["ospf"]
            self.ldp_pe2  = FRR_HEALTHY["pe2"]["ldp"]
            self.bgp_vpn_pe1 = FRR_HEALTHY["pe1"]["bgp_vpn"]  # restore pe1's view
        elif node == "p1":
            self.ldp_p1 = FRR_HEALTHY["p1"]["ldp"]
            self.ospf_p1 = FRR_HEALTHY["p1"]["ospf"]
            self.ldp_pe1 = FRR_HEALTHY["pe1"]["ldp"]; self.ospf_pe1 = FRR_HEALTHY["pe1"]["ospf"]
            self.bgp_vpn_pe1 = FRR_HEALTHY["pe1"]["bgp_vpn"]; self.bgp_vrf_pe1 = FRR_HEALTHY["pe1"]["bgp_vrf"]
            self.ldp_pe2 = FRR_HEALTHY["pe2"]["ldp"]; self.ospf_pe2 = FRR_HEALTHY["pe2"]["ospf"]
            self.bgp_vpn_pe2 = FRR_HEALTHY["pe2"]["bgp_vpn"]; self.bgp_vrf_pe2 = FRR_HEALTHY["pe2"]["bgp_vrf"]

    def is_down(self) -> bool:
        return self._flapping_node is not None

    def get_values(self) -> dict:
        return {
            "p1_ldp":  self.ldp_p1, "p1_ospf": self.ospf_p1,
            "pe1_ldp": self.ldp_pe1, "pe1_ospf": self.ospf_pe1,
            "pe1_bgp_vpn": self.bgp_vpn_pe1, "pe1_bgp_vrf": self.bgp_vrf_pe1,
            "pe2_ldp": self.ldp_pe2, "pe2_ospf": self.ospf_pe2,
            "pe2_bgp_vpn": self.bgp_vpn_pe2, "pe2_bgp_vrf": self.bgp_vrf_pe2,
        }


def _noise(scale: float = 0.1) -> float:
    """Multiplicative noise factor around 1.0."""
    return max(0.0, 1.0 + random.gauss(0, scale))


def _ramp(step: int, onset: int, plateau: int, recovery: int) -> float:
    """
    Returns a [0, 1] severity multiplier for smooth fault onset/recovery.
    step=0 is fault start, plateau ends at (onset+plateau), recovery ends at total.
    """
    total = onset + plateau + recovery
    if step < onset:
        return step / max(onset, 1)
    elif step < onset + plateau:
        return 1.0
    elif step < total:
        return 1.0 - (step - onset - plateau) / max(recovery, 1)
    return 0.0


# ── Episode generators ────────────────────────────────────────────────────────

def _diurnal_factor(sample_idx: int) -> float:
    """Simulates business-hour traffic peaks (higher during day, quieter at night)."""
    # 1 day = 43200 samples at 2s; range 0.5 (night) to 1.3 (peak)
    hour_phase = (sample_idx * DT_SECONDS % 86400) / 86400
    return 0.75 + 0.55 * math.sin(math.pi * hour_phase)  # peak at noon


def generate_healthy_episode(states: dict, frr: FRRState,
                              duration: int, start_idx: int, ts: datetime,
                              writer, out_rows: list):
    """Generate a block of healthy samples."""
    for i in range(duration):
        frr.step(start_idx + i)
        d = _diurnal_factor(start_idx + i)
        row = _build_row(ts, "Healthy", "", states, frr, d, {})
        out_rows.append(row)
        ts += timedelta(seconds=DT_SECONDS)
    return ts


def _profile_dur_range(fc: str, profile: tuple) -> tuple:
    """Return the duration range tuple from a profile, regardless of fault class."""
    if fc == "flap":
        return profile[2]
    elif fc == "corrupt":
        return profile[5]
    else:
        return profile[4]


def _build_node_overrides(fault_class: str, profile: tuple, eff: float,
                           frr: FRRState) -> dict:
    """Return the override dict for one node at given severity×propagation."""
    if fault_class == "latency":
        _, _, byte_reduction, rx_drop_frac, _ = profile
        return {"byte_reduction": byte_reduction * eff,
                "rx_drop_frac":   rx_drop_frac   * eff}
    elif fault_class == "loss":
        _, _, rx_drop_frac, byte_reduction, _ = profile
        return {"rx_drop_frac":   rx_drop_frac   * eff,
                "byte_reduction": byte_reduction * eff}
    elif fault_class == "corrupt":
        _, _, rx_err_frac, rx_drop_frac, byte_reduction, _ = profile
        return {"rx_err_frac":    rx_err_frac    * eff,
                "rx_drop_frac":   rx_drop_frac   * eff,
                "byte_reduction": byte_reduction * eff}
    elif fault_class == "rate":
        _, _, tx_drop_frac, byte_reduction, _ = profile
        return {"tx_drop_frac":   tx_drop_frac   * eff,
                "byte_reduction": byte_reduction * eff,
                "tx_cap":         True}
    elif fault_class == "flap":
        fs = 1.0 if frr.is_down() else 0.0
        return {"traffic_kill": fs}
    return {}


def generate_fault_episode(fault_class: str, profile: tuple,
                            states: dict, frr: FRRState,
                            start_idx: int, ts: datetime,
                            writer, out_rows: list):
    """Generate a fault episode with gradual onset, sustained phase, and recovery."""
    affected_node = profile[0]
    dur_range     = _profile_dur_range(fault_class, profile)
    duration      = random.randint(*dur_range)

    if fault_class == "flap":
        reconverge_t = profile[1]
        reconverge_s = reconverge_t // DT_SECONDS
        frr.trigger_flap(affected_node, reconverge_s, start_idx)
        onset = 2; plateau = duration - 2; recovery = 0
    else:
        onset    = max(3, int(duration * 0.15))
        plateau  = max(5, int(duration * 0.65))
        recovery = duration - onset - plateau

    d = _diurnal_factor(start_idx)

    for i in range(duration):
        frr.step(start_idx + i)
        severity = _ramp(i, onset, plateau, recovery)

        node_overrides = {}
        for node in NODES:
            if node == affected_node or _is_downstream(node, affected_node):
                prop = 1.0 if node == affected_node else 0.4
                eff  = severity * prop
                node_overrides[node] = _build_node_overrides(fault_class, profile, eff, frr)

        row = _build_row(ts, fault_class, affected_node, states, frr, d, node_overrides)
        out_rows.append(row)
        ts += timedelta(seconds=DT_SECONDS)

    return ts


def _is_downstream(node: str, affected: str) -> bool:
    """Nodes that would see degradation when affected node is faulty."""
    downstream_map = {
        "p1":  ["pe1", "pe2", "ce_branch1", "ce_branch2", "ce_hub", "ce_dc"],
        "pe1": ["ce_branch1", "ce_hub"],
        "pe2": ["ce_branch2", "ce_dc"],
    }
    return node in downstream_map.get(affected, [])


def _build_row(ts: datetime, fault_label: str, fault_location: str,
               states: dict, frr: FRRState, diurnal: float,
               node_overrides: dict) -> list:
    """Build one CSV row of 140 values."""
    row = [ts.isoformat(), fault_label, fault_location]
    frr_vals = frr.get_values()
    all_metrics = {}  # node → {abs, rate}

    for node in NODES:
        base         = BASE_RATES[node]
        ov           = node_overrides.get(node, {})
        rx_drop_frac = ov.get("rx_drop_frac", 0.0)  # absolute drop fraction for rx
        tx_drop_frac = ov.get("tx_drop_frac", 0.0)  # absolute drop fraction for tx (rate shaping)
        rx_err_frac  = ov.get("rx_err_frac",  0.0)  # absolute error fraction (corrupt)
        br           = ov.get("byte_reduction", 0.0)
        tx_cap       = ov.get("tx_cap", False)
        traffic_kill = ov.get("traffic_kill", 0.0)

        # When routes are withdrawn (BGP flap), traffic collapses near-zero
        kill_factor = max(0.01, 1.0 - traffic_kill * 0.98)

        # byte_reduction is a DIRECT fraction — apply it straight (no damping factor).
        # rate faults: tx drops heavily (tx_cap), rx also falls (TCP backoff upstream).
        rx_bps = base["rx"] * diurnal * max(0.01, 1.0 - br) * kill_factor
        tx_bps = base["tx"] * diurnal * max(0.01, 1.0 - br * (0.98 if tx_cap else 1.0)) * kill_factor
        rx_pps = base["rx_pkt"] * diurnal * max(0.01, 1.0 - br * 0.90) * kill_factor
        tx_pps = base["tx_pkt"] * diurnal * max(0.01, 1.0 - br * (0.98 if tx_cap else 0.90)) * kill_factor

        m = states[node].step(rx_bps, tx_bps, rx_pps, tx_pps,
                              rx_drop_frac, tx_drop_frac, rx_err_frac)
        all_metrics[node] = m

        row.append(1)  # container_running
        for k in ["rx_bytes","rx_drops","rx_errors","rx_packets",
                  "tx_bytes","tx_drops","tx_errors","tx_packets"]:
            row.append(int(m["abs"][k]))

        if node in FRR_NODES:
            info = FRR_NODES[node]
            if info["ldp"]:  row.append(frr_vals[f"{node}_ldp"])
            if info["ospf"]: row.append(frr_vals[f"{node}_ospf"])
            for peer in info["bgp_vpn"]:
                est = frr_vals[f"{node}_bgp_vpn"]
                row.append(1 if est > 0 else 0)
                row.append(est if est > 0 else 0)
            for peer in info["bgp_vrf"]:
                est = frr_vals[f"{node}_bgp_vrf"]
                row.append(1 if est > 0 else 0)
                row.append(max(0, est * 2 + random.randint(-1, 1)))

    # Rate columns
    for node in NODES:
        m = all_metrics[node]
        for k in ["rx_bytes","rx_drops","rx_errors","rx_packets",
                  "tx_bytes","tx_drops","tx_errors","tx_packets"]:
            row.append(round(m["rate"][k], 2))

    return row


# ── Episode plan builder ──────────────────────────────────────────────────────

def _build_episode_plan(target_rows: int, seed: int = 0) -> list:
    """
    Returns a list of (fault_class, profile_or_None, duration) tuples
    that sum to approximately target_rows.
    Distribution: 40% healthy, 12% each fault class.
    """
    random.seed(seed)
    fault_classes = ["latency", "loss", "corrupt", "rate", "flap"]
    targets = {
        "Healthy":  int(target_rows * 0.40),
        "latency":  int(target_rows * 0.12),
        "loss":     int(target_rows * 0.12),
        "corrupt":  int(target_rows * 0.12),
        "rate":     int(target_rows * 0.12),
        "flap":     int(target_rows * 0.12),
    }

    plan = []
    counters = {k: 0 for k in targets}

    while any(counters[k] < targets[k] for k in targets):
        # Healthy window
        h_dur = random.randint(15, 60)
        h_dur = min(h_dur, targets["Healthy"] - counters["Healthy"])
        if h_dur > 0:
            plan.append(("Healthy", None, h_dur))
            counters["Healthy"] += h_dur

        # Pick a fault class that still needs more rows
        available = [fc for fc in fault_classes if counters[fc] < targets[fc]]
        if not available:
            continue
        fc = random.choice(available)
        profiles = FAULT_PROFILES[fc]
        profile  = random.choice(profiles)
        ep_dur   = random.randint(*_profile_dur_range(fc, profile))
        ep_dur   = min(ep_dur, targets[fc] - counters[fc])
        if ep_dur > 0:
            plan.append((fc, profile, ep_dur))
            counters[fc] += ep_dur

        # Short healthy window after fault (recovery period)
        r_dur = random.randint(8, 25)
        r_dur = min(r_dur, targets["Healthy"] - counters["Healthy"])
        if r_dur > 0:
            plan.append(("Healthy", None, r_dur))
            counters["Healthy"] += r_dur

    return plan


# ── Main generator ────────────────────────────────────────────────────────────

def _compute_ttf_array(plan: list) -> np.ndarray:
    """
    Build the per-sample time_to_breach (lead-time) target over the full plan span.

    For each fault episode the breach index is the plateau start
    (episode_start + onset). Onset-ramp rows count down to it; plateau/recovery
    rows sit at 0 (already breached). Preceding healthy rows count down to the
    next upcoming breach, capped at MAX_LEAD_SECONDS.
    """
    import bisect
    total_span = sum(d for _, _, d in plan)
    ttf = np.full(total_span, MAX_LEAD_SECONDS, dtype=np.float32)
    active = np.zeros(total_span, dtype=bool)
    breach_of = np.full(total_span, -1, dtype=np.int64)
    breaches = []

    idx = 0
    for fault_class, profile, duration in plan:
        start, end = idx, idx + duration
        if fault_class != "Healthy" and duration >= 3:
            if fault_class == "flap":
                onset = 2
            else:
                onset = max(3, int(duration * 0.15))
            # breach = plateau start + sustained-violation window (capped at episode end)
            breach = min(start + onset + SLA_SUSTAIN_SAMPLES, end - 1)
            breaches.append(breach)
            active[start:end] = True
            breach_of[start:end] = breach
        idx = end

    breaches.sort()
    for i in range(total_span):
        if active[i]:
            # onset ramp counts down to breach; plateau/recovery rest at 0
            ttf[i] = max(0, breach_of[i] - i) * DT_SECONDS
        else:
            j = bisect.bisect_left(breaches, i)
            if j < len(breaches):
                ttf[i] = min(MAX_LEAD_SECONDS, (breaches[j] - i) * DT_SECONDS)
            else:
                ttf[i] = MAX_LEAD_SECONDS
    return ttf


def generate(target_rows: int, output_path: str, verbose: bool = True):
    plan = _build_episode_plan(target_rows)
    total_planned = sum(d for _, _, d in plan)
    ttf_arr = _compute_ttf_array(plan)

    if verbose:
        print(f"[*] Target rows: {target_rows:,}")
        print(f"[*] Episodes planned: {len(plan)}")
        print(f"[*] Planned total rows: {total_planned:,}")
        print(f"[*] Output: {output_path}")
        print(f"[*] Generating...")

    states = {node: NodeState(node) for node in NODES}
    frr    = FRRState()

    ts = datetime(2026, 1, 1, 0, 0, 0)
    sample_idx = 0
    written    = 0

    with open(output_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(HEADER)

        batch = []
        FLUSH_EVERY = 5000

        for ep_idx, (fault_class, profile, duration) in enumerate(plan):
            if fault_class == "Healthy":
                for i in range(duration):
                    frr.step(sample_idx + i)
                    d   = _diurnal_factor(sample_idx + i)
                    row = _build_row(ts, "Healthy", "", states, frr, d, {})
                    row.insert(3, round(float(ttf_arr[sample_idx + i]), 1))
                    batch.append(row)
                    ts += timedelta(seconds=DT_SECONDS)
                sample_idx += duration
            else:
                if duration < 3:
                    sample_idx += duration
                    continue
                affected_node = profile[0]
                if fault_class == "flap":
                    reconverge_t  = profile[1]
                    reconverge_s  = reconverge_t // DT_SECONDS
                    frr.trigger_flap(affected_node, reconverge_s, sample_idx)
                    onset = 2; plateau = duration - 2; recovery = 0
                else:
                    onset    = max(3, int(duration * 0.15))
                    plateau  = max(5, int(duration * 0.65))
                    recovery = duration - onset - plateau

                d_factor = _diurnal_factor(sample_idx)
                for i in range(duration):
                    frr.step(sample_idx + i)
                    severity = _ramp(i, onset, plateau, recovery)

                    node_ov = {}
                    for node in NODES:
                        if node == affected_node or _is_downstream(node, affected_node):
                            prop = 1.0 if node == affected_node else 0.4
                            eff  = severity * prop
                            node_ov[node] = _build_node_overrides(
                                fault_class, profile, eff, frr)

                    row = _build_row(ts, fault_class, affected_node, states, frr,
                                     d_factor, node_ov)
                    row.insert(3, round(float(ttf_arr[sample_idx + i]), 1))
                    batch.append(row)
                    ts += timedelta(seconds=DT_SECONDS)
                sample_idx += duration

            if len(batch) >= FLUSH_EVERY:
                writer.writerows(batch)
                written += len(batch)
                batch = []
                if verbose:
                    pct = min(100, 100 * written // target_rows)
                    bar = "█" * (pct // 2) + "░" * (50 - pct // 2)
                    print(f"\r  [{bar}] {written:>7,}/{target_rows:,} rows  ep {ep_idx+1}/{len(plan)}",
                          end="", flush=True)

        if batch:
            writer.writerows(batch)
            written += len(batch)

    if verbose:
        print(f"\r  [{'█'*50}] {written:>7,}/{target_rows:,} rows  done!         ")
        print(f"[+] Written {written:,} rows to {output_path}")
        # Print class distribution
        _print_stats(output_path)


def _print_stats(path: str):
    """Quick class distribution check."""
    counts = {}
    with open(path) as f:
        reader = csv.reader(f)
        next(reader)
        for row in reader:
            lbl = row[1]
            counts[lbl] = counts.get(lbl, 0) + 1
    total = sum(counts.values())
    print(f"\n[*] Class distribution ({total:,} total):")
    for lbl in sorted(counts):
        n = counts[lbl]
        bar = "▓" * int(30 * n / total)
        print(f"    {lbl:<20} {n:>7,}  ({100*n/total:5.1f}%)  {bar}")


# ── CLI ────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Aether synthetic dataset generator")
    parser.add_argument("--rows",  type=int, default=50_000,
                        help="Target number of rows (default: 50000)")
    parser.add_argument("--out",   default=None,
                        help="Output CSV path (default: dataset_large.csv)")
    parser.add_argument("--seed",  type=int, default=42)
    args = parser.parse_args()

    random.seed(args.seed)
    np.random.seed(args.seed)

    out = args.out or os.path.join(os.path.dirname(__file__), "dataset_large.csv")
    generate(args.rows, out)


if __name__ == "__main__":
    main()
