"""
action_log.py — Real remediation execution log.

Every action Aether takes — auto-executed or operator-approved — is written here
with the actual shell output so you have a real audit trail, not just ACP metadata.
"""
import json
import os
import subprocess
from datetime import datetime, timezone

LOG_PATH = os.path.join(os.path.dirname(__file__), "action_log.jsonl")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def execute_and_log(acp_id: str, action: str, steps: list[dict],
                    executed_by: str, fault_class: str, severity: str) -> dict:
    """
    Runs each step's command via shell, captures real stdout/stderr,
    appends the full record to action_log.jsonl, and returns the entry.

    executed_by: "AUTO" (inference engine) or "OPERATOR" (dashboard Approve click)
    """
    results = []
    any_success = False
    any_fail = False

    for step in steps:
        cmd = step.get("command", "")
        desc = step.get("description", "")
        if not cmd or cmd.startswith("#"):
            results.append({"description": desc, "command": cmd,
                             "skipped": True, "note": "comment-only command"})
            continue
        try:
            proc = subprocess.run(
                cmd, shell=True, capture_output=True, text=True, timeout=15
            )
            success = proc.returncode == 0
            if success:
                any_success = True
            else:
                any_fail = True
            results.append({
                "description": desc,
                "command":     cmd,
                "rc":          proc.returncode,
                "stdout":      proc.stdout.strip()[-2000:] if proc.stdout else "",
                "stderr":      proc.stderr.strip()[-1000:] if proc.stderr else "",
                "success":     success,
            })
        except subprocess.TimeoutExpired:
            any_fail = True
            results.append({
                "description": desc,
                "command":     cmd,
                "rc":          -1,
                "stderr":      "TIMEOUT after 15s",
                "success":     False,
            })
        except Exception as exc:
            any_fail = True
            results.append({
                "description": desc,
                "command":     cmd,
                "rc":          -1,
                "stderr":      str(exc),
                "success":     False,
            })

    if any_fail and not any_success:
        overall = "FAILED"
    elif any_fail:
        overall = "PARTIAL"
    else:
        overall = "SUCCESS"

    entry = {
        "timestamp":   _now(),
        "acp_id":      acp_id,
        "fault_class": fault_class,
        "severity":    severity,
        "action":      action,
        "executed_by": executed_by,
        "overall":     overall,
        "steps":       results,
    }

    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "a") as f:
        f.write(json.dumps(entry) + "\n")

    return entry


def log_rejected(acp_id: str, action: str, fault_class: str, severity: str) -> dict:
    """Write a rejection (no commands run) to the action log."""
    entry = {
        "timestamp":   _now(),
        "acp_id":      acp_id,
        "fault_class": fault_class,
        "severity":    severity,
        "action":      action,
        "executed_by": "OPERATOR",
        "overall":     "REJECTED",
        "steps":       [],
    }
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "a") as f:
        f.write(json.dumps(entry) + "\n")
    return entry


def read_log(limit: int = 100) -> list[dict]:
    """Return the most recent `limit` entries, newest first."""
    if not os.path.exists(LOG_PATH):
        return []
    entries = []
    with open(LOG_PATH) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return list(reversed(entries[-limit:]))
