#!/usr/bin/env bash
# =============================================================================
# run.sh — Start (or stop) the Project Aether stack (PS-13)
#
# Usage:
#   ./run.sh                  SYNTHETIC mode (no sudo, no Containerlab) — always
#                             works. This is the default demo path.
#   ./run.sh --clab           Deploy (or reuse) the real Containerlab topology +
#                             MPLS/VRF + SD-WAN overlay + QoS, and feed the
#                             dashboard REAL FRR telemetry via the exporter. Uses
#                             sudo only if Docker needs it (not needed when you
#                             are in the docker group). Falls back to synthetic
#                             if containerlab/docker are unavailable.
#   ./run.sh --airgap         Run the SYNTHETIC stack inside a zero-egress
#                             network namespace (loopback only). Proves true
#                             air-gap: the compliance probe reports COMPLIANT and
#                             a signed report is written to
#                             .logs/airgap_compliance.json. The dashboard is then
#                             reachable only from inside that namespace.
#   ./run.sh --clab --airgap  Real Containerlab AND a genuinely air-gapped stack:
#                             the lab is deployed on the host (its data-plane has
#                             no internet route), then the whole copilot stack
#                             re-launches inside a zero-egress network namespace.
#                             Real FRR telemetry still flows (docker exec uses the
#                             unix socket, which crosses namespaces), the
#                             compliance probe genuinely reports COMPLIANT, and a
#                             loopback-only unix-socket bridge keeps the dashboard
#                             browsable from the host at http://localhost:8080.
#   ./run.sh stop             Stop the Aether processes (leaves the lab running).
#   ./run.sh destroy          Stop everything AND tear down the Containerlab lab.
#
# Always started:  Ollama (LLM) · NetFlow sim · traffic gen · fault streamer
#                  + inference · NOC dashboard (:8080)
# -----------------------------------------------------------------------------
set -uo pipefail   # NOTE: no -e; we want graceful fallback, not hard exits

REPO="$(cd "$(dirname "$0")" && pwd)"
LOGS="$REPO/.logs"

# Air-gap: every child process must use ONLY local model caches — never let
# huggingface_hub/transformers attempt an online version check at runtime.
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1
TOPOLOGY="$REPO/phase1-simulation/topology/aether-lab.clab.yml"
TELEMETRY="$REPO/phase2-telemetry"
mkdir -p "$LOGS"

log()  { echo ""; echo "==> $*"; }
info() { echo "    $*"; }

kill_proc() {
  local pattern="$1" pids
  pids=$(pgrep -f "$pattern" 2>/dev/null || true)
  if [ -n "$pids" ]; then
    info "stopping $pattern (PIDs $pids)"
    kill $pids 2>/dev/null || true
  fi
}

stop_all() {
  log "Stopping Project Aether…"
  kill_proc "phase5-dashboard/app.py"
  kill_proc "phase3-models/fault_streamer.py"
  kill_proc "phase2-telemetry/netflow_simulator.py"
  kill_proc "phase2-telemetry/traffic_generator.py"
  kill_proc "phase2-telemetry/exporter.py"
  kill_proc "phase5-dashboard/netns_bridge.py"
  kill_proc "topology/lab_traffic.sh"
  kill_proc "ollama serve"   # includes any namespace-local instance from --airgap
  info "Containerlab topology left running for fast reuse — './run.sh destroy' to remove it."
  info "done."
}

destroy_clab() {
  command -v containerlab >/dev/null 2>&1 || { info "containerlab not installed."; return; }
  local CL="containerlab"; docker info >/dev/null 2>&1 || CL="sudo containerlab"
  log "Destroying Containerlab topology…"
  $CL destroy -t "$TOPOLOGY" --cleanup 2>&1 | sed 's/^/    /' || true
  docker network rm clab >/dev/null 2>&1 || true
}

start_bg() {
  local name="$1"; shift
  nohup "$@" > "$LOGS/${name}.log" 2>&1 &
  info "[$!] $name  →  $LOGS/${name}.log"
}

wait_http() {  # url label tries
  local url="$1" label="$2" tries="${3:-25}" i
  for ((i=1; i<=tries; i++)); do
    curl -s "$url" >/dev/null 2>&1 && { info "$label ready"; return 0; }
    sleep 1
  done
  info "!! $label not responding at $url after ${tries}s"
  return 1
}

# ── arg parsing (flags compose: --clab and --airgap can both be passed) ────────
WANT_CLAB=0
WANT_AIRGAP=0
for arg in "$@"; do
  case "$arg" in
    stop)            stop_all; exit 0 ;;
    destroy)         stop_all; destroy_clab; exit 0 ;;
    --clab|--clab=1) WANT_CLAB=1 ;;
    --no-clab)       WANT_CLAB=0 ;;
    --airgap)        WANT_AIRGAP=1 ;;
    -h|--help)       grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  esac
done

# ── [--airgap, synthetic] re-launch inside a zero-egress network namespace ─────
# A real air-gapped deployment has no outbound route. For the synthetic stack we
# reproduce that with a rootless user+network namespace that has only loopback —
# localhost services keep working, but every external host is unreachable, so the
# compliance probe genuinely reports COMPLIANT. (For --clab --airgap the lab is
# deployed on the host FIRST, then the stack re-execs into the namespace — see
# the Containerlab block below.)
if [ "$WANT_AIRGAP" = 1 ] && [ "$WANT_CLAB" = 0 ] && [ -z "${AETHER_NETNS:-}" ]; then
  if ! command -v unshare >/dev/null 2>&1; then
    echo "!! 'unshare' not available — cannot create an air-gapped namespace."; exit 1
  fi
  echo "==> --airgap: re-launching the synthetic stack inside a loopback-only namespace…"
  exec unshare -rn env AETHER_NETNS=1 bash "$0" --airgap
fi
if [ -n "${AETHER_NETNS:-}" ]; then
  ip link set lo up 2>/dev/null || true
fi

# ── clean previous run ────────────────────────────────────────────────────────
log "Stopping any previous Aether processes…"
kill_proc "phase5-dashboard/app.py"
kill_proc "phase3-models/fault_streamer.py"
kill_proc "phase2-telemetry/netflow_simulator.py"
kill_proc "phase2-telemetry/traffic_generator.py"
kill_proc "phase2-telemetry/exporter.py"
# Bridges are killed only by the host phase: the netns phase runs right after the
# host phase started the host-side bridge, and must not tear it down.
[ -z "${AETHER_NETNS:-}" ] && kill_proc "phase5-dashboard/netns_bridge.py"
sleep 1

CLAB_UP=0

# ── [optional] Containerlab + telemetry stack ────────────────────────────────
if [ "$WANT_CLAB" = 1 ] && [ -n "${AETHER_NETNS:-}" ] && [ "${AETHER_CLAB_UP:-0}" = 1 ]; then
  # Netns phase of --clab --airgap: the host phase already deployed the lab and
  # started the host-side bridge. Docker's unix socket crosses namespaces, so
  # 'docker exec' scraping keeps working — real telemetry inside the air gap.
  CLAB_UP=1
  log "Air-gapped Containerlab mode (inside zero-egress namespace)."
  info "Starting FRR telemetry exporter (real counters via the docker socket)…"
  start_bg "exporter" env LAB=aether python3 "$TELEMETRY/exporter.py"
  wait_http "http://localhost:8000/metrics" "exporter" 15 || true
  info "Starting ingress bridge (netns side) so the host browser can reach :8080…"
  start_bg "bridge_netns" python3 "$REPO/phase5-dashboard/netns_bridge.py" \
    --unix-to-tcp "$LOGS/dash.sock" 127.0.0.1:8080
  info "Starting in-lab VPN traffic (ping-only, air-gap safe)…"
  start_bg "lab_traffic" env LAB=aether bash "$REPO/phase1-simulation/topology/lab_traffic.sh"
elif [ "$WANT_CLAB" = 1 ]; then
  log "Containerlab requested — checking prerequisites…"
  if ! command -v containerlab >/dev/null 2>&1 || ! command -v docker >/dev/null 2>&1; then
    info "!! containerlab/docker not installed — falling back to synthetic mode"
    info "   (the AI stack runs fine on synthetic data; topology is optional)"
  else
    # Use sudo only if Docker is NOT usable directly (i.e. user not in docker group).
    # On a docker-group machine, containerlab needs no sudo at all.
    if docker info >/dev/null 2>&1; then
      DK="docker"; CL="containerlab"
      info "docker usable without sudo — running containerlab unprivileged"
    else
      DK="sudo docker"; CL="sudo containerlab"
      info "docker needs sudo — you may be prompted for your password"
    fi

    # --clab --airgap: if the lab network doesn't exist yet, create it INTERNAL
    # (no NAT/gateway) as defense-in-depth for the data-plane. The stack-level
    # air gap happens after deploy via the zero-egress namespace re-exec below.
    if [ "$WANT_AIRGAP" = 1 ] && ! $DK network inspect clab >/dev/null 2>&1; then
      $DK network create --internal --subnet 172.20.20.0/24 clab >/dev/null 2>&1 \
        && info "air-gap: created INTERNAL 'clab' network (no egress)"
    fi

    # Reuse an already-running topology if present (fast, non-disruptive); else deploy.
    if $CL inspect -t "$TOPOLOGY" 2>/dev/null | grep -q running; then
      CLAB_UP=1
      info "Reusing already-deployed topology (clab-aether-*)."
    else
      info "Deploying topology ($CL deploy)…"
      $CL destroy -t "$TOPOLOGY" --cleanup >/dev/null 2>&1 || true
      if $CL deploy -t "$TOPOLOGY"; then
        CLAB_UP=1
        info "Applying MPLS / VRF / SD-WAN overlay / QoS…"
        ( cd "$REPO/phase1-simulation/topology" \
          && LAB=aether bash chunk3-setup.sh \
          && LAB=aether bash overlay-setup.sh \
          && LAB=aether bash qos-setup.sh ) || info "!! post-deploy config had errors (continuing)"
      else
        info "!! containerlab deploy failed — falling back to synthetic mode"
      fi
    fi

    # --clab --airgap: lab is up on the host; now air-gap the STACK by re-exec'ing
    # into a zero-egress namespace. Real telemetry keeps flowing (docker exec is a
    # unix socket, which crosses network namespaces), and a loopback-only bridge
    # keeps :8080 browsable from the host.
    if [ "$CLAB_UP" = 1 ] && [ "$WANT_AIRGAP" = 1 ]; then
      if ! command -v unshare >/dev/null 2>&1; then
        echo "!! 'unshare' not available — cannot create an air-gapped namespace."; exit 1
      fi
      info "Verifying the lab data-plane has no internet route…"
      if $DK exec clab-aether-pe1 timeout 3 ping -c 1 -W 2 8.8.8.8 >/dev/null 2>&1; then
        info "!! WARNING: lab container reached 8.8.8.8 — lab egress is NOT blocked"
      else
        info "lab egress blocked (pe1 cannot reach 8.8.8.8) ✓"
      fi
      log "Re-launching the copilot stack inside a zero-egress namespace…"
      rm -f "$LOGS/dash.sock"
      start_bg "bridge_host" python3 "$REPO/phase5-dashboard/netns_bridge.py" \
        --tcp-to-unix 127.0.0.1:8080 "$LOGS/dash.sock"
      exec unshare -rn env AETHER_NETNS=1 AETHER_CLAB_UP=1 bash "$0" --clab --airgap
    fi

    if [ "$CLAB_UP" = 1 ]; then
      info "Starting FRR telemetry exporter (real interface/routing counters)…"
      start_bg "exporter" env LAB=aether python3 "$TELEMETRY/exporter.py"
      wait_http "http://localhost:8000/metrics" "exporter" 15 || true
      info "Starting in-lab VPN traffic (ping-only, air-gap safe)…"
      start_bg "lab_traffic" env LAB=aether bash "$REPO/phase1-simulation/topology/lab_traffic.sh"
      info "Starting Prometheus + Grafana (optional)…"
      ( cd "$TELEMETRY" && $DK compose up -d ) >/dev/null 2>&1 || info "!! docker compose failed (continuing)"
    fi
  fi
elif [ -n "${AETHER_NETNS:-}" ]; then
  log "Air-gapped synthetic mode (zero-egress namespace)."
else
  log "Synthetic mode (no Containerlab). Use './run.sh --clab' for the real lab."
fi

# ── always-on services ───────────────────────────────────────────────────────
log "Starting NetFlow simulator (UDP :9995)…"
start_bg "netflow_simulator" python3 "$REPO/phase2-telemetry/netflow_simulator.py"

log "Starting traffic generator…"
start_bg "traffic_generator" python3 "$REPO/phase2-telemetry/traffic_generator.py"

log "Starting fault streamer + inference engine…"
start_bg "fault_streamer" python3 "$REPO/phase3-models/fault_streamer.py"

log "Checking Ollama (Mistral 7B)…"
# Check *reachability*, not just whether a process named 'ollama serve' exists —
# a stale or namespace-local instance can hold the process name without actually
# serving :11434 on this network namespace.
if curl -s -m 3 http://localhost:11434/api/tags >/dev/null 2>&1; then
  info "Ollama already reachable on :11434"
elif command -v ollama >/dev/null 2>&1; then
  info "Starting Ollama…"
  start_bg "ollama" ollama serve
  wait_http "http://localhost:11434/api/tags" "Ollama" 25 || true
else
  info "!! ollama not installed — the copilot will use its offline RAG fallback"
fi

log "Starting NOC Dashboard…"
sleep 2
start_bg "dashboard" python3 "$REPO/phase5-dashboard/app.py"

echo ""
info "waiting for dashboard on :8080 (loads ML models, ~5-15s)…"
if wait_http "http://localhost:8080/api/status" "Dashboard" 30; then
  if   [ -n "${AETHER_NETNS:-}" ] && [ "$CLAB_UP" = 1 ]; then _mode="AIR-GAPPED Containerlab (real lab, zero-egress stack)"
  elif [ -n "${AETHER_NETNS:-}" ];   then _mode="air-gapped synthetic (zero-egress namespace)"
  elif [ "$CLAB_UP" = 1 ]; then _mode="Containerlab + synthetic"
  else _mode="synthetic data"; fi
  echo ""
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "  Project Aether — up ($_mode)"
  echo ""
  echo "  NOC Dashboard   →  http://localhost:8080"
  [ "$CLAB_UP" = 1 ] && [ -z "${AETHER_NETNS:-}" ] && echo "  Prometheus      →  http://localhost:9090"
  [ "$CLAB_UP" = 1 ] && [ -z "${AETHER_NETNS:-}" ] && echo "  Grafana         →  http://localhost:3000  (admin/admin)"
  [ "$CLAB_UP" = 1 ] && echo "  Exporter        →  http://localhost:8000/metrics"
  echo "  LLM (Ollama)    →  http://localhost:11434"
  echo ""
  echo "  Logs   →  $LOGS/      Stop   →  ./run.sh stop"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo ""
  curl -s http://localhost:8080/api/status | python3 -m json.tool 2>/dev/null || true

  # ── air-gap reporting ────────────────────────────────────────────────────────
  if [ -n "${AETHER_NETNS:-}" ]; then
    echo ""
    log "Air-gap verification (running inside zero-egress namespace)…"
    ( cd "$REPO/phase3-models" && python3 airgap_compliance.py --out "$LOGS/airgap_compliance.json" ) \
      | sed 's/^/    /' || true
    info "Signed report → $LOGS/airgap_compliance.json"
    if [ "$CLAB_UP" = 1 ]; then
      info "Real Containerlab telemetry flows via the docker unix socket (no network)."
      info "Dashboard is browsable from the HOST at http://localhost:8080 through the"
      info "loopback-only ingress bridge (no egress path for the stack)."
    else
      info "The dashboard is namespace-local (the air gap). Browse it from inside"
      info "the namespace, or capture it headless from within."
    fi
  fi
else
  echo "!! Dashboard did not come up — check $LOGS/dashboard.log"
  exit 1
fi
