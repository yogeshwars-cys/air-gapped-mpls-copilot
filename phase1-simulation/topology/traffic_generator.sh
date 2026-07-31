#!/usr/bin/env bash
# =============================================================================
# traffic_generator.sh — Simulates Enterprise & Government Traffic Profiles
#
# Profiles Simulated:
#   1. Voice & Video Teleconferencing (VTC/VoIP) - High-priority, continuous UDP
#   2. Database Replication & Backups - Periodic high-throughput TCP
#   3. Web Intranet & Directory Services - Bursty HTTP/HTTPS (curl)
#   4. Administrative Session Control - Low-volume interactive TCP (SSH emulation)
# =============================================================================
set -euo pipefail

LAB="${LAB:-aether}"
P() { echo "clab-${LAB}-$1"; }

# Check if containers are running
if ! docker ps | grep -q "$(P pe1)"; then
  echo "Error: Lab is not running. Deploy aether-lab.clab.yml first."
  exit 1
fi

echo "==> Installing iperf3 and curl on Customer Edge (CE) nodes if not present"
for ce in ce-branch1 ce-branch2 ce-hub ce-dc; do
  docker exec "$(P "$ce")" apk add --no-cache iperf3 curl >/dev/null 2>&1 || \
  docker exec "$(P "$ce")" apt-get update -y && docker exec "$(P "$ce")" apt-get install -y iperf3 curl >/dev/null 2>&1 || true
done

# We will use ce-dc and ce-hub as the main destinations (servers)
# We will use ce-branch1 and ce-branch2 as the sources (clients)

echo "==> Starting iperf3 servers on ce-dc and ce-hub (background)"
docker exec -d "$(P ce-dc)" iperf3 -s -p 5001
docker exec -d "$(P ce-dc)" iperf3 -s -p 5002
docker exec -d "$(P ce-hub)" iperf3 -s -p 5001

echo "==> Spawning traffic profiles..."

# -----------------------------------------------------------------------------
# PROFILE 1: Voice & Video Teleconferencing (VoIP/VTC)
# - Continuous UDP streams, low latency/packet loss requirements.
# - Branch 1 calling Data Center (500Kbps UDP, small packet size)
# - Branch 2 calling Hub (1.5Mbps UDP video call)
# -----------------------------------------------------------------------------
echo "    [+] Launching VoIP Profile (UDP, continuous)"
# Branch 1 -> DC (VoIP Audio)
docker exec -d "$(P ce-branch1)" iperf3 -c 14.14.14.14 -p 5001 -u -b 150k -l 172 -t 99999 --bidir
# Branch 2 -> Hub (VTC Video)
docker exec -d "$(P ce-branch2)" iperf3 -c 13.13.13.13 -p 5001 -u -b 1.5M -l 1200 -t 99999 --bidir

# -----------------------------------------------------------------------------
# PROFILE 2: Database Replication & Backups (TCP Bulk)
# - Running in loops: high bandwidth, bursts of TCP data.
# - Branch 1 syncing database to DC every 30 seconds
# -----------------------------------------------------------------------------
echo "    [+] Launching Database Replication & Bulk Storage Backup Simulator"
docker exec -d "$(P ce-branch1)" sh -c '
  while true; do
    echo "Starting DB sync backup..."
    iperf3 -c 14.14.14.14 -p 5002 -t 10 -b 5M
    sleep 30
  done
'

# -----------------------------------------------------------------------------
# PROFILE 3: HTTP/HTTPS Intranet & Directory Services (Web traffic)
# - Bursty curl requests to simulate users fetching intranet portals or LDAP checks.
# -----------------------------------------------------------------------------
echo "    [+] Launching HTTP Intranet/Web Simulator"
# Spin up a simple HTTP server on ce-dc for intranet simulation
docker exec -d "$(P ce-dc)" python3 -m http.server 8080 2>/dev/null || \
docker exec -d "$(P ce-dc)" sh -c 'while true; do echo -e "HTTP/1.1 200 OK\n\n Intranet Portal Active" | nc -l -p 8080; done'

# Branch 1 and Branch 2 querying the intranet server
docker exec -d "$(P ce-branch1)" sh -c '
  while true; do
    curl -s http://14.14.14.14:8080/ >/dev/null || true
    sleep $((RANDOM % 5 + 1))
  done
'
docker exec -d "$(P ce-branch2)" sh -c '
  while true; do
    curl -s http://14.14.14.14:8080/ >/dev/null || true
    sleep $((RANDOM % 8 + 2))
  done
'

# -----------------------------------------------------------------------------
# PROFILE 4: Secure Administrative Session (SSH interactive)
# - Interactive SSH-like sessions. Low bandwidth, sensitive to high latency.
# -----------------------------------------------------------------------------
echo "    [+] Launching Administrative SSH Simulation"
docker exec -d "$(P ce-branch2)" sh -c '
  while true; do
    # Simulates periodic interactive commands over TCP
    iperf3 -c 14.14.14.14 -p 5001 -t 3 -b 50k
    sleep $((RANDOM % 15 + 10))
  done
'

echo "==> Traffic simulation running successfully in the background!"
echo "    To verify: run 'docker exec clab-${LAB}-pe1 tcpdump -i eth2 -n'"
echo "    To stop: run 'killall iperf3' or rebuild containers."
