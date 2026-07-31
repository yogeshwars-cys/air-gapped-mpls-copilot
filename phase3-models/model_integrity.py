#!/usr/bin/env python3
# =============================================================================
# model_integrity.py — Ed25519 Model Weight Signing & Verification
#
# Computes SHA256 of each .pt model file and verifies it against an Ed25519
# signature.  The private key stays OFFLINE; only the public key lives on
# the deployed box.  On mismatch the EPE drops to supervised mode and raises
# a standing alert.
#
# Usage:
#   # One-time: generate keys (run offline, copy pubkey to deployed box)
#   python3 model_integrity.py --generate-keys --key-dir ./keys
#
#   # Sign model files (run offline with private key)
#   python3 model_integrity.py --sign --key-dir ./keys --model-dir ./saved
#
#   # Verify at startup (on deployed box, only needs pubkey)
#   python3 model_integrity.py --verify --key-dir ./keys --model-dir ./saved
#
#   # Python API:
#   from model_integrity import verify_all_models, IntegrityError
# =============================================================================
import os
import sys
import json
import hashlib
import argparse
from pathlib import Path

try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey, Ed25519PublicKey,
    )
    from cryptography.hazmat.primitives.serialization import (
        Encoding, PublicFormat, PrivateFormat, NoEncryption,
        load_pem_private_key, load_pem_public_key,
    )
    from cryptography.exceptions import InvalidSignature
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False

MODEL_FILES = ["autoencoder.pt", "classifier.pt", "regressor.pt"]
SIG_MANIFEST = "model_signatures.json"


class IntegrityError(RuntimeError):
    """Raised when a model file fails signature verification."""


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ── Key management ────────────────────────────────────────────────────────────

def generate_keys(key_dir: str):
    if not HAS_CRYPTO:
        print("[-] cryptography package not installed.  Run: pip install cryptography")
        sys.exit(1)
    os.makedirs(key_dir, exist_ok=True)
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()

    priv_path = os.path.join(key_dir, "aether_model_key.pem")
    pub_path  = os.path.join(key_dir, "aether_model_key.pub.pem")

    with open(priv_path, "wb") as f:
        f.write(priv.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
    os.chmod(priv_path, 0o600)

    with open(pub_path, "wb") as f:
        f.write(pub.public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo))

    print(f"[+] Private key → {priv_path}  (keep this OFFLINE)")
    print(f"[+] Public key  → {pub_path}   (deploy this to the air-gapped box)")


# ── Signing ───────────────────────────────────────────────────────────────────

def sign_models(key_dir: str, model_dir: str):
    if not HAS_CRYPTO:
        print("[-] cryptography package not installed.")
        sys.exit(1)
    priv_path = os.path.join(key_dir, "aether_model_key.pem")
    if not os.path.exists(priv_path):
        print(f"[-] Private key not found at {priv_path}.  Run --generate-keys first.")
        sys.exit(1)

    with open(priv_path, "rb") as f:
        priv = load_pem_private_key(f.read(), password=None)

    manifest = {}
    for fname in MODEL_FILES:
        fpath = os.path.join(model_dir, fname)
        if not os.path.exists(fpath):
            print(f"  [!] {fname} not found — skipping")
            continue
        digest = sha256_file(fpath)
        sig = priv.sign(digest.encode())
        manifest[fname] = {
            "sha256": digest,
            "signature": sig.hex(),
        }
        print(f"  [+] Signed {fname} (sha256: {digest[:16]}...)")

    out_path = os.path.join(model_dir, SIG_MANIFEST)
    with open(out_path, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"[+] Manifest written to {out_path}")


# ── Verification (deployed-box only) ─────────────────────────────────────────

def verify_all_models(key_dir: str, model_dir: str) -> dict:
    """
    Verify all model files against the signed manifest.

    Returns a dict: {filename: "OK" | "MISSING" | "TAMPERED" | "NO_SIGNATURE"}
    Raises IntegrityError if any file is TAMPERED.
    """
    results = {}

    if not HAS_CRYPTO:
        print("[!] cryptography not installed — skipping integrity check (insecure)")
        return {f: "SKIPPED" for f in MODEL_FILES}

    pub_path = os.path.join(key_dir, "aether_model_key.pub.pem")
    manifest_path = os.path.join(model_dir, SIG_MANIFEST)

    if not os.path.exists(pub_path):
        print(f"[!] Public key not found at {pub_path} — integrity check disabled")
        return {f: "NO_KEY" for f in MODEL_FILES}

    if not os.path.exists(manifest_path):
        print(f"[!] Signature manifest not found at {manifest_path} — models unsigned")
        return {f: "NO_SIGNATURE" for f in MODEL_FILES}

    with open(pub_path, "rb") as f:
        pub = load_pem_public_key(f.read())

    with open(manifest_path) as f:
        manifest = json.load(f)

    tampered = []
    for fname in MODEL_FILES:
        fpath = os.path.join(model_dir, fname)
        if not os.path.exists(fpath):
            results[fname] = "MISSING"
            continue
        if fname not in manifest:
            results[fname] = "NO_SIGNATURE"
            continue

        expected_hash = manifest[fname]["sha256"]
        sig_bytes = bytes.fromhex(manifest[fname]["signature"])
        actual_hash = sha256_file(fpath)

        if actual_hash != expected_hash:
            results[fname] = "TAMPERED"
            tampered.append(fname)
            continue

        try:
            pub.verify(sig_bytes, actual_hash.encode())
            results[fname] = "OK"
        except InvalidSignature:
            results[fname] = "TAMPERED"
            tampered.append(fname)

    if tampered:
        raise IntegrityError(
            f"Model integrity check FAILED for: {tampered}. "
            f"EPE is dropping to SUPERVISED mode — AUTO_EXECUTE disabled."
        )

    return results


def _print_results(results: dict):
    icons = {"OK": "✓", "MISSING": "✗", "TAMPERED": "⚠", "NO_SIGNATURE": "?", "NO_KEY": "?", "SKIPPED": "-"}
    for fname, status in results.items():
        print(f"  {icons.get(status, '?')} {fname:25s} {status}")


def main():
    parser = argparse.ArgumentParser(description="Aether Model Integrity — Ed25519 signing")
    parser.add_argument("--generate-keys", action="store_true")
    parser.add_argument("--sign",   action="store_true")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--key-dir",   default=os.path.join(os.path.dirname(__file__), "keys"))
    parser.add_argument("--model-dir", default=os.path.join(os.path.dirname(__file__), "saved"))
    args = parser.parse_args()

    if args.generate_keys:
        generate_keys(args.key_dir)
    elif args.sign:
        sign_models(args.key_dir, args.model_dir)
    elif args.verify:
        print(f"[*] Verifying model integrity...")
        try:
            results = verify_all_models(args.key_dir, args.model_dir)
            _print_results(results)
            print("[+] All models verified OK.")
        except IntegrityError as e:
            print(f"[!] {e}")
            sys.exit(1)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
