# Project Aether — Phase 3 Models Package
# =============================================================================
# This package contains the core ML and decision-making engine:
#
#   taxonomy.py            — Single source of truth for fault classes + autonomy policy
#   predictive_engine.py   — PyTorch LSTM models (Autoencoder, Classifier, Regressor)
#   graph_model.py         — NetworkX Graph-Analytical Engine & Clonal State Search
#   acp_manager.py         — Anomaly Context Packet (ACP) schema and serialization
#   aether_corroborator.py — Dual-model corroboration & Edge Policy Engine
#   data_collector.py      — Telemetry-to-Dataset bridge
#   train_models.py        — End-to-end training pipeline
#   inference_engine.py    — Live inference with sliding window
# =============================================================================
