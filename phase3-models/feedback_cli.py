#!/usr/bin/env python3
# =============================================================================
# feedback_cli.py — Operator Accept/Reject Feedback Loop
#
# Reads recent ACPs from ikb/incidents.jsonl, lets the operator mark any alert
# accepted or rejected after the fact.  The label is written back to the JSONL
# entry and feeds the IKB's false-positive rate lookup in the Priority Scoring
# Function (PSF) — closing the feedback gap for recommend-only actions that
# never get auto-validated by the graph engine.
#
# Usage:
#   python3 feedback_cli.py              # interactive mode (list + prompt)
#   python3 feedback_cli.py --list       # just list recent ACPs
#   python3 feedback_cli.py --acp-id <id> --feedback accepted
#   python3 feedback_cli.py --acp-id <id> --feedback rejected
#   python3 feedback_cli.py --stats      # false-positive rate per fault class
# =============================================================================
import os
import sys
import json
import argparse
from datetime import datetime, timezone
from collections import defaultdict

IKB_LOG = os.path.join(os.path.dirname(__file__), "ikb", "incidents.jsonl")


def _read_log() -> list[dict]:
    if not os.path.exists(IKB_LOG):
        return []
    entries = []
    with open(IKB_LOG) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return entries


def _write_log(entries: list[dict]):
    os.makedirs(os.path.dirname(IKB_LOG), exist_ok=True)
    with open(IKB_LOG, "w") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


def _apply_feedback(acp_id: str, feedback: str) -> bool:
    """Write accept/reject label back to the JSONL entry. Returns True on success."""
    entries = _read_log()
    matched = False
    for entry in entries:
        if entry.get("acp_id", "").startswith(acp_id):
            entry["operator_feedback"] = feedback
            entry["feedback_timestamp"] = datetime.now(timezone.utc).isoformat()
            matched = True
            break
    if matched:
        _write_log(entries)
    return matched


def _list_acps(entries: list[dict], n: int = 20) -> None:
    recent = entries[-n:]
    if not recent:
        print("  (no ACPs logged yet)")
        return
    print(f"\n  {'#':<4} {'ACP ID':10s} {'Timestamp':26s} {'Severity':10s} {'Feedback':12s}")
    print(f"  {'─'*4} {'─'*10} {'─'*26} {'─'*10} {'─'*12}")
    for i, e in enumerate(reversed(recent), 1):
        acp_short = e.get("acp_id", "?")[:8]
        ts  = e.get("timestamp", "")[:19]
        sev = e.get("severity", "?")
        fb  = e.get("operator_feedback") or "pending"
        fb_icon = {"accepted": "✓ accepted", "rejected": "✗ rejected"}.get(fb, f"  {fb}")
        print(f"  {i:<4} {acp_short:10s} {ts:26s} {sev:10s} {fb_icon}")
    print()


def _stats(entries: list[dict]) -> None:
    if not entries:
        print("  No ACP data.")
        return
    by_class: dict[str, list[str]] = defaultdict(list)
    for e in entries:
        fault = e.get("fault_class", e.get("severity", "unknown"))
        fb = e.get("operator_feedback")
        if fb:
            by_class[fault].append(fb)

    print("\n  False-positive rates by fault class (operator-labelled):")
    print(f"  {'Class':25s} {'Total':7s} {'Rejected (FP)':14s} {'FP Rate':8s}")
    print(f"  {'─'*25} {'─'*7} {'─'*14} {'─'*8}")
    total_labelled = 0
    for fault, labels in sorted(by_class.items()):
        total = len(labels)
        rejected = labels.count("rejected")
        fp_rate = rejected / total if total else 0.0
        total_labelled += total
        print(f"  {fault:25s} {total:7d} {rejected:14d} {fp_rate:8.1%}")
    pending = sum(1 for e in entries if not e.get("operator_feedback"))
    print(f"\n  Total ACPs: {len(entries)}  |  Labelled: {total_labelled}  |  Pending: {pending}")


def _interactive(entries: list[dict]) -> None:
    print("\n=== Aether Operator Feedback CLI ===")
    print(f"  IKB log: {IKB_LOG}\n")
    _list_acps(entries, n=20)

    if not entries:
        return

    print("  Enter ACP ID prefix (first 4–8 chars) to label it,")
    print("  or press Enter to exit.\n")
    while True:
        try:
            raw = input("  ACP ID > ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not raw:
            break

        # Find match
        matches = [e for e in entries if e.get("acp_id", "").startswith(raw)]
        if not matches:
            print(f"  [!] No ACP starting with '{raw}'")
            continue
        e = matches[-1]  # most recent if multiple
        print(f"  ACP: {e['acp_id']} | {e.get('timestamp','')[:19]} | {e.get('severity','?')}")
        current_fb = e.get("operator_feedback")
        if current_fb:
            print(f"  Already labelled: {current_fb}")
        try:
            fb = input("  Label [a]ccepted / [r]ejected / skip > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            break
        if fb in ("a", "accepted"):
            if _apply_feedback(raw, "accepted"):
                print("  ✓ Marked as ACCEPTED")
        elif fb in ("r", "rejected"):
            if _apply_feedback(raw, "rejected"):
                print("  ✗ Marked as REJECTED (false positive)")
        else:
            print("  Skipped.")
        print()


def main():
    parser = argparse.ArgumentParser(description="Aether Operator Feedback CLI")
    parser.add_argument("--list",     action="store_true", help="List recent ACPs")
    parser.add_argument("--stats",    action="store_true", help="Show false-positive rate stats")
    parser.add_argument("--acp-id",   help="ACP ID prefix to label")
    parser.add_argument("--feedback", choices=["accepted", "rejected"], help="Feedback label")
    args = parser.parse_args()

    entries = _read_log()

    if args.acp_id and args.feedback:
        ok = _apply_feedback(args.acp_id, args.feedback)
        if ok:
            print(f"[+] ACP {args.acp_id} marked as {args.feedback}")
        else:
            print(f"[-] ACP ID '{args.acp_id}' not found in {IKB_LOG}")
            sys.exit(1)
    elif args.list:
        _list_acps(entries)
    elif args.stats:
        _stats(entries)
    else:
        _interactive(entries)


if __name__ == "__main__":
    main()
