# Phase 5 — Copilot Integration & Decision Support

> **Where the code lives:** the Phase 5 integration is implemented across
> [`phase5-dashboard/`](../phase5-dashboard) (the wiring, API surface, and operator UI)
> and [`phase4-llm/`](../phase4-llm) (the copilot itself). This directory is a
> structural marker for that phase — the integration glue is the data flow below,
> not a separate codebase.

The problem statement's Phase 5 asks us to *"wire predictive model outputs and
network telemetry into the LLM context window via the RAG pipeline"* and *"produce
structured responses for every alert"*. That is exactly the ACP → explain flow:

```
 Predictive engine (Phase 3)                 Offline LLM copilot (Phase 4)
 ┌───────────────────────────┐               ┌──────────────────────────────┐
 │ BiLSTM + Autoencoder + TTF │   ACP json   │ AetherCopilot.explain(acp)   │
 │ + graph clonal search      │ ───────────▶ │  • ChromaDB RAG over runbooks │
 │ → Anomaly Context Packet   │              │    + past incidents           │
 └───────────────────────────┘              │  • Mistral 7B (Ollama, local) │
            │                                 │  • structured Q1/Q2/Q3 output │
            │ telemetry snapshot,             └──────────────────────────────┘
            │ top_features, severity,                      │
            │ recommended_action                           ▼
            ▼                                  ┌──────────────────────────────┐
 acp_logs/*.json  ───────────────────────────▶│ GET /api/explain/{acp_id}    │
                                               │  (phase5-dashboard/app.py)    │
                                               │  → attaches node-specific     │
                                               │    remediation commands       │
                                               └──────────────────────────────┘
                                                              │
                                                              ▼
                                            Dashboard incident modal — Q1/Q2/Q3
                                            (predicted issue, confidence, root
                                             cause, affected scope, time-to-impact,
                                             recommended action + commands)
```

## Concrete entry points

| Concern | Location |
|---|---|
| Structured per-alert report (predicted issue, confidence, root cause, scope, TTF, action) | `phase4-llm/llm_copilot.py` → `AetherCopilot.explain(acp)` returns `q1_what_fails` / `q2_why_risk` / `q3_action` |
| HTTP integration of model output + telemetry into the copilot | `phase5-dashboard/app.py` → `GET /api/explain/{acp_id}` (loads the ACP, calls `explain`, attaches remediation) |
| Natural-language query interface (multi-turn) | `phase5-dashboard/app.py` → `POST /api/nlq` (+ `/api/nlq/reset`), `llm_copilot.query_multiturn` |
| RAG retrieval over internal artifacts only | `phase4-llm/ikb_manager.py` (ChromaDB: runbooks + incident history) |
| Live operator surface for the three NOC questions | `phase5-dashboard/app.py` — "Overview" Q1/Q2/Q3 panels + incident modal |

## The three operational questions (problem statement Expected Outcomes)

Every alert is rendered against the three questions the NOC must answer in real time:

- **Q1 — What is likely to fail next, and when?** → fault class, confidence, and the
  time-to-breach lead time from the TTF regressor.
- **Q2 — Why is risk elevated — which signals contributed?** → top attention features
  from the classifier plus the corroboration rationale.
- **Q3 — What corrective action should be taken before SLA/security impact?** → the
  policy-gated recommended action (AUTO_EXECUTE vs RECOMMEND_ONLY) and the exact
  FRR/`tc` commands.

All inference is local (Ollama + Mistral 7B, ChromaDB) — no cloud dependency, per the
air-gap requirement.
