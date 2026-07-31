#!/usr/bin/env python3
# =============================================================================
# ikb_manager.py — Incident Knowledge Base (ChromaDB vector store)
#
# Manages two collections:
#   runbooks  — static operational playbooks (topology, fault procedures)
#   incidents — ACP logs ingested from ikb/incidents.jsonl
#
# Embedding model: all-MiniLM-L6-v2 (sentence-transformers, runs 100% offline)
# Vector DB: ChromaDB (persistent, local filesystem)
#
# Usage:
#   python3 ikb_manager.py --seed          # load runbooks into ChromaDB
#   python3 ikb_manager.py --ingest-acps  # sync incidents.jsonl → ChromaDB
#   python3 ikb_manager.py --query "BGP flap on pe1" --top-k 3
# =============================================================================
import os
import sys
import json
import glob
import argparse

# Air-gap: force huggingface_hub/transformers to use ONLY the local model cache.
# Without this, SentenceTransformerEmbeddingFunction does an online version
# check against huggingface.co on first query — an outbound call that violates
# the air gap and 500s the whole RAG path inside a zero-egress namespace.
# Must be set before sentence_transformers/huggingface_hub are imported.
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT   = os.path.dirname(SCRIPT_DIR)
RUNBOOK_DIR = os.path.join(SCRIPT_DIR, "runbooks")
IKB_LOG     = os.path.join(REPO_ROOT, "phase3-models", "ikb", "incidents.jsonl")
CHROMA_DIR  = os.path.join(SCRIPT_DIR, "chroma_db")

try:
    import chromadb
    from chromadb.utils import embedding_functions
    HAS_CHROMA = True
except ImportError:
    HAS_CHROMA = False

try:
    from sentence_transformers import SentenceTransformer
    HAS_SBERT = True
except ImportError:
    HAS_SBERT = False


def _get_client():
    if not HAS_CHROMA:
        raise RuntimeError("chromadb not installed — run: pip install chromadb")
    return chromadb.PersistentClient(path=CHROMA_DIR)


def _get_ef():
    """Local embedding function — all-MiniLM-L6-v2, no cloud calls."""
    if HAS_SBERT:
        return embedding_functions.SentenceTransformerEmbeddingFunction(
            model_name="all-MiniLM-L6-v2"
        )
    # Fallback: chromadb's default (still local)
    return embedding_functions.DefaultEmbeddingFunction()


# ── Seed runbooks ─────────────────────────────────────────────────────────────

def seed_runbooks(verbose=True):
    client = _get_client()
    ef = _get_ef()
    col = client.get_or_create_collection("runbooks", embedding_function=ef)

    md_files = glob.glob(os.path.join(RUNBOOK_DIR, "*.md"))
    if not md_files:
        print(f"[-] No runbooks found in {RUNBOOK_DIR}")
        return 0

    added = 0
    for path in md_files:
        doc_id = os.path.basename(path)
        with open(path) as f:
            content = f.read()
        # Chunk by heading sections so queries hit relevant paragraphs
        chunks = _chunk_markdown(content, doc_id)
        for chunk_id, chunk_text, chunk_meta in chunks:
            existing = col.get(ids=[chunk_id])
            if existing["ids"]:
                continue  # already indexed
            col.add(documents=[chunk_text], ids=[chunk_id], metadatas=[chunk_meta])
            added += 1
        if verbose:
            print(f"  [+] {doc_id} — {len(chunks)} chunks")

    if verbose:
        print(f"[+] Runbooks seeded: {added} chunks across {len(md_files)} files")
    return added


def _chunk_markdown(text: str, source: str) -> list[tuple]:
    """Split markdown into heading-bounded chunks."""
    import re
    sections = re.split(r'\n(?=## )', text)
    chunks = []
    for i, section in enumerate(sections):
        if not section.strip():
            continue
        chunk_id  = f"{source}__chunk{i}"
        chunk_meta = {"source": source, "chunk": i}
        chunks.append((chunk_id, section.strip(), chunk_meta))
    return chunks if chunks else [(f"{source}__0", text, {"source": source, "chunk": 0})]


# ── Ingest ACP incidents ──────────────────────────────────────────────────────

def ingest_acps(verbose=True):
    if not os.path.exists(IKB_LOG):
        if verbose:
            print(f"[!] No IKB log at {IKB_LOG}")
        return 0
    client = _get_client()
    ef = _get_ef()
    col = client.get_or_create_collection("incidents", embedding_function=ef)

    added = 0
    with open(IKB_LOG) as f:
        for line in f:
            try:
                entry = json.loads(line.strip())
            except json.JSONDecodeError:
                continue
            acp_id = entry.get("acp_id", "")
            if not acp_id:
                continue
            existing = col.get(ids=[acp_id])
            if existing["ids"]:
                # Update operator_feedback if it changed
                fb = entry.get("operator_feedback")
                if fb:
                    col.update(ids=[acp_id], metadatas=[{"operator_feedback": fb}])
                continue
            # Build a searchable text representation of the ACP
            text = (
                f"ACP {acp_id[:8]} | severity={entry.get('severity','?')} | "
                f"timestamp={entry.get('timestamp','?')} | "
                f"trigger={entry.get('trigger_source','?')} | "
                f"feedback={entry.get('operator_feedback','pending')}"
            )
            meta = {
                "acp_id": acp_id,
                "severity": entry.get("severity", "MEDIUM"),
                "timestamp": entry.get("timestamp", ""),
                "operator_feedback": entry.get("operator_feedback") or "pending",
            }
            col.add(documents=[text], ids=[acp_id], metadatas=[meta])
            added += 1

    if verbose and added:
        print(f"[+] Ingested {added} new ACP entries into ChromaDB")
    return added


# ── Query ─────────────────────────────────────────────────────────────────────

def query(question: str, top_k: int = 4, collection: str = "runbooks") -> list[dict]:
    client = _get_client()
    ef = _get_ef()
    try:
        col = client.get_collection(collection, embedding_function=ef)
    except Exception:
        return []
    results = col.query(query_texts=[question], n_results=min(top_k, col.count()))
    docs     = results.get("documents", [[]])[0]
    metas    = results.get("metadatas", [[]])[0]
    distances = results.get("distances",  [[]])[0]
    return [{"text": d, "meta": m, "distance": dist}
            for d, m, dist in zip(docs, metas, distances)]


def query_all(question: str, top_k: int = 4) -> list[dict]:
    """Query both runbooks and incidents, merge and re-rank by distance."""
    rb = query(question, top_k, "runbooks")
    inc = query(question, top_k, "incidents")
    combined = rb + inc
    combined.sort(key=lambda x: x["distance"])
    return combined[:top_k]


def format_context(results: list[dict]) -> str:
    parts = []
    for r in results:
        src = r["meta"].get("source", r["meta"].get("acp_id", "?"))
        parts.append(f"[{src}]\n{r['text']}")
    return "\n\n---\n\n".join(parts)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Aether IKB Manager")
    parser.add_argument("--seed",        action="store_true", help="Seed runbooks into ChromaDB")
    parser.add_argument("--ingest-acps", action="store_true", help="Sync incidents.jsonl → ChromaDB")
    parser.add_argument("--query",       help="Search the IKB")
    parser.add_argument("--top-k", type=int, default=4)
    args = parser.parse_args()

    if args.seed:
        seed_runbooks()
    if args.ingest_acps:
        ingest_acps()
    if args.query:
        results = query_all(args.query, args.top_k)
        if not results:
            print("No results. Run --seed first.")
        for i, r in enumerate(results, 1):
            print(f"\n── Result {i} (dist={r['distance']:.3f}) ──")
            print(r["text"][:800])


if __name__ == "__main__":
    main()
