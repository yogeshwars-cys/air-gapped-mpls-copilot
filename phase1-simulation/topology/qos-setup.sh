#!/usr/bin/env bash
# =============================================================================
# qos-setup.sh — Baseline QoS policies for Project Aether (PS-13 Objective 1)
#
# Installs an HTB hierarchy on each PE→CE egress interface so QoS is in force
# from startup (not only as a reactive remediation). Three classes per link:
#
#   1:10  PRIORITY   — VoIP + routing control plane   (guaranteed, low latency)
#   1:20  INTERACTIVE — database / transactional        (assured rate)
#   1:30  BULK       — backups / bulk transfer (default) (capped, best-effort)
#
# This is the steady-state policy. The QOS_SHAPE_QUEUE remediation tightens the
# BULK ceiling further when congestion is predicted; this script establishes the
# baseline the remediation builds on.
#
#   sudo containerlab deploy -t aether-lab.clab.yml
#   LAB=aether ./qos-setup.sh
# =============================================================================
set -euo pipefail

LAB="${LAB:-aether}"
P() { echo "clab-${LAB}-$1"; }

# PE egress interfaces facing customer sites (PE-CE access links, 5 Mbit each)
#   pe1 eth2 -> ce-branch1 | pe1 eth3 -> ce-hub
#   pe2 eth2 -> ce-branch2 | pe2 eth3 -> ce-dc
PE_LINKS=( "pe1:eth2" "pe1:eth3" "pe2:eth2" "pe2:eth3" )

LINK_RATE="5mbit"
PRIO_RATE="2mbit"     # VoIP + control
INTER_RATE="2mbit"    # database / interactive
BULK_RATE="1mbit"     # bulk/backup baseline (ceil lets it borrow when idle)
BULK_CEIL="3mbit"

apply_qos() {
  local box="$1" ifc="$2"
  # Idempotent: clear any existing root qdisc first
  docker exec "$box" tc qdisc del dev "$ifc" root 2>/dev/null || true

  docker exec "$box" tc qdisc add dev "$ifc" root handle 1: htb default 30
  docker exec "$box" tc class add dev "$ifc" parent 1: classid 1:1 \
      htb rate "$LINK_RATE" ceil "$LINK_RATE"

  docker exec "$box" tc class add dev "$ifc" parent 1:1 classid 1:10 \
      htb rate "$PRIO_RATE"  ceil "$LINK_RATE" prio 0
  docker exec "$box" tc class add dev "$ifc" parent 1:1 classid 1:20 \
      htb rate "$INTER_RATE" ceil "$LINK_RATE" prio 1
  docker exec "$box" tc class add dev "$ifc" parent 1:1 classid 1:30 \
      htb rate "$BULK_RATE"  ceil "$BULK_CEIL" prio 2

  # Low-latency leaf qdisc on the priority class
  docker exec "$box" tc qdisc add dev "$ifc" parent 1:10 handle 10: sfq perturb 10

  # Classify: EF/CS6 (VoIP + control) -> 1:10, AF21 (interactive) -> 1:20
  docker exec "$box" tc filter add dev "$ifc" parent 1: protocol ip prio 1 \
      u32 match ip tos 0xb8 0xff flowid 1:10   # DSCP EF (46)
  docker exec "$box" tc filter add dev "$ifc" parent 1: protocol ip prio 1 \
      u32 match ip tos 0xc0 0xff flowid 1:10   # DSCP CS6 (routing control)
  docker exec "$box" tc filter add dev "$ifc" parent 1: protocol ip prio 2 \
      u32 match ip tos 0x48 0xff flowid 1:20   # DSCP AF21 (18)
}

echo "==> Installing baseline QoS on PE→CE egress interfaces (LAB=$LAB)"
for entry in "${PE_LINKS[@]}"; do
  node="${entry%%:*}"; ifc="${entry##*:}"
  echo "    $(P "$node")  $ifc  [PRIO $PRIO_RATE | INTER $INTER_RATE | BULK $BULK_RATE/$BULK_CEIL]"
  apply_qos "$(P "$node")" "$ifc"
done

echo
echo "==> Verify (pe1 eth2):"
docker exec "$(P pe1)" tc -s class show dev eth2 || true
echo
echo "Baseline QoS active. QOS_SHAPE_QUEUE remediation tightens 1:30 (BULK) on demand."
