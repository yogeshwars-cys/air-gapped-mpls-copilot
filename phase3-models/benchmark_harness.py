#!/usr/bin/env python3
# =============================================================================
# benchmark_harness.py — Lead-Time Benchmark Harness
#
# Replays recorded scenario CSVs offline and prints per-scenario detection
# lead time: "detected Xs before SLA breach."
#
# Algorithm (pure timestamp arithmetic — no new modeling):
#   1. Load the dataset CSV.
#   2. Identify SLA breach points: first sample where fault_label != 'Healthy'.
#   3. Run the inference pipeline (autoencoder + classifier) on a sliding window
#      over the CSV rows — exactly as it would in production.
#   4. Record the first inference step where anomaly_detected = True.
#   5. Lead time = breach_timestamp - detection_timestamp.
#
# Usage:
#   python3 benchmark_harness.py                          # run on dataset.csv
#   python3 benchmark_harness.py --data dataset.csv       # explicit path
#   python3 benchmark_harness.py --scenario 1             # only Scenario 1 slice
# =============================================================================
import os
import sys
import csv
import argparse
import numpy as np
from collections import deque
from datetime import datetime

try:
    import torch
    import torch.nn as nn
except ImportError:
    print("[-] PyTorch not installed.")
    sys.exit(1)

from predictive_engine import LSTMAutoencoder, LSTMAttentionClassifier, create_sequences
from taxonomy import LABEL_TO_ID, NUM_CLASSES

DEVICE   = torch.device("cuda" if torch.cuda.is_available() else "cpu")
SAVE_DIR = os.path.join(os.path.dirname(__file__), "saved")

# SLA latency thresholds per fault type (ms) — mirrors sla_config.yaml
SLA_BREACH_LABEL_THRESHOLD = {"latency": 150, "loss": 150, "corrupt": 150,
                               "rate": 150, "flap": 150}


def _load_trained_columns() -> list[str]:
    """Return the exact feature columns the trained models expect (from saved/columns.txt)."""
    col_path = os.path.join(SAVE_DIR, "columns.txt")
    if os.path.exists(col_path):
        with open(col_path) as f:
            return [ln.strip() for ln in f if ln.strip()]
    return []


def _load_csv(path: str):
    """Load dataset CSV, filtering to only the columns the trained models expect."""
    trained_cols = _load_trained_columns()

    rows, labels, timestamps = [], [], []
    with open(path) as f:
        reader = csv.reader(f)
        header = next(reader)

    # Determine which header indices correspond to the trained columns
    if trained_cols:
        col_idx = {col: i for i, col in enumerate(header)}
        selected_indices = [col_idx[c] for c in trained_cols if c in col_idx]
        columns = [trained_cols[i] for i, c in enumerate(trained_cols) if c in col_idx]
    else:
        # Fallback: skip meta cols (timestamp, fault_label, fault_location,
        # and time_to_breach if present — it is a target, not a feature)
        meta = {"timestamp", "fault_label", "fault_location", "time_to_breach"}
        selected_indices = [i for i, c in enumerate(header) if c not in meta]
        columns = [header[i] for i in selected_indices]

    with open(path) as f:
        reader = csv.reader(f)
        next(reader)  # skip header
        for row in reader:
            if not row:
                continue
            timestamps.append(row[0])
            labels.append(row[1])
            feats = []
            for idx in selected_indices:
                try:
                    feats.append(float(row[idx]) if idx < len(row) else 0.0)
                except ValueError:
                    feats.append(0.0)
            rows.append(feats)

    X = np.array(rows, dtype=np.float32)
    # Use saved normalisation params if available, else compute from data
    mins_path = os.path.join(SAVE_DIR, "norm_mins.npy")
    maxs_path = os.path.join(SAVE_DIR, "norm_maxs.npy")
    if os.path.exists(mins_path) and os.path.exists(maxs_path):
        mins = np.load(mins_path)
        maxs = np.load(maxs_path)
        if len(mins) == X.shape[1]:
            ranges = maxs - mins; ranges[ranges == 0] = 1.0
            X_norm = np.clip((X - mins) / ranges, 0.0, 1.0)
            return X_norm, labels, timestamps, columns
    mins = X.min(axis=0); maxs = X.max(axis=0)
    ranges = maxs - mins; ranges[ranges == 0] = 1.0
    X_norm = (X - mins) / ranges
    return X_norm, labels, timestamps, columns


def _load_models(num_features: int):
    ae, clf = None, None
    ae_threshold = 0.01

    ae_path = os.path.join(SAVE_DIR, "autoencoder.pt")
    if os.path.exists(ae_path):
        ckpt = torch.load(ae_path, map_location=DEVICE, weights_only=True)
        ae = LSTMAutoencoder(ckpt["seq_len"], ckpt["num_features"], ckpt["hidden_dim"]).to(DEVICE)
        ae.load_state_dict(ckpt["model_state"])
        ae.eval()
        ae_threshold = ckpt.get("threshold", 0.01)

    clf_path = os.path.join(SAVE_DIR, "classifier.pt")
    if os.path.exists(clf_path):
        ckpt = torch.load(clf_path, map_location=DEVICE, weights_only=True)
        clf_nf = ckpt["num_features"]
        clf = LSTMAttentionClassifier(clf_nf, ckpt["hidden_dim"], ckpt["num_classes"]).to(DEVICE)
        clf.load_state_dict(ckpt["model_state"])
        clf.eval()

    return ae, clf, ae_threshold


def _find_scenarios(labels: list[str]) -> list[dict]:
    """
    Identify contiguous fault segments (scenario slices).
    Returns list of {fault, start, end} dicts.
    """
    scenarios = []
    in_fault = False
    for i, lbl in enumerate(labels):
        if lbl != "Healthy" and not in_fault:
            in_fault = True
            seg = {"fault": lbl, "start": i, "end": i}
        elif lbl != "Healthy" and in_fault:
            seg["end"] = i
            seg["fault"] = lbl  # in case it transitions
        elif lbl == "Healthy" and in_fault:
            in_fault = False
            scenarios.append(seg)
    if in_fault:
        scenarios.append(seg)
    return scenarios


def _ts_to_epoch(ts_str: str) -> float:
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(ts_str, fmt).timestamp()
        except ValueError:
            continue
    return 0.0


def run_benchmark(data_path: str, seq_len: int = None):
    print(f"[*] Aether Lead-Time Benchmark Harness")
    print(f"    Data: {data_path}  |  Device: {DEVICE}\n")

    X, labels, timestamps, columns = _load_csv(data_path)
    num_features = X.shape[1]
    ae, clf, ae_threshold = _load_models(num_features)

    if ae is None and clf is None:
        print("[-] No trained models found. Run train_models.py first.")
        sys.exit(1)

    # Derive seq_len from the saved checkpoint if not provided
    if seq_len is None:
        ae_path = os.path.join(SAVE_DIR, "autoencoder.pt")
        if os.path.exists(ae_path):
            ck = torch.load(ae_path, map_location=DEVICE, weights_only=True)
            seq_len = ck.get("seq_len", 20)
        else:
            seq_len = 20

    scenarios = _find_scenarios(labels)
    if not scenarios:
        print("[!] No fault segments found in dataset — nothing to benchmark.")
        return

    # Sample one scenario per fault class for representative results
    seen_faults = set()
    sampled = []
    for s in scenarios:
        if s["fault"] not in seen_faults:
            sampled.append(s)
            seen_faults.add(s["fault"])
    scenarios = sampled

    print(f"  Running {len(scenarios)} scenario(s) (one per fault class):\n")

    results = []
    for si, scen in enumerate(scenarios, 1):
        fault = scen["fault"]
        breach_idx = scen["start"]

        window = deque(maxlen=seq_len)
        detection_idx = None

        for i in range(min(breach_idx + seq_len + 30, len(X))):
            window.append(X[i])
            if len(window) < seq_len:
                continue
            seq = np.array(list(window), dtype=np.float32)
            t_in = torch.FloatTensor(seq).unsqueeze(0).to(DEVICE)

            detected = False
            with torch.no_grad():
                if ae:
                    recon = ae(t_in)
                    ae_loss = nn.functional.mse_loss(recon, t_in).item()
                    if ae_loss > ae_threshold:
                        detected = True
                if not detected and clf:
                    logits, _, _ = clf(t_in)
                    pred_class = logits.argmax(dim=1).item()
                    if pred_class != 0:
                        detected = True

            if detected:
                detection_idx = i
                break

        # Compute lead time
        if detection_idx is not None:
            lead_samples = breach_idx - detection_idx
            ts_breach    = timestamps[breach_idx]    if breach_idx    < len(timestamps) else "?"
            ts_detected  = timestamps[detection_idx] if detection_idx < len(timestamps) else "?"
            breach_epoch   = _ts_to_epoch(ts_breach)
            detected_epoch = _ts_to_epoch(ts_detected)
            lead_seconds = breach_epoch - detected_epoch if breach_epoch and detected_epoch else lead_samples

            if lead_seconds >= 0:
                verdict = f"detected {lead_seconds:.0f}s BEFORE SLA breach  ✓"
            else:
                verdict = f"detected {abs(lead_seconds):.0f}s AFTER breach (late)  ⚠"
        else:
            lead_seconds = None
            verdict = "not detected in replay window  ✗"

        row = {
            "scenario": si,
            "fault": fault,
            "breach_idx": breach_idx,
            "detection_idx": detection_idx,
            "lead_seconds": lead_seconds,
        }
        results.append(row)

        print(f"  Scenario {si}: [{fault:10s}]  breach@idx={breach_idx:4d}  "
              f"detect@idx={str(detection_idx):6s}  → {verdict}")

    # Summary
    detected = [r for r in results if r["lead_seconds"] is not None]
    early    = [r for r in detected if r["lead_seconds"] >= 0]
    avg_lead = np.mean([r["lead_seconds"] for r in early]) if early else 0

    print(f"\n  ─────────────────────────────────────────────────")
    print(f"  Scenarios:   {len(results)}")
    print(f"  Detected:    {len(detected)} / {len(results)}")
    print(f"  Early warn:  {len(early)} / {len(detected)}")
    print(f"  Avg lead:    {avg_lead:.1f}s before SLA breach")
    print(f"  ─────────────────────────────────────────────────\n")
    return results


def main():
    parser = argparse.ArgumentParser(description="Aether Lead-Time Benchmark Harness")
    _default = os.path.join(os.path.dirname(__file__), "dataset_large.csv")
    parser.add_argument("--data", default=_default, help="Dataset CSV path")
    parser.add_argument("--seq-len", type=int, default=30)
    args = parser.parse_args()

    if not os.path.exists(args.data):
        print(f"[-] Dataset not found: {args.data}")
        sys.exit(1)

    run_benchmark(args.data)  # seq_len comes from the saved model checkpoint


if __name__ == "__main__":
    main()
