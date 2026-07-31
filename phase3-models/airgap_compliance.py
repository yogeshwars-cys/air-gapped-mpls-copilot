#!/usr/bin/env python3
# =============================================================================
# airgap_compliance.py — Air-Gap Connectivity Compliance Report Generator
#
# Attempts outbound TCP connections to known public endpoints.  In a properly
# air-gapped deployment ALL connections must fail.  Emits a signed JSON report.
# Same Ed25519 key pair as model_integrity.py — one key governs both reports.
#
# Usage:
#   python3 airgap_compliance.py                          # run check + print report
#   python3 airgap_compliance.py --out compliance.json    # save report to file
#   python3 airgap_compliance.py --verify compliance.json # verify a saved report
# =============================================================================
import os
import sys
import json
import socket
import hashlib
import argparse
from datetime import datetime, timezone

try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import load_pem_private_key, load_pem_public_key
    from cryptography.exceptions import InvalidSignature
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False

PROBE_TARGETS = [
    ("8.8.8.8",      53,  "Google Public DNS"),
    ("1.1.1.1",      53,  "Cloudflare DNS"),
    ("pypi.org",     443, "PyPI package index"),
    ("raw.githubusercontent.com", 443, "GitHub raw content"),
]

TIMEOUT_S = 3  # seconds per probe

KEY_DIR = os.path.join(os.path.dirname(__file__), "keys")
PRIV_KEY_PATH = os.path.join(KEY_DIR, "aether_model_key.pem")
PUB_KEY_PATH  = os.path.join(KEY_DIR, "aether_model_key.pub.pem")


def _probe(host: str, port: int) -> dict:
    result = {"host": host, "port": port, "reachable": False, "error": None}
    try:
        s = socket.create_connection((host, port), timeout=TIMEOUT_S)
        s.close()
        result["reachable"] = True
    except (socket.timeout, ConnectionRefusedError, OSError) as e:
        result["error"] = type(e).__name__
    return result


def run_compliance_check() -> dict:
    """
    Probe all PROBE_TARGETS and return a compliance report dict.
    status: COMPLIANT if all fail, NON_COMPLIANT if any succeed.
    """
    ts = datetime.now(timezone.utc).isoformat()
    probes = []
    for host, port, label in PROBE_TARGETS:
        r = _probe(host, port)
        r["label"] = label
        probes.append(r)
        icon = "⚠ BREACH" if r["reachable"] else "✓ blocked"
        print(f"  {icon}  {label:35s} ({host}:{port})")

    any_breach = any(p["reachable"] for p in probes)
    report = {
        "report_type": "airgap_compliance",
        "timestamp": ts,
        "hostname": socket.gethostname(),
        "status": "NON_COMPLIANT" if any_breach else "COMPLIANT",
        "probes": probes,
        "signature": None,
    }
    return report


def _sign_report(report: dict) -> dict:
    if not HAS_CRYPTO or not os.path.exists(PRIV_KEY_PATH):
        print(f"[!] Private key not found at {PRIV_KEY_PATH} — report will be unsigned")
        return report
    with open(PRIV_KEY_PATH, "rb") as f:
        priv = load_pem_private_key(f.read(), password=None)
    payload = json.dumps(
        {k: v for k, v in report.items() if k != "signature"}, sort_keys=True
    ).encode()
    digest = hashlib.sha256(payload).hexdigest()
    sig = priv.sign(digest.encode())
    report["signature"] = sig.hex()
    report["payload_sha256"] = digest
    return report


def verify_report(report_path: str) -> bool:
    if not HAS_CRYPTO:
        print("[!] cryptography not installed — cannot verify")
        return False
    if not os.path.exists(PUB_KEY_PATH):
        print(f"[!] Public key not found at {PUB_KEY_PATH}")
        return False
    with open(report_path) as f:
        report = json.load(f)
    with open(PUB_KEY_PATH, "rb") as f:
        pub = load_pem_public_key(f.read())

    stored_sig = bytes.fromhex(report["signature"])
    payload = json.dumps(
        {k: v for k, v in report.items() if k not in ("signature", "payload_sha256")},
        sort_keys=True
    ).encode()
    digest = hashlib.sha256(payload).hexdigest()
    try:
        pub.verify(stored_sig, digest.encode())
        print("[+] Signature VALID")
        return True
    except InvalidSignature:
        print("[!] Signature INVALID — report may have been tampered")
        return False


def main():
    parser = argparse.ArgumentParser(description="Aether Air-Gap Compliance Reporter")
    parser.add_argument("--out",    help="Write report JSON to this path")
    parser.add_argument("--verify", help="Verify a saved report JSON file")
    args = parser.parse_args()

    if args.verify:
        ok = verify_report(args.verify)
        sys.exit(0 if ok else 1)

    print("[*] Aether Air-Gap Compliance Check")
    print(f"    Probing {len(PROBE_TARGETS)} public endpoints (timeout={TIMEOUT_S}s each)...\n")
    report = run_compliance_check()
    report = _sign_report(report)

    status_icon = "✓" if report["status"] == "COMPLIANT" else "⚠"
    print(f"\n  {status_icon} Status: {report['status']}")
    if report.get("signature"):
        print(f"    Signed with Ed25519 (sha256: {report.get('payload_sha256','')[:16]}...)")
    else:
        print("    WARNING: report is unsigned")

    if args.out:
        with open(args.out, "w") as f:
            json.dump(report, f, indent=2)
        print(f"[+] Report saved to {args.out}")
    else:
        print(f"\n{json.dumps(report, indent=2)}")


if __name__ == "__main__":
    main()
