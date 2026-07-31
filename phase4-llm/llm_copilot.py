#!/usr/bin/env python3
# =============================================================================
# llm_copilot.py — Offline LLM Copilot (Ollama + Mistral 7B + ChromaDB RAG)
#
# Answers the three NOC operator questions for every ACP:
#   Q1: What is likely to fail next — and when?
#   Q2: Why is risk elevated — which signals contributed?
#   Q3: What corrective action should be taken?
#
# Architecture:
#   ACP → IKB query (ChromaDB) → RAG context → Mistral 7B prompt → response
#
# Runs 100% offline. Ollama serves Mistral locally via HTTP on port 11434.
# If Ollama is not running, returns a structured fallback from the ACP alone.
#
# Usage:
#   from llm_copilot import AetherCopilot
#   copilot = AetherCopilot()
#   report = copilot.explain(acp)          # full structured report
#   answer = copilot.query("What is wrong with pe1?")  # NLQ
# =============================================================================
import os
import sys
import json
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(SCRIPT_DIR, "..", "phase3-models"))

from ikb_manager import query_all, format_context, ingest_acps, seed_runbooks

try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False

OLLAMA_URL   = "http://127.0.0.1:11434"
OLLAMA_MODEL = "mistral:7b-instruct-q4_K_M"
TIMEOUT      = 120  # seconds


# ── Ollama helpers ────────────────────────────────────────────────────────────

def _ollama_available() -> bool:
    if not HAS_HTTPX:
        return False
    try:
        r = httpx.get(f"{OLLAMA_URL}/api/tags", timeout=3)
        return r.status_code == 200
    except Exception:
        return False


def _ollama_generate(prompt: str, system: str = "") -> str:
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "system": system,
        "stream": False,
        "options": {"temperature": 0.2, "top_p": 0.9, "num_ctx": 4096},
    }
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            r = client.post(f"{OLLAMA_URL}/api/generate", json=payload)
            r.raise_for_status()
            return r.json().get("response", "").strip()
    except Exception as e:
        return f"[LLM unavailable: {e}]"


# ── Prompt templates ──────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are Aether, an offline AI NOC copilot for a secure MPLS network.
You answer in structured, concise operator-ready language.
Never mention cloud services or external APIs — everything is air-gapped.
Base your answers on the provided telemetry data and runbook context only.
Do not hallucinate. If you don't know, say so."""

ACP_EXPLAIN_TEMPLATE = """
NETWORK ALERT — Anomaly Context Packet
=======================================
Fault class    : {fault_class}
Confidence     : {confidence:.0%}
Severity       : {severity}
Time to impact : {ttf}
Execution mode : {execution_mode}
Engines agree  : {engines_agree}
Top features   : {top_features}
Service SLA tag: {sla_tag}
Digital twin   : {twin_divergence}
Recommended    : {recommended_action}
Rationale      : {rationale}

RUNBOOK / IKB CONTEXT
======================
{context}

OPERATOR QUESTIONS — answer all three:

Q1. What is likely to fail next, and when?
Q2. Why is risk assessed as elevated — which signals contributed most?
Q3. What corrective action should be taken before SLA or security impact occurs?

Keep each answer to 2–3 sentences. Use exact node/interface names from the context.
"""

NLQ_TEMPLATE = """
NETWORK TOPOLOGY CONTEXT
=========================
{context}

OPERATOR QUESTION
=================
{question}

Answer in plain English, concisely (3–5 sentences). Reference specific nodes or interfaces where relevant.
"""

NLQ_TEMPLATE_MULTITURN = """
NETWORK TOPOLOGY CONTEXT (retrieved from local runbooks + incident history)
=========================
{context}

CONVERSATION SO FAR
=================
{history}

NEW OPERATOR QUESTION
=================
{question}

Continue the conversation. Use the earlier turns to resolve follow-ups and
pronouns (e.g. "it", "that link", "the same node"). Answer in plain English,
concisely (3–5 sentences), grounded in the context above. Reference specific
nodes or interfaces where relevant. Do not invent data that is not in the context.
"""


def _format_history(history) -> str:
    """Render prior turns as a compact transcript for the prompt."""
    if not history:
        return "(this is the first question in the conversation)"
    lines = []
    for t in history[-8:]:  # cap context to the last 8 turns
        who = "Operator" if t.get("role") == "user" else "Aether"
        lines.append(f"{who}: {t.get('content', '').strip()}")
    return "\n".join(lines)


# ── AetherCopilot class ───────────────────────────────────────────────────────

class AetherCopilot:
    def __init__(self, auto_seed=True):
        self._available = _ollama_available()
        if auto_seed:
            try:
                seed_runbooks(verbose=False)
                ingest_acps(verbose=False)
            except Exception:
                pass
        if self._available:
            print(f"[+] Aether LLM Copilot online ({OLLAMA_MODEL})")
        else:
            print("[!] Ollama not running — copilot will use structured fallback")
            print(f"    To enable: install Ollama and run: ollama pull {OLLAMA_MODEL}")

    def explain(self, acp) -> dict:
        """
        Generate a full structured incident report for an ACP.
        Returns dict with keys: q1, q2, q3, raw_llm, source
        """
        ml  = acp.ml_analysis
        cor = acp.corroboration

        ttf_val = ml.get("estimated_time_to_failure_sec", -1)
        ttf_str = f"{ttf_val:.0f}s" if ttf_val >= 0 else "unknown"

        twin_div = getattr(acp, "digital_twin_divergence", None)
        twin_str = f"{twin_div:.3f}" if twin_div is not None else "N/A"

        top_features = getattr(acp, "top_features", [])
        sla_tag      = getattr(acp, "service_sla_tag", "default")

        # Retrieve relevant runbook context
        query_text = (
            f"{ml.get('predicted_fault_class','')} "
            f"{cor.get('recommended_action','')} "
            f"{cor.get('rationale','')[:100]}"
        )
        rag_results = query_all(query_text, top_k=3)
        context     = format_context(rag_results) if rag_results else "No runbook context available."

        use_llm = self._available
        if use_llm:
            prompt = ACP_EXPLAIN_TEMPLATE.format(
                fault_class     = ml.get("predicted_fault_class", "Unknown"),
                confidence      = ml.get("confidence_score", 0),
                severity        = acp.severity,
                ttf             = ttf_str,
                execution_mode  = cor.get("execution_mode", "?"),
                engines_agree   = cor.get("engines_agree", False),
                top_features    = ", ".join(top_features[:5]) or "N/A",
                sla_tag         = sla_tag,
                twin_divergence = twin_str,
                recommended_action = cor.get("recommended_action", "?"),
                rationale       = cor.get("rationale", "")[:300],
                context         = context[:2000],
            )
            raw = _ollama_generate(prompt, system=SYSTEM_PROMPT)
            if raw.startswith("[LLM unavailable"):
                use_llm = False
            else:
                q1, q2, q3 = _parse_q_answers(raw)
                source = "ollama"
        if not use_llm:
            q1, q2, q3 = self._fallback_answers(acp, ttf_str, top_features)
            raw    = ""
            source = "structured_fallback"

        return {
            "acp_id"     : acp.acp_id,
            "fault_class": ml.get("predicted_fault_class", "Unknown"),
            "severity"   : acp.severity,
            "q1_what_fails": q1,
            "q2_why_risk"  : q2,
            "q3_action"    : q3,
            "raw_llm"      : raw,
            "source"       : source,
            "timestamp"    : acp.timestamp,
        }

    def query(self, question: str) -> str:
        """Natural language query — searches IKB and answers via LLM (single-shot)."""
        return self.query_multiturn(question, history=None)

    def query_multiturn(self, question: str, history=None) -> str:
        """
        Multi-turn NLQ. `history` is a list of prior turns:
            [{"role": "user"|"assistant", "content": "..."}, ...]
        RAG retrieval is run against the full conversation (history + new question)
        so follow-ups like "and what about pe2?" still pull relevant context.
        """
        history = history or []
        # Retrieve against the running conversation, not just the latest line,
        # so pronoun/elliptical follow-ups still ground in the right runbooks.
        recent_user = " ".join(
            t["content"] for t in history[-4:] if t.get("role") == "user"
        )
        retrieval_query = (recent_user + " " + question).strip()
        results = query_all(retrieval_query, top_k=4)
        context = format_context(results) if results else "No relevant context found in the IKB."

        if not self._available:
            return self._nlq_fallback(question, results)

        convo = _format_history(history)
        prompt = NLQ_TEMPLATE_MULTITURN.format(
            context=context[:2500], history=convo, question=question
        )
        answer = _ollama_generate(prompt, system=SYSTEM_PROMPT)
        if answer.startswith("[LLM unavailable"):
            return self._nlq_fallback(question, results)
        return answer

    def _fallback_answers(self, acp, ttf_str, top_features) -> tuple[str, str, str]:
        """Rule-based structured answers when Ollama is offline."""
        ml  = acp.ml_analysis
        cor = acp.corroboration
        fc  = ml.get("predicted_fault_class", "Unknown")
        conf= ml.get("confidence_score", 0)
        act = cor.get("recommended_action", "NO_ACTION")

        q1 = (f"The {fc} fault is predicted to breach SLA in approximately {ttf_str}. "
              f"Model confidence is {conf:.0%}. "
              f"{'Digital twin divergence confirms the trend is accelerating.' if acp.digital_twin_divergence else ''}")

        q2 = (f"Risk is elevated because the BiLSTM classifier detected a {fc} pattern "
              f"with {conf:.0%} confidence, corroborated by the NetworkX graph model. "
              f"Top contributing features: {', '.join(top_features[:3]) or 'see ACP ml_analysis'}.")

        q3 = (f"Recommended action: {act}. "
              f"Execution mode: {cor.get('execution_mode','RECOMMEND_ONLY')}. "
              f"{cor.get('rationale','')[:200]}")

        return q1, q2, q3

    def _nlq_fallback(self, question: str, results: list) -> str:
        if not results:
            return ("No relevant runbook context found. "
                    f"Run `ollama pull {OLLAMA_MODEL}` for full LLM answers.")
        # Build a structured answer from top RAG hits
        header = (
            "**[RAG runbook excerpts — Mistral is not responding; full LLM answers resume when Ollama is back]**"
            if self._available else
            "**[Offline runbook answer — Ollama is not running]**"
        )
        lines = [header + "\n"]
        lines.append(f"**Question:** {question}\n")
        for i, r in enumerate(results[:3], 1):
            preview = r["text"][:400].strip()
            lines.append(f"**Runbook match {i}:**\n{preview}\n")
        lines.append(f"\n_Full LLM answers available once `ollama pull {OLLAMA_MODEL}` completes._")
        return "\n".join(lines)


def _parse_q_answers(raw: str) -> tuple[str, str, str]:
    """Extract Q1/Q2/Q3 answers from LLM free-form output."""
    import re
    parts = {"q1": "", "q2": "", "q3": ""}
    current = None
    for line in raw.splitlines():
        line = line.strip()
        if re.match(r'Q1[\.:]', line, re.I):
            current = "q1"; line = re.sub(r'^Q1[\.:\s]+', '', line, flags=re.I)
        elif re.match(r'Q2[\.:]', line, re.I):
            current = "q2"; line = re.sub(r'^Q2[\.:\s]+', '', line, flags=re.I)
        elif re.match(r'Q3[\.:]', line, re.I):
            current = "q3"; line = re.sub(r'^Q3[\.:\s]+', '', line, flags=re.I)
        if current and line:
            parts[current] += (" " if parts[current] else "") + line
    # If parsing failed, split by thirds
    if not any(parts.values()):
        lines = [l for l in raw.splitlines() if l.strip()]
        n = max(len(lines) // 3, 1)
        parts["q1"] = " ".join(lines[:n])
        parts["q2"] = " ".join(lines[n:2*n])
        parts["q3"] = " ".join(lines[2*n:])
    return parts["q1"] or raw[:200], parts["q2"] or "", parts["q3"] or ""


# ── Demo ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    sys.path.insert(0, os.path.join(SCRIPT_DIR, "..", "phase3-models"))
    from acp_manager import AnomalyContextPacket

    print("[*] Seeding IKB...")
    seed_runbooks()

    print("[*] Building demo ACP...")
    acp = AnomalyContextPacket()
    acp.set_ml_results(True, 0.02, "Control-Plane Flap", 0.91, 28.0)
    acp.set_corroboration(True, "Both engines agree on flap pattern.", "CORE_PATH_FAILOVER", "RECOMMEND_ONLY")
    acp.set_top_features(["pe1_frr_bgp_vpn_established", "pe1_frr_ospf_neighbors_total",
                          "pe1_eth1_rx_drops", "pe1_frr_ldp_session_operational", "p1_eth1_rx_bytes"])
    acp.service_sla_tag = "voip"

    copilot = AetherCopilot(auto_seed=False)
    report  = copilot.explain(acp)

    print(f"\n{'='*60}")
    print(f"Q1 — What fails next?  {report['q1_what_fails']}")
    print(f"\nQ2 — Why elevated?    {report['q2_why_risk']}")
    print(f"\nQ3 — What action?     {report['q3_action']}")
    print(f"\nSource: {report['source']}")

    print(f"\n{'='*60}")
    print("NLQ test: 'How do I fix a BGP neighbor flap on pe1?'")
    ans = copilot.query("How do I fix a BGP neighbor flap on pe1?")
    print(ans[:500])
