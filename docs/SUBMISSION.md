# Idea Submission — Bharatiya Antariksh Hackathon 2026 (PS-13)

Challenge: **Air-Gapped Predictive Copilot for Secure MPLS Operations**
Team deck: `ISRO_Aether_Idea_Submission.pptx` (10-slide Hack2skill template, kept outside the repo — decks are gitignored).

This file preserves the submission-form copy so it stays versioned with the code.

## Brief about the idea

Aether is an autonomous, fully air-gapped AI copilot for secure MPLS network
operations — built for environments like ISRO ground networks and defence
backbones where cloud-based AIOps is prohibited.

It turns a reactive NOC into a predictive one by answering three operator
questions in real time: what will fail and when, why, and what action to take.
Precursor-based ML (BiLSTM+Attention, an LSTM-Autoencoder, and a time-to-breach
regressor) forecasts faults 20–500 s before impact; a graph-based digital twin
corroborates the diagnosis and selects the best reroute/QoS fix; and an offline
LLM (Mistral 7B + local RAG) explains it in operator language. Corrective
actions are policy-gated — auto-execute vs recommend-only, with core paths
locked to humans.

Everything runs with zero cloud dependency, proven by Ed25519-signed models and
a signed air-gap compliance report. A working 7-node Containerlab prototype
already runs it end-to-end.

## What problem are we trying to solve?

Mission-critical MPLS backbones in air-gapped environments — ISRO ground
networks, defence, and other secure operations — cannot use modern cloud AIOps
tools because those depend on outbound connectivity that is prohibited inside
the security boundary. As a result, these NOCs remain largely reactive: alerts
fire only *after* an SLA is already breached, root-cause analysis is manual,
and there is no safe way to automate corrective action.

Three gaps make this acute:

1. **No prediction** — congestion, latency/jitter drift, and routing/tunnel
   degradation are caught late, not forecast.
2. **No explainability** — raw telemetry and thresholds don't tell an operator
   *what* is failing, *why*, or *what to do*.
3. **No safe autonomy** — even when a fix is known, blindly automating reroutes
   on a core path is dangerous.

We solve this with a fully offline predictive copilot that forecasts faults
20–500 s before impact, explains them in operator language via a local LLM,
and recommends a policy-gated corrective action — with zero cloud dependency,
proven by a signed air-gap compliance report.

## Technology stack

- **Network simulation**: Containerlab (7-node MPLS L3VPN), FRRouting
  (OSPF/BGP/LDP), GRE SD-WAN overlay, tc/HTB QoS
- **Telemetry**: SNMP / NetFlow / syslog / streaming, custom zero-dependency
  Prometheus exporter, Grafana
- **Predictive ML**: PyTorch — BiLSTM+Attention classifier, LSTM-Autoencoder,
  time-to-breach regressor; NetworkX graph clonal search; CUDA (RTX 4060)
- **Offline LLM + RAG**: Ollama + Mistral 7B (quantized), ChromaDB,
  sentence-transformers
- **App & security**: FastAPI NOC dashboard, Ed25519 model signing + signed
  air-gap compliance report

100% open-source, runs offline.

## Measured on the prototype (synthetic validation set)

- Classifier: 91.8% accuracy · macro-F1 0.905 · per-class F1 0.84–0.96 (6 classes)
- Detection: P 0.92 · R 0.95 · F1 0.93 · FPR 0.13
- Time-to-breach error ≈ 20 s · 4/4 brief scenarios pass
- Honest caveat: prediction lead time is still improving
