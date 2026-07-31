#!/usr/bin/env python3
# =============================================================================
# train_models.py — End-to-End Training Pipeline for Aether ML Models
#
# Trains all three models (Autoencoder, Classifier, TTF Regressor) on
# collected telemetry data from data_collector.py.
#
# Usage:
#   python3 train_models.py --data dataset.csv --epochs 100 --seq-len 30
#
# Outputs:
#   phase3-models/saved/autoencoder.pt
#   phase3-models/saved/classifier.pt
#   phase3-models/saved/regressor.pt
# =============================================================================
import os
import sys
import csv
import argparse
import numpy as np
from collections import defaultdict

# Check torch availability early
try:
    import torch
    import torch.nn as nn
    from torch.utils.data import DataLoader, TensorDataset
except ImportError:
    print("[-] PyTorch not installed. Run: pip install torch")
    sys.exit(1)

from predictive_engine import (
    LSTMAutoencoder,
    LSTMAttentionClassifier,
    TimeToFailureRegressor,
    create_sequences
)
from taxonomy import LABEL_TO_ID as FAULT_CLASSES, NUM_CLASSES

DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")
SAVE_DIR = os.path.join(os.path.dirname(__file__), "saved")
HIDDEN_DIM = 128  # Up from 64 — RTX 4060 8GB can handle it


def load_dataset(csv_path):
    print(f"[*] Loading dataset from {csv_path}...")
    rows, labels, ttf_vals = [], [], []
    with open(csv_path, 'r') as f:
        reader = csv.reader(f)
        header = next(reader)
        # Datasets generated after the lead-time fix carry a "time_to_breach"
        # regression target at column 3. Older datasets do not — stay compatible.
        has_ttf = len(header) > 3 and header[3] == "time_to_breach"
        feat_start = 4 if has_ttf else 3
        columns = header[feat_start:]
        num_features = len(columns)

        for row in reader:
            if len(row) < feat_start + 1:
                continue
            fault_label = row[1]
            label_int = FAULT_CLASSES.get(fault_label, 0)
            labels.append(label_int)
            if has_ttf:
                try:
                    ttf_vals.append(float(row[3]))
                except (ValueError, IndexError):
                    ttf_vals.append(0.0)
            features = []
            for val in row[feat_start:feat_start + num_features]:
                try:
                    features.append(float(val))
                except (ValueError, IndexError):
                    features.append(0.0)
            while len(features) < num_features:
                features.append(0.0)
            rows.append(features)

    X = np.array(rows, dtype=np.float32)
    y = np.array(labels, dtype=np.int64)
    ttf = np.array(ttf_vals, dtype=np.float32) if has_ttf else None
    print(f"    Loaded {X.shape[0]} samples × {X.shape[1]} features")
    if ttf is not None:
        print(f"    time_to_breach target present: min={ttf.min():.0f}s max={ttf.max():.0f}s mean={ttf.mean():.1f}s")
    dist = {k: int((y == v).sum()) for k, v in FAULT_CLASSES.items()}
    print(f"    Class distribution: {dist}")
    return X, y, columns, ttf


def normalize(X):
    mins = X.min(axis=0)
    maxs = X.max(axis=0)
    ranges = maxs - mins
    ranges[ranges == 0] = 1.0
    return (X - mins) / ranges, mins, maxs


def augment_sequences(X_seq, y_seq, target_per_class=150, noise_std=0.02):
    """
    Augment at the SEQUENCE level — add noise to complete temporal windows.
    Must be called AFTER create_sequences so each augmented sample is a valid
    temporal sequence, not a shuffled mix of timesteps from different fault states.
    """
    X_aug, y_aug = [X_seq], [y_seq]
    class_counts = {c: int((y_seq == c).sum()) for c in np.unique(y_seq)}

    print(f"[*] Augmenting minority sequences (target: {target_per_class} each)...")
    for class_id, count in class_counts.items():
        if class_id == 0:
            continue
        if count < target_per_class:
            n_needed = target_per_class - count
            class_seqs = X_seq[y_seq == class_id]
            indices = np.random.choice(len(class_seqs), n_needed, replace=True)
            noise = np.random.normal(0, noise_std, class_seqs[indices].shape).astype(np.float32)
            augmented = np.clip(class_seqs[indices] + noise, 0.0, 1.0)
            X_aug.append(augmented)
            y_aug.append(np.full(n_needed, class_id, dtype=np.int64))
            print(f"    Class {class_id}: {count} → {count + n_needed}")

    X_out = np.vstack(X_aug)
    y_out = np.concatenate(y_aug)
    perm = np.random.permutation(len(X_out))
    return X_out[perm], y_out[perm]


def stratified_split(X, y, val_ratio=0.2):
    """Stratified train/val split — each class is proportionally represented in val."""
    class_indices = defaultdict(list)
    for i, label in enumerate(y):
        class_indices[int(label)].append(i)

    train_idx, val_idx = [], []
    for label, indices in class_indices.items():
        arr = np.array(indices)
        np.random.shuffle(arr)
        n_val = max(1, int(len(arr) * val_ratio))
        val_idx.extend(arr[:n_val].tolist())
        train_idx.extend(arr[n_val:].tolist())

    return (X[train_idx], y[train_idx]), (X[val_idx], y[val_idx])


def compute_class_weights(y):
    """Inverse-frequency class weights to penalise majority class less."""
    counts = np.bincount(y, minlength=NUM_CLASSES).astype(np.float32)
    counts[counts == 0] = 1.0
    weights = 1.0 / counts
    weights = weights / weights.sum() * NUM_CLASSES
    return torch.FloatTensor(weights).to(DEVICE)


def train_autoencoder(X_healthy, seq_len, epochs, batch_size, lr):
    print(f"\n{'='*60}")
    print(f"[*] Training LSTM Autoencoder (healthy data only)")
    print(f"    Samples: {len(X_healthy)}, SeqLen: {seq_len}, Epochs: {epochs}, hidden={HIDDEN_DIM}")

    num_features = X_healthy.shape[1]
    dummy_labels = np.zeros(len(X_healthy))
    X_seq, _ = create_sequences(X_healthy, dummy_labels, seq_len)

    tensor_X = torch.FloatTensor(X_seq).to(DEVICE)
    dataset = TensorDataset(tensor_X, tensor_X)
    loader = DataLoader(dataset, batch_size=batch_size, shuffle=True)

    model = LSTMAutoencoder(seq_len, num_features, hidden_dim=HIDDEN_DIM).to(DEVICE)
    optimizer = torch.optim.Adam(model.parameters(), lr=lr)
    scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer, patience=5, factor=0.5)
    criterion = nn.MSELoss()

    best_loss = float('inf')
    best_state = None

    model.train()
    for epoch in range(epochs):
        total_loss = 0.0
        for batch_x, batch_target in loader:
            optimizer.zero_grad()
            reconstructed = model(batch_x)
            loss = criterion(reconstructed, batch_target)
            loss.backward()
            optimizer.step()
            total_loss += loss.item()

        avg_loss = total_loss / len(loader)
        scheduler.step(avg_loss)

        if avg_loss < best_loss:
            best_loss = avg_loss
            best_state = {k: v.clone() for k, v in model.state_dict().items()}

        if (epoch + 1) % 10 == 0 or epoch == 0:
            print(f"    Epoch {epoch+1}/{epochs} — Loss: {avg_loss:.6f} | LR: {optimizer.param_groups[0]['lr']:.2e}")

    model.load_state_dict(best_state)
    os.makedirs(SAVE_DIR, exist_ok=True)
    save_path = os.path.join(SAVE_DIR, "autoencoder.pt")
    torch.save({
        "model_state": model.state_dict(),
        "seq_len": seq_len,
        "num_features": num_features,
        "hidden_dim": HIDDEN_DIM,
        "threshold": best_loss * 3.0
    }, save_path)
    print(f"    [+] Saved (best loss: {best_loss:.6f}, threshold: {best_loss * 3.0:.6f})")
    return model, best_loss


def train_classifier(X_all, y_all, seq_len, epochs, batch_size, lr):
    print(f"\n{'='*60}")
    print(f"[*] Training LSTM Attention Classifier (supervised)")
    print(f"    Samples before aug: {len(X_all)}, Classes: {NUM_CLASSES}, hidden={HIDDEN_DIM}")

    # Create sequences FIRST (preserves temporal continuity), then augment
    num_features = X_all.shape[1]
    X_seq, y_seq = create_sequences(X_all, y_all, seq_len)

    # Augment at sequence level — each augmented sample is a valid temporal window
    X_seq, y_seq = augment_sequences(X_seq, y_seq, target_per_class=150)
    print(f"    Sequences after aug: {len(X_seq)}")

    # Stratified split
    (X_train, y_train), (X_val, y_val) = stratified_split(X_seq, y_seq, val_ratio=0.2)
    print(f"    Train: {len(X_train)}, Val: {len(X_val)}")

    class_weights = compute_class_weights(y_train)
    print(f"    Class weights: {class_weights.cpu().numpy().round(3)}")

    train_dataset = TensorDataset(
        torch.FloatTensor(X_train).to(DEVICE),
        torch.LongTensor(y_train).to(DEVICE)
    )
    val_dataset = TensorDataset(
        torch.FloatTensor(X_val).to(DEVICE),
        torch.LongTensor(y_val).to(DEVICE)
    )
    train_loader = DataLoader(train_dataset, batch_size=batch_size, shuffle=True)
    val_loader = DataLoader(val_dataset, batch_size=batch_size, shuffle=False)

    model = LSTMAttentionClassifier(num_features, hidden_dim=HIDDEN_DIM, num_classes=NUM_CLASSES).to(DEVICE)
    optimizer = torch.optim.Adam(model.parameters(), lr=lr, weight_decay=1e-4)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=epochs, eta_min=1e-5)
    criterion = nn.CrossEntropyLoss(weight=class_weights)

    best_val_acc = 0.0
    best_state = None

    for epoch in range(epochs):
        model.train()
        total_loss, correct, total = 0.0, 0, 0
        for batch_x, batch_y in train_loader:
            optimizer.zero_grad()
            logits, _, _ = model(batch_x)
            loss = criterion(logits, batch_y)
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)
            optimizer.step()
            total_loss += loss.item()
            preds = logits.argmax(dim=1)
            correct += (preds == batch_y).sum().item()
            total += batch_y.size(0)

        train_acc = correct / max(total, 1)

        model.eval()
        val_correct, val_total = 0, 0
        with torch.no_grad():
            for batch_x, batch_y in val_loader:
                logits, _, _ = model(batch_x)
                preds = logits.argmax(dim=1)
                val_correct += (preds == batch_y).sum().item()
                val_total += batch_y.size(0)
        val_acc = val_correct / max(val_total, 1)
        scheduler.step()

        if val_acc > best_val_acc:
            best_val_acc = val_acc
            best_state = {k: v.clone() for k, v in model.state_dict().items()}

        if (epoch + 1) % 10 == 0 or epoch == 0:
            print(f"    Epoch {epoch+1}/{epochs} — Loss: {total_loss/len(train_loader):.4f} | "
                  f"Train: {train_acc:.2%} | Val: {val_acc:.2%} | "
                  f"Best Val: {best_val_acc:.2%} | LR: {optimizer.param_groups[0]['lr']:.2e}")

    model.load_state_dict(best_state)
    save_path = os.path.join(SAVE_DIR, "classifier.pt")
    torch.save({
        "model_state": model.state_dict(),
        "seq_len": seq_len,
        "num_features": num_features,
        "hidden_dim": HIDDEN_DIM,
        "num_classes": NUM_CLASSES,
        "fault_classes": FAULT_CLASSES,
        "best_val_acc": best_val_acc
    }, save_path)
    print(f"    [+] Saved best checkpoint (val accuracy: {best_val_acc:.2%})")
    return model


def train_regressor(X_all, y_all, seq_len, epochs, batch_size, lr, ttf_col=None):
    print(f"\n{'='*60}")
    print(f"[*] Training Time-To-Failure Regressor, hidden={HIDDEN_DIM}")

    num_features = X_all.shape[1]
    if ttf_col is not None:
        # Use the generator's lead-time target: seconds until the upcoming SLA
        # breach (fault plateau), counting down through the pre-fault healthy
        # window and the onset ramp. This is the real "lead time" signal.
        ttf_labels = ttf_col.astype(np.float32)
        print(f"    Using dataset time_to_breach target — "
              f"median={np.median(ttf_labels):.0f}s, "
              f"pct>0={(ttf_labels > 0).mean()*100:.0f}%")
    else:
        # Backward-compatible fallback for datasets without time_to_breach:
        # distance to the next fault row (collapses to ~0 on dense-fault data).
        print("    [!] No time_to_breach column — deriving TTF from label gaps (legacy)")
        ttf_labels = np.full(len(y_all), 300.0, dtype=np.float32)
        next_fault_at = len(y_all) + 300
        for i in range(len(y_all) - 1, -1, -1):
            if y_all[i] != 0:
                next_fault_at = i
            ttf_labels[i] = min(float(next_fault_at - i), 300.0)

    X_seq, ttf_seq = create_sequences(X_all, ttf_labels, seq_len)

    tensor_X = torch.FloatTensor(X_seq).to(DEVICE)
    tensor_y = torch.FloatTensor(ttf_seq).unsqueeze(1).to(DEVICE)
    dataset = TensorDataset(tensor_X, tensor_y)
    loader = DataLoader(dataset, batch_size=batch_size, shuffle=True)

    model = TimeToFailureRegressor(num_features, hidden_dim=HIDDEN_DIM).to(DEVICE)
    optimizer = torch.optim.Adam(model.parameters(), lr=lr, weight_decay=1e-4)
    scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer, patience=8, factor=0.5)
    criterion = nn.HuberLoss(delta=30.0)  # Huber is more robust than MSE for TTF outliers

    best_loss = float('inf')
    best_state = None

    model.train()
    for epoch in range(epochs):
        total_loss = 0.0
        for batch_x, batch_y in loader:
            optimizer.zero_grad()
            pred = model(batch_x)
            loss = criterion(pred, batch_y)
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)
            optimizer.step()
            total_loss += loss.item()

        avg_loss = total_loss / len(loader)
        scheduler.step(avg_loss)

        if avg_loss < best_loss:
            best_loss = avg_loss
            best_state = {k: v.clone() for k, v in model.state_dict().items()}

        if (epoch + 1) % 10 == 0 or epoch == 0:
            # Batched RMSE to avoid OOM on large datasets
            sq_err_sum, n_total = 0.0, 0
            with torch.no_grad():
                for i in range(0, len(tensor_X), batch_size * 4):
                    bx = tensor_X[i:i + batch_size * 4]
                    by = tensor_y[i:i + batch_size * 4]
                    preds = model(bx)
                    sq_err_sum += nn.functional.mse_loss(preds, by, reduction="sum").item()
                    n_total += by.size(0)
            mse = sq_err_sum / max(n_total, 1)
            print(f"    Epoch {epoch+1}/{epochs} — Huber: {avg_loss:.4f} | RMSE: {mse**0.5:.2f}s | LR: {optimizer.param_groups[0]['lr']:.2e}")

    model.load_state_dict(best_state)
    save_path = os.path.join(SAVE_DIR, "regressor.pt")
    torch.save({
        "model_state": model.state_dict(),
        "seq_len": seq_len,
        "num_features": num_features,
        "hidden_dim": HIDDEN_DIM,
    }, save_path)
    print(f"    [+] Saved best checkpoint")
    return model


def generate_synthetic_dataset(output_path, num_samples=2000):
    print(f"[*] Generating synthetic training dataset ({num_samples} samples)...")
    np.random.seed(42)

    feature_names = [
        "pe1_eth1_rx_bytes", "pe1_eth1_tx_bytes",
        "pe1_eth1_rx_drops", "pe1_eth1_tx_drops",
        "p1_eth1_rx_bytes", "p1_eth1_tx_bytes",
        "pe1_frr_ospf_neighbors_total", "pe1_frr_ldp_session_operational",
        "pe1_frr_bgp_vpn_established", "pe1_frr_bgp_vpn_prefixes_received",
        "pe2_frr_bgp_vpn_established", "pe2_frr_bgp_vpn_prefixes_received"
    ]
    rate_names = [f"{n}_rate" for n in feature_names[:6]]
    header = ["timestamp", "fault_label", "fault_location"] + feature_names + rate_names

    with open(output_path, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(header)

        for i in range(num_samples):
            ts = f"2026-06-27T19:{i//60:02d}:{i%60:02d}"
            fault_label, fault_loc = "Healthy", ""

            if 400 <= i < 500:
                fault_label, fault_loc = "latency", "pe1_eth1"
            elif 800 <= i < 900:
                fault_label, fault_loc = "loss", "pe1_eth1"
            elif 1200 <= i < 1250:
                fault_label, fault_loc = "flap", "p1_eth1"
            elif 1600 <= i < 1700:
                fault_label, fault_loc = "rate", "pe1_eth1"
            elif 1900 <= i < 1950:
                fault_label, fault_loc = "corrupt", "pe2_eth1"

            rx_bytes = 5_000_000 + np.random.normal(0, 200_000)
            tx_bytes = 4_500_000 + np.random.normal(0, 180_000)
            rx_drops = max(0, np.random.normal(0, 1))
            tx_drops = max(0, np.random.normal(0, 0.5))
            p1_rx = 9_000_000 + np.random.normal(0, 300_000)
            p1_tx = 8_500_000 + np.random.normal(0, 280_000)
            ospf_nbrs, ldp_oper = 2.0, 1.0
            bgp_est_1, bgp_pfx_1, bgp_est_2, bgp_pfx_2 = 1.0, 2.0, 1.0, 2.0

            if fault_label == "latency":
                progress = (i - 400) / 100.0
                rx_drops += progress * 5; tx_drops += progress * 3
            elif fault_label == "loss":
                rx_drops += np.random.uniform(5, 20); tx_drops += np.random.uniform(3, 15)
                rx_bytes *= 0.85
            elif fault_label == "flap":
                if np.random.random() > 0.5:
                    ospf_nbrs, ldp_oper, bgp_est_1, bgp_pfx_1 = 1.0, 0.0, 0.0, 0.0
                rx_bytes *= 0.3; tx_bytes *= 0.3
            elif fault_label == "rate":
                rx_bytes = min(rx_bytes, 1_000_000); tx_bytes = min(tx_bytes, 1_000_000)
            elif fault_label == "corrupt":
                rx_drops += np.random.uniform(2, 10)

            features = [rx_bytes, tx_bytes, rx_drops, tx_drops, p1_rx, p1_tx,
                        ospf_nbrs, ldp_oper, bgp_est_1, bgp_pfx_1, bgp_est_2, bgp_pfx_2]
            rates = [abs(np.random.normal(0, f * 0.01)) for f in features[:6]]
            row = [ts, fault_label, fault_loc] + [f"{v:.2f}" for v in features] + [f"{v:.2f}" for v in rates]
            writer.writerow(row)

    print(f"    [+] Synthetic dataset written to {output_path}")


def main():
    parser = argparse.ArgumentParser(description="Aether ML Training Pipeline")
    _default_data = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dataset.csv")
    parser.add_argument("--data", default=_default_data, help="Path to training CSV")
    parser.add_argument("--epochs", type=int, default=100, help="Training epochs (default: 100)")
    parser.add_argument("--seq-len", type=int, default=30, help="Sequence length (default: 30)")
    parser.add_argument("--batch-size", type=int, default=32, help="Batch size (default: 32)")
    parser.add_argument("--lr", type=float, default=0.001, help="Learning rate (default: 0.001)")
    parser.add_argument("--synthetic", action="store_true", help="Generate and train on synthetic data")
    parser.add_argument("--no-augment", action="store_true", help="Skip minority class augmentation")
    parser.add_argument("--classifier-only", action="store_true", help="Retrain only the classifier (skip autoencoder + regressor)")
    parser.add_argument("--regressor-only", action="store_true", help="Retrain only the TTF regressor (skip autoencoder + classifier)")
    args = parser.parse_args()

    data_path = args.data
    if args.synthetic or not os.path.exists(data_path):
        if not args.synthetic:
            print(f"[!] Dataset not found. Generating synthetic data...")
        generate_synthetic_dataset(data_path)

    X, y, columns, ttf_col = load_dataset(data_path)

    if len(X) < args.seq_len + 10:
        print(f"[-] Not enough data ({len(X)} samples). Need {args.seq_len + 10} minimum.")
        sys.exit(1)

    X_norm, mins, maxs = normalize(X)

    os.makedirs(SAVE_DIR, exist_ok=True)
    np.save(os.path.join(SAVE_DIR, "norm_mins.npy"), mins)
    np.save(os.path.join(SAVE_DIR, "norm_maxs.npy"), maxs)
    with open(os.path.join(SAVE_DIR, "columns.txt"), 'w') as f:
        f.write('\n'.join(columns))

    print(f"\n[*] Device: {DEVICE}")
    if DEVICE.type == "cuda":
        print(f"    GPU: {torch.cuda.get_device_name(0)}")
        print(f"    VRAM: {torch.cuda.get_device_properties(0).total_memory / 1e9:.1f} GB")

    if args.regressor_only:
        # Retrain only the TTF regressor (e.g. after the lead-time label fix)
        train_regressor(X_norm, y, args.seq_len, args.epochs, args.batch_size, args.lr, ttf_col=ttf_col)
        print(f"\n[+] Regressor retrained → {SAVE_DIR}/regressor.pt")
        return

    if not args.classifier_only:
        # 1. Autoencoder — healthy data only
        healthy_mask = (y == 0)
        X_healthy = X_norm[healthy_mask]
        if len(X_healthy) > args.seq_len:
            train_autoencoder(X_healthy, args.seq_len, args.epochs, args.batch_size, args.lr)
        else:
            print("[!] Not enough healthy samples for autoencoder.")

    # 2. Classifier — all data, with augmentation
    train_classifier(X_norm, y, args.seq_len, args.epochs, args.batch_size, args.lr)

    if not args.classifier_only:
        # 3. TTF Regressor
        train_regressor(X_norm, y, args.seq_len, args.epochs, args.batch_size, args.lr, ttf_col=ttf_col)

    print(f"\n{'='*60}")
    print(f"[+] All models saved to {SAVE_DIR}/")
    print(f"    autoencoder.pt  — Unsupervised anomaly detection")
    print(f"    classifier.pt   — Supervised fault classification")
    print(f"    regressor.pt    — Time-to-failure regression")


if __name__ == "__main__":
    main()
