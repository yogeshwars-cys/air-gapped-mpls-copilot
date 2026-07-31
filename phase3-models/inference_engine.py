#!/usr/bin/env python3
# =============================================================================
# inference_engine.py — Live Inference Pipeline for Project Aether
#
# Loads trained models and runs real-time inference on incoming telemetry.
# Produces Anomaly Context Packets (ACPs) with predictions, corroboration,
# and mitigation commands.
#
# Usage:
#   python3 inference_engine.py                  # Runs continuous inference
#   python3 inference_engine.py --single-shot    # Runs one inference cycle
# =============================================================================
import os
import sys
import time
import json
import argparse
import numpy as np
from datetime import datetime
from collections import deque

try:
    import torch
except ImportError:
    print("[-] PyTorch not installed. Run: pip install torch")
    sys.exit(1)

from predictive_engine import (
    LSTMAutoencoder,
    LSTMAttentionClassifier,
    TimeToFailureRegressor
)
from acp_manager import AnomalyContextPacket
from graph_model import ClonalGraphEngine
from taxonomy import info_for_id, policy_for_action

# Try importing data_collector for live scraping
try:
    from data_collector import scrape_once, flatten_metrics
    HAS_COLLECTOR = True
except ImportError:
    HAS_COLLECTOR = False

# Digital Twin (optional — degrades gracefully if statsmodels not installed)
try:
    from digital_twin import DigitalTwin
    from trend_forecaster import TrendForecaster
    HAS_TWIN = True
except ImportError:
    HAS_TWIN = False

DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")
SAVE_DIR = os.path.join(os.path.dirname(__file__), "saved")
ACP_LOG_DIR = os.path.join(os.path.dirname(__file__), "acp_logs")


class EMAThreshold:
    """
    Self-calibrating anomaly threshold using exponential moving average.
    Tracks EMA mean and variance of reconstruction loss; threshold =
    ema_mean + k * ema_std (Bollinger-band style).  Replaces the fixed
    3× multiplier used in v3.
    """
    def __init__(self, alpha=0.05, k=3.0, warmup=50):
        self.alpha = alpha      # EMA decay (smaller = longer memory)
        self.k = k              # std multiplier
        self.warmup = warmup    # samples before EMA kicks in
        self._ema_mean = None
        self._ema_var = None
        self._n = 0
        self._static_threshold = None  # fallback from checkpoint

    def set_static(self, v):
        self._static_threshold = v

    def update(self, loss):
        self._n += 1
        if self._ema_mean is None:
            self._ema_mean = loss
            self._ema_var = 0.0
        else:
            delta = loss - self._ema_mean
            self._ema_mean += self.alpha * delta
            self._ema_var = (1 - self.alpha) * (self._ema_var + self.alpha * delta ** 2)

    @property
    def threshold(self):
        if self._n < self.warmup or self._ema_var is None:
            return self._static_threshold or 0.01
        return self._ema_mean + self.k * (self._ema_var ** 0.5)

    def is_anomaly(self, loss):
        self.update(loss)
        return loss > self.threshold


class AetherInferenceEngine:
    def __init__(self):
        self.autoencoder = None
        self.classifier = None
        self.regressor = None
        self.ema_threshold = EMAThreshold(alpha=0.05, k=3.0, warmup=50)
        self.seq_len = 30
        self.num_features = 0
        self.norm_mins = None
        self.norm_maxs = None
        self.columns = []
        self.graph_engine = ClonalGraphEngine()
        self.sliding_window = deque(maxlen=60)  # Keep 60s of history
        self.acp_count = 0
        # Digital Twin + trend forecasting (wired in post-load)
        self.trend_forecaster = TrendForecaster() if HAS_TWIN else None
        self.digital_twin = DigitalTwin(self.graph_engine, self.trend_forecaster) if HAS_TWIN else None
        self._twin_channel_cols: dict = {}  # populated after columns are known

    def load_models(self):
        """Load all trained model checkpoints."""
        print(f"[*] Loading models from {SAVE_DIR}/ on {DEVICE}...")

        # Load Autoencoder
        ae_path = os.path.join(SAVE_DIR, "autoencoder.pt")
        if os.path.exists(ae_path):
            checkpoint = torch.load(ae_path, map_location=DEVICE, weights_only=True)
            self.seq_len = checkpoint["seq_len"]
            self.num_features = checkpoint["num_features"]
            self.ema_threshold.set_static(checkpoint.get("threshold", 0.01))
            self.autoencoder = LSTMAutoencoder(
                self.seq_len, self.num_features, checkpoint["hidden_dim"]
            ).to(DEVICE)
            self.autoencoder.load_state_dict(checkpoint["model_state"])
            self.autoencoder.eval()
            print(f"    ✓ Autoencoder loaded (threshold: {self.ema_threshold.threshold:.6f})")
        else:
            print(f"    ✗ Autoencoder not found at {ae_path}")

        # Load Classifier
        clf_path = os.path.join(SAVE_DIR, "classifier.pt")
        if os.path.exists(clf_path):
            checkpoint = torch.load(clf_path, map_location=DEVICE, weights_only=True)
            self.classifier = LSTMAttentionClassifier(
                checkpoint["num_features"], checkpoint["hidden_dim"], checkpoint["num_classes"]
            ).to(DEVICE)
            self.classifier.load_state_dict(checkpoint["model_state"])
            self.classifier.eval()
            print(f"    ✓ Classifier loaded (val acc: {checkpoint.get('best_val_acc', 0):.2%})")
        else:
            print(f"    ✗ Classifier not found at {clf_path}")

        # Load Regressor
        reg_path = os.path.join(SAVE_DIR, "regressor.pt")
        if os.path.exists(reg_path):
            checkpoint = torch.load(reg_path, map_location=DEVICE, weights_only=True)
            self.regressor = TimeToFailureRegressor(
                checkpoint["num_features"], checkpoint["hidden_dim"]
            ).to(DEVICE)
            self.regressor.load_state_dict(checkpoint["model_state"])
            self.regressor.eval()
            print(f"    ✓ TTF Regressor loaded")
        else:
            print(f"    ✗ Regressor not found at {reg_path}")

        # Load normalization params
        mins_path = os.path.join(SAVE_DIR, "norm_mins.npy")
        maxs_path = os.path.join(SAVE_DIR, "norm_maxs.npy")
        cols_path = os.path.join(SAVE_DIR, "columns.txt")

        if os.path.exists(mins_path):
            self.norm_mins = np.load(mins_path)
            self.norm_maxs = np.load(maxs_path)
            with open(cols_path) as f:
                self.columns = [line.strip() for line in f.readlines()]
            print(f"    ✓ Normalization params loaded ({len(self.columns)} features)")
        else:
            print(f"    ✗ Normalization params not found — will use raw values")

    def normalize_sample(self, features):
        """Normalize a single sample using saved min/max."""
        if self.norm_mins is None:
            return features
        ranges = self.norm_maxs - self.norm_mins
        ranges[ranges == 0] = 1.0
        return (features - self.norm_mins) / ranges

    def ingest_sample(self, flat_metrics):
        """
        Ingests a flat metrics dict (from scrape_once) into the sliding window.
        Aligns columns to match training order.
        """
        if not self.columns:
            # First time — just use whatever columns arrive
            self.columns = sorted(flat_metrics.keys())
            self.num_features = len(self.columns)

        feature_vec = np.zeros(len(self.columns), dtype=np.float32)
        for i, col in enumerate(self.columns):
            feature_vec[i] = flat_metrics.get(col, 0.0)

        feature_vec = self.normalize_sample(feature_vec)
        self.sliding_window.append(feature_vec)

        # Feed raw (un-normalized) values to trend forecaster for digital twin
        if self.trend_forecaster:
            raw_vec = np.zeros(len(self.columns), dtype=np.float32)
            for i, col in enumerate(self.columns):
                raw_vec[i] = flat_metrics.get(col, 0.0)
            self.trend_forecaster.update_batch(
                {col: float(raw_vec[i]) for i, col in enumerate(self.columns)}
            )

    def run_inference(self):
        """
        Runs all three models on the current sliding window.
        Returns an ACP with all predictions and corroboration results.
        """
        if len(self.sliding_window) < self.seq_len:
            return None  # Not enough data yet

        # Extract the latest sequence
        window = np.array(list(self.sliding_window))[-self.seq_len:]
        tensor_input = torch.FloatTensor(window).unsqueeze(0).to(DEVICE)  # [1, SeqLen, Features]

        acp = AnomalyContextPacket(trigger_source="ML_ENGINE")

        # 1. Autoencoder Anomaly Detection
        ae_loss = 0.0
        ae_anomaly = False
        if self.autoencoder:
            with torch.no_grad():
                reconstructed = self.autoencoder(tensor_input)
                ae_loss = torch.nn.functional.mse_loss(reconstructed, tensor_input).item()
                ae_anomaly = self.ema_threshold.is_anomaly(ae_loss)

        # 2. Fault Classification
        fault_class = "Healthy"
        action = "NO_ACTION"
        confidence = 1.0
        attn_weights = None
        feature_weights = None
        if self.classifier:
            with torch.no_grad():
                logits, attn, feat_w = self.classifier(tensor_input)
                probs = torch.softmax(logits, dim=1)
                class_id = probs.argmax(dim=1).item()
                confidence = probs[0, class_id].item()
                info = info_for_id(class_id)
                fault_class = info["display"]
                action = info["action"]
                attn_weights = attn.squeeze().cpu().numpy()
                feature_weights = feat_w.squeeze().cpu().numpy()

        # 3. Time-to-Failure Estimation
        ttf = -1.0
        if self.regressor:
            with torch.no_grad():
                ttf_pred = self.regressor(tensor_input)
                ttf = max(0.0, ttf_pred.item())

        # Attach attention heatmap top-5 features to ACP (read-only explainability)
        if feature_weights is not None and self.columns:
            top5_idx = feature_weights.argsort()[-5:][::-1]
            top5_names = [self.columns[i] for i in top5_idx if i < len(self.columns)]
            acp.set_top_features(top5_names)

        # Set ML results in ACP
        ml_detected = ae_anomaly or fault_class != "Healthy"
        acp.set_ml_results(
            detected=ml_detected,
            loss=ae_loss,
            fault_class=fault_class,
            confidence=confidence,
            ttf=ttf
        )

        # 4. Graph Model Corroboration
        # Extract telemetry signal for graph update from latest raw values
        latest = list(self.sliding_window)[-1]
        # Simple heuristic: check if drops or errors are elevated
        drop_indices = [i for i, c in enumerate(self.columns) if "drop" in c or "error" in c]
        elevated_drops = any(latest[i] > 0.5 for i in drop_indices if i < len(latest))

        # Run clonal search
        degraded_link = None
        if ml_detected:
            degraded_link = ("pe1", "p1")  # Default degraded link assumption
        winner, results = self.graph_engine.run_clonal_search(degraded_link)

        graph_detects = len(results.get("CLONE_BASELINE", {}).get("bottlenecks", [])) > 0

        bottlenecks = results.get(winner, {}).get("bottlenecks", [])
        delays = results.get(winner, {}).get("delays", {})
        acp.set_graph_results(bottlenecks, list(delays.keys()))

        # 5. Corroboration Logic
        engines_agree = ml_detected == (graph_detects or elevated_drops)

        policy = policy_for_action(action)
        recommended_action = action
        execution_mode = "RECOMMEND_ONLY"
        rationale = ""

        if not ml_detected:
            rationale = "System healthy. All models nominal."
            recommended_action = "NO_ACTION"
        elif not engines_agree:
            rationale = (f"SAFETY TRIP: ML predicts {fault_class} (conf={confidence:.2f}) "
                         f"but Graph model projects no queue violations. Downgrading to RECOMMEND_ONLY.")
        else:
            if confidence >= policy["min_conf"] and policy["auto_execute"]:
                execution_mode = "AUTO_EXECUTE"
                rationale = (f"Dual-model corroboration confirmed. {fault_class} detected "
                             f"with conf={confidence:.2f} (threshold={policy['min_conf']}). "
                             f"Autonomy policy authorizes {recommended_action}.")
            else:
                rationale = (f"Models agree on {fault_class}, but confidence={confidence:.2f} "
                             f"{'below threshold' if confidence < policy['min_conf'] else 'or action requires operator approval'}.")

        acp.set_corroboration(engines_agree, rationale, recommended_action, execution_mode)

        # 6. Digital Twin Divergence (observability — does not gate corroboration)
        if self.digital_twin and self.trend_forecaster:
            try:
                # Feed current raw metrics to the digital twin's forecaster
                latest_metrics = {col: float(list(self.sliding_window)[-1][i])
                                  for i, col in enumerate(self.columns)}
                self.digital_twin.update(latest_metrics)
                div_result = self.digital_twin.evaluate()
                if div_result is not None:
                    acp.set_digital_twin_divergence(div_result.divergence)
            except Exception:
                pass  # twin never blocks inference

        return acp

    def log_acp(self, acp):
        """Saves ACP to disk for IKB (Incident Knowledge Base)."""
        os.makedirs(ACP_LOG_DIR, exist_ok=True)
        filepath = os.path.join(ACP_LOG_DIR, f"acp_{acp.acp_id[:8]}_{datetime.now().strftime('%H%M%S')}.json")
        acp.write_to_file(filepath)
        self.acp_count += 1

    def print_acp_summary(self, acp):
        """Prints a human-readable summary of the ACP."""
        ml = acp.ml_analysis
        corr = acp.corroboration

        severity_colors = {"LOW": "🟢", "MEDIUM": "🟡", "HIGH": "🟠", "CRITICAL": "🔴"}
        icon = severity_colors.get(acp.severity, "⚪")

        print(f"\n{'─'*70}")
        print(f"  {icon} ACP #{self.acp_count} | {acp.timestamp} | Severity: {acp.severity}")
        print(f"  ML Prediction: {ml['predicted_fault_class']} (conf={ml['confidence_score']:.2%})")
        print(f"  Reconstruction Loss: {ml['reconstruction_loss']:.6f} | TTF: {ml['estimated_time_to_failure_sec']:.1f}s")
        print(f"  Corroboration: {'✅ AGREE' if corr['engines_agree'] else '⚠️  DISAGREE'}")
        print(f"  Execution: {corr['execution_mode']}")
        print(f"  Action: {corr['recommended_action']}")
        print(f"  Rationale: {corr['rationale']}")
        print(f"{'─'*70}")


def run_continuous(engine, interval=1.0):
    """Continuously scrapes telemetry, runs inference, and logs ACPs."""
    if not HAS_COLLECTOR:
        print("[-] data_collector.py not importable. Cannot scrape live metrics.")
        print("[!] Run with --demo to test with synthetic data instead.")
        return

    print(f"\n[*] Starting continuous inference loop (interval={interval}s)...")
    print(f"    Waiting for {engine.seq_len} samples to fill sliding window...\n")

    sample_count = 0
    while True:
        metrics = scrape_once()
        if "error" in metrics:
            print(f"  [!] Scrape error: {metrics['error']}")
            time.sleep(interval)
            continue

        engine.ingest_sample(metrics)
        sample_count += 1

        if sample_count >= engine.seq_len:
            acp = engine.run_inference()
            if acp:
                if acp.ml_analysis["anomaly_detected"]:
                    engine.print_acp_summary(acp)
                    engine.log_acp(acp)
                elif sample_count % 30 == 0:
                    print(f"  [{sample_count}] System healthy | Window: {len(engine.sliding_window)}/{engine.sliding_window.maxlen}")

        time.sleep(interval)


def run_demo(engine):
    """Runs inference on synthetic data to demonstrate the pipeline."""
    print("\n[*] Running demo inference with synthetic data...\n")
    np.random.seed(42)

    num_features = engine.num_features or 12

    # Simulate 60 samples: 40 healthy + 20 degrading
    for i in range(60):
        if i < 40:
            # Healthy baseline
            sample = np.random.normal(0.5, 0.05, num_features).astype(np.float32)
        else:
            # Degradation ramp
            progress = (i - 40) / 20.0
            sample = np.random.normal(0.5, 0.05, num_features).astype(np.float32)
            # Spike drops and errors
            sample[2] = 0.5 + progress * 0.5  # rx_drops rising
            sample[3] = 0.3 + progress * 0.4  # tx_drops rising
            sample[6] = max(0, 1.0 - progress)  # OSPF neighbors dropping
            sample[7] = max(0, 1.0 - progress * 0.8)  # LDP degrading

        # Create a fake metrics dict
        fake_metrics = {}
        cols = engine.columns if engine.columns else [f"feature_{j}" for j in range(num_features)]
        for j, col in enumerate(cols):
            fake_metrics[col] = float(sample[j]) if j < len(sample) else 0.0

        engine.ingest_sample(fake_metrics)

        if len(engine.sliding_window) >= engine.seq_len:
            acp = engine.run_inference()
            if acp:
                if acp.ml_analysis["anomaly_detected"] or i % 20 == 0:
                    engine.print_acp_summary(acp)
                    if acp.ml_analysis["anomaly_detected"]:
                        engine.log_acp(acp)


def main():
    parser = argparse.ArgumentParser(description="Aether Live Inference Engine")
    parser.add_argument("--single-shot", action="store_true", help="Run one inference cycle then exit")
    parser.add_argument("--demo", action="store_true", help="Run demo with synthetic data")
    parser.add_argument("--interval", type=float, default=1.0, help="Scrape interval in seconds")
    args = parser.parse_args()

    engine = AetherInferenceEngine()
    engine.load_models()

    if args.demo:
        run_demo(engine)
    elif args.single_shot:
        if HAS_COLLECTOR:
            metrics = scrape_once()
            engine.ingest_sample(metrics)
            print(f"[*] Ingested 1 sample. Need {engine.seq_len} to run inference.")
        else:
            print("[!] No live exporter available. Use --demo instead.")
    else:
        run_continuous(engine, args.interval)


if __name__ == "__main__":
    main()
