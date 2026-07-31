#!/usr/bin/env python3
# =============================================================================
# nlq_interface.py — Natural Language Query Interface for Project Aether
#
# Interactive terminal loop that accepts operator questions about the network
# and either answers from the IKB (ChromaDB) or delegates to Mistral via RAG.
#
# Conversation states:
#   IDLE → CLARIFY → CONFIRM → (SIMULATE | REPORT) → IDLE
#
# The clarification step detects if the question is about a specific fault
# class or node and enriches the query with relevant IKB context before
# generating the final answer.
#
# Usage:
#   python3 nlq_interface.py              # interactive loop
#   python3 nlq_interface.py --once "how do I fix BGP flap on pe1?"
# =============================================================================
import os
import sys
import argparse

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
sys.path.insert(0, os.path.join(SCRIPT_DIR, "..", "phase3-models"))

from llm_copilot import AetherCopilot, _ollama_available
from ikb_manager import query_all, format_context, seed_runbooks

# Known fault classes and node names for intent detection
FAULT_TERMS = {
    "bgp": "Control-Plane Flap",
    "ospf": "Control-Plane Flap",
    "flap": "Control-Plane Flap",
    "control": "Control-Plane Flap",
    "latency": "Latency Spike",
    "delay": "Latency Spike",
    "slow": "Latency Spike",
    "loss": "Packet Loss",
    "drop": "Packet Loss",
    "corrupt": "Frame Corruption",
    "crc": "Frame Corruption",
    "congestion": "Rate Limiting / Congestion",
    "throttle": "Rate Limiting / Congestion",
    "bandwidth": "Rate Limiting / Congestion",
    "saturation": "Rate Limiting / Congestion",
}
NODES = {"pe1", "pe2", "p1", "ce-branch1", "ce-branch2", "ce-hub", "ce-dc"}
SLA_SERVICES = {"voip", "database", "bulk_transfer", "video"}


def _detect_intent(text: str) -> dict:
    """Classify operator intent for focused IKB retrieval."""
    low = text.lower()
    intent = {
        "fault_class": None,
        "nodes": [],
        "service": None,
        "is_how_to": any(w in low for w in ("how", "fix", "resolve", "mitigate", "repair")),
        "is_status": any(w in low for w in ("what", "why", "status", "happening", "wrong")),
    }
    for term, cls in FAULT_TERMS.items():
        if term in low:
            intent["fault_class"] = cls
            break
    intent["nodes"] = [n for n in NODES if n in low]
    for svc in SLA_SERVICES:
        if svc in low.replace("-", ""):
            intent["service"] = svc
            break
    return intent


def _clarify(question: str, intent: dict) -> str:
    """
    Expand the query for IKB retrieval based on detected intent.
    Returns an enriched query string (more context = better vector match).
    """
    parts = [question]
    if intent["fault_class"]:
        parts.append(f"fault type: {intent['fault_class']}")
    if intent["nodes"]:
        parts.append(f"nodes: {', '.join(intent['nodes'])}")
    if intent["service"]:
        parts.append(f"service SLA: {intent['service']}")
    if intent["is_how_to"]:
        parts.append("mitigation steps procedure")
    if intent["is_status"]:
        parts.append("root cause symptoms diagnosis")
    return " | ".join(parts)


def _build_context_banner(intent: dict) -> str:
    """Show what was detected to help operator understand the retrieval."""
    parts = []
    if intent["fault_class"]:
        parts.append(f"fault: {intent['fault_class']}")
    if intent["nodes"]:
        parts.append(f"nodes: {', '.join(intent['nodes'])}")
    if intent["service"]:
        parts.append(f"sla: {intent['service']}")
    return f"[intent: {', '.join(parts)}]" if parts else "[intent: general query]"


class NLQInterface:
    def __init__(self):
        print("[*] Initializing Aether NLQ Interface...")
        seed_runbooks(verbose=False)
        self.copilot = AetherCopilot(auto_seed=False)
        self._history: list[tuple[str, str]] = []  # (question, answer)

    def ask(self, question: str, verbose=True) -> str:
        intent  = _detect_intent(question)
        enriched = _clarify(question, intent)

        if verbose:
            banner = _build_context_banner(intent)
            print(f"  {banner}")

        results  = query_all(enriched, top_k=4)
        context  = format_context(results) if results else ""

        if _ollama_available():
            from llm_copilot import NLQ_TEMPLATE, _ollama_generate, SYSTEM_PROMPT
            prompt = NLQ_TEMPLATE.format(
                context=context[:2500] if context else "No runbook context found.",
                question=question,
            )
            answer = _ollama_generate(prompt, system=SYSTEM_PROMPT)
        else:
            if results:
                best = results[0]["text"]
                src  = results[0]["meta"].get("source", "?")
                answer = (f"[Ollama offline — best runbook match from {src}]\n\n"
                         f"{best[:600]}\n\n"
                         f"Start Ollama for full natural language answers.")
            else:
                answer = "No relevant runbook found and Ollama is offline."

        self._history.append((question, answer))
        return answer

    def run_interactive(self):
        print("\n" + "="*60)
        print("  AETHER NOC COPILOT — Natural Language Query Interface")
        print("  Type 'quit' or Ctrl-C to exit  |  'history' for past Q&A")
        print("="*60 + "\n")

        while True:
            try:
                q = input("  NOC> ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n[*] Exiting NLQ interface.")
                break
            if not q:
                continue
            if q.lower() in ("quit", "exit", "q"):
                break
            if q.lower() == "history":
                for i, (question, answer) in enumerate(self._history[-5:], 1):
                    print(f"\n  [{i}] Q: {question}")
                    print(f"      A: {answer[:200]}...")
                continue
            print()
            answer = self.ask(q)
            print(f"  {answer}\n")


def main():
    parser = argparse.ArgumentParser(description="Aether NLQ Interface")
    parser.add_argument("--once", help="Ask a single question and exit")
    args = parser.parse_args()

    iface = NLQInterface()
    if args.once:
        answer = iface.ask(args.once)
        print(f"\n{answer}\n")
    else:
        iface.run_interactive()


if __name__ == "__main__":
    main()
