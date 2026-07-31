# =============================================================================
# taxonomy.py — Single Source of Truth for Aether's Fault Taxonomy
#
# Every component (fault injector → data collector labels → model training →
# inference → corroborator → ACP) must agree on:
#   - the integer class id a model predicts,
#   - the canonical label written by the fault injector / read from faults_log,
#   - the operator-facing display name,
#   - the mitigation action class that fault maps to.
#
# Before this module existed, train_models.py and inference_engine.py disagreed
# on what class id 2/4/5 meant, so a trained model was interpreted with the
# wrong labels. Import from here instead of redefining these maps anywhere.
#
# Zero dependencies (no torch / numpy) so it is safe to import everywhere.
# =============================================================================

# Canonical fault catalogue.
#   id       : integer class index the classifier predicts (and trains on)
#   label    : exactly what fault_injector.py writes to faults_log.csv
#   display  : human / operator-facing name shown in ACPs and the copilot
#   action   : mitigation action class (key into ACTION_POLICY below)
#   critical : escalates ACP severity to CRITICAL when detected
FAULTS = [
    {"id": 0, "label": "Healthy", "display": "Healthy",
     "action": "NO_ACTION", "critical": False},
    {"id": 1, "label": "latency", "display": "Latency Drift",
     "action": "REROUTE_BRANCH", "critical": False},
    {"id": 2, "label": "loss", "display": "Packet Loss",
     "action": "REROUTE_BRANCH", "critical": False},
    {"id": 3, "label": "corrupt", "display": "Frame Corruption",
     "action": "REROUTE_BRANCH", "critical": False},
    {"id": 4, "label": "rate", "display": "Congestion / Saturation",
     "action": "QOS_SHAPE_QUEUE", "critical": False},
    {"id": 5, "label": "flap", "display": "Control-Plane Flap",
     "action": "CORE_PATH_FAILOVER", "critical": True},
]

NUM_CLASSES = len(FAULTS)

# label (faults_log) -> id, used by the training data loader.
LABEL_TO_ID = {f["label"]: f["id"] for f in FAULTS}
# id -> record, used by inference to recover label / display / action.
ID_TO_INFO = {f["id"]: f for f in FAULTS}
# convenience maps
ID_TO_LABEL = {f["id"]: f["label"] for f in FAULTS}
ID_TO_DISPLAY = {f["id"]: f["display"] for f in FAULTS}
DISPLAY_TO_INFO = {f["display"]: f for f in FAULTS}

# -----------------------------------------------------------------------------
# OPERATOR-CONFIGURABLE AUTONOMY POLICY MATRIX
# Keyed by action class. The dial the operator owns: per action class, the
# minimum ML confidence required and whether the Edge Policy Engine may execute
# autonomously (auto_execute) versus surface a recommendation.
#
# Safety floors enforced in code regardless of this table:
#   - model disagreement always downgrades to RECOMMEND_ONLY
#   - everything auto-executed is logged and reversible
# -----------------------------------------------------------------------------
ACTION_POLICY = {
    "REROUTE_BRANCH": {
        "min_conf": 0.80, "auto_execute": True,
        "description": "Reroute traffic around a degraded branch link"},
    "QOS_SHAPE_QUEUE": {
        "min_conf": 0.75, "auto_execute": True,
        "description": "Shape / throttle non-critical queues during congestion"},
    "CORE_PATH_FAILOVER": {
        "min_conf": 0.90, "auto_execute": False,
        "description": "Fail a primary core link over to the backup path (hub/DC: never auto)"},
    "NODE_ISOLATION": {
        "min_conf": 0.99, "auto_execute": False,
        "description": "Isolate a suspected rogue / compromised node"},
    "NO_ACTION": {
        "min_conf": 1.0, "auto_execute": False,
        "description": "System nominal — no mitigation required"},
}


def info_for_id(class_id):
    """Return the taxonomy record for a predicted class id (falls back to Healthy)."""
    return ID_TO_INFO.get(int(class_id), ID_TO_INFO[0])


def policy_for_action(action):
    """Return the autonomy policy for an action class (falls back to manual)."""
    return ACTION_POLICY.get(action, {"min_conf": 1.0, "auto_execute": False,
                                       "description": "unknown action"})


if __name__ == "__main__":
    print(f"Aether fault taxonomy — {NUM_CLASSES} classes")
    for f in FAULTS:
        pol = ACTION_POLICY[f["action"]]
        print(f"  [{f['id']}] {f['label']:<8} → {f['display']:<24} "
              f"action={f['action']:<18} auto={pol['auto_execute']} "
              f"min_conf={pol['min_conf']}")
