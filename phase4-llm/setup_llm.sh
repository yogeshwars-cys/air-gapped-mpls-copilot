#!/usr/bin/env bash
# =============================================================================
# setup_llm.sh — Ollama + Mistral 7B offline setup for Project Aether
#
# Run this ONCE before going air-gapped (needs internet for initial download).
# After setup, everything runs fully offline.
#
# What this does:
#   1. Installs Ollama (system daemon)
#   2. Pulls mistral:7b-instruct-q4_K_M (~4.1 GB GGUF)
#   3. Installs Python deps (chromadb, sentence-transformers, fastapi)
#   4. Seeds the IKB (ChromaDB) with runbooks + topology metadata
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

echo "╔══════════════════════════════════════════════════════════╗"
echo "║     Project Aether — Phase 4 LLM Setup                  ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""

# ── 1. Ollama ────────────────────────────────────────────────────────────────
if ! command -v ollama &>/dev/null; then
    echo "[*] Installing Ollama..."
    curl -fsSL https://ollama.com/install.sh | sh
    echo "[+] Ollama installed"
else
    echo "[✓] Ollama already installed: $(ollama --version)"
fi

# Start Ollama server in background if not running
if ! pgrep -x ollama &>/dev/null; then
    echo "[*] Starting Ollama server..."
    ollama serve &>/tmp/ollama.log &
    sleep 3
fi

# ── 2. Pull Mistral 7B (quantized, 4.1 GB) ───────────────────────────────────
MODEL="mistral:7b-instruct-q4_K_M"
if ollama list 2>/dev/null | grep -q "mistral"; then
    echo "[✓] Mistral already available"
else
    echo "[*] Pulling ${MODEL} (~4.1 GB — this takes a few minutes)..."
    ollama pull "${MODEL}"
    echo "[+] Model pulled"
fi

# ── 3. Python deps ───────────────────────────────────────────────────────────
echo "[*] Installing Python dependencies..."
pip install chromadb sentence-transformers fastapi uvicorn jinja2 httpx statsmodels jsonlines cryptography pyyaml -q
echo "[+] Python deps installed"

# ── 4. Seed IKB ──────────────────────────────────────────────────────────────
echo "[*] Seeding Incident Knowledge Base (ChromaDB)..."
cd "${SCRIPT_DIR}"
python3 ikb_manager.py --seed
echo "[+] IKB seeded"

echo ""
echo "╔══════════════════════════════════════════════════════════╗"
echo "║     Setup complete — system is air-gap ready             ║"
echo "║                                                          ║"
echo "║  Start Aether:  python3 phase5-dashboard/app.py          ║"
echo "║  Dashboard:     http://localhost:8080                    ║"
echo "╚══════════════════════════════════════════════════════════╝"
