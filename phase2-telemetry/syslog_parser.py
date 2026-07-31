#!/usr/bin/env python3
# =============================================================================
# syslog_parser.py — FRR BGP/OSPF Adjacency Event Parser
#
# Tails FRR log files and extracts BGP neighbor up/down transitions and
# OSPF adjacency state changes via regex. Emits structured events that
# feed the routing_instability ML channel (existing channel — new data source).
#
# Works on forwarded log copies or direct FRR log paths.  No elevated
# syslog access required — just read permission on the log file.
#
# Usage:
#   python3 syslog_parser.py --log /var/log/frr/frr.log
#   python3 syslog_parser.py --log /var/log/frr/frr.log --follow     # tail -f mode
#   python3 syslog_parser.py --demo                                   # synthetic demo
# =============================================================================
import re
import os
import sys
import time
import json
import argparse
from datetime import datetime, timezone
from collections import deque

# ── Patterns ─────────────────────────────────────────────────────────────────
# FRR native BGP state: "BGP: 10.0.0.2 went from Established to Idle"
# FRR native BGP state (with instance tag): "BGP: [PJCS3-VECNA] 3.3.3.3 went from Established to Idle"
BGP_STATE_RE = re.compile(
    r"BGP:(?:\s+\[[^\]]+\])?\s+(?P<peer>[\d\.]+)\s+went from\s+(?P<from_state>\S+)\s+to\s+(?P<to_state>\S+)",
    re.IGNORECASE,
)
# Syslog-style: "frr bgpd: %BGP-5-ADJCHANGE: neighbor 10.0.0.1 Up"
# or rsyslog:   "bgpd[123]: %BGP-5-ADJCHANGE: neighbor 10.0.0.1 Down"
BGP_ADJCHANGE_RE = re.compile(
    r"(?:bgp[^\s]*|BGP)[^\n]*?(?:ADJCHANGE|neighbor)\s+(?P<peer>[\d\.]+)\s+(?P<to_state>Up|Down|Established|Idle|Active)",
    re.IGNORECASE,
)
# FRR OSPF: "OSPF: 10.0.0.1: 0.0.0.1 State change Full -> Down"
OSPF_STATE_RE = re.compile(
    r"OSPF:.*?(?P<neighbor>[\d\.]+)\s+State change\s+(?P<from_state>\S+)\s+->\s+(?P<to_state>\S+)",
    re.IGNORECASE,
)
# BGP NOTIFICATION message
BGP_NOTIF_RE = re.compile(
    r"(?:BGP|bgp).*?NOTIFICATION.*?neighbor\s+(?P<peer>[\d\.]+)",
    re.IGNORECASE,
)
# Timestamp prefix (FRR default): "2024/06/27 12:34:56"
TIMESTAMP_RE = re.compile(r"^(\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}:\d{2})")

# ── Event types ───────────────────────────────────────────────────────────────
EVENT_BGP_DOWN  = "bgp_neighbor_down"
EVENT_BGP_UP    = "bgp_neighbor_up"
EVENT_OSPF_DOWN = "ospf_adjacency_down"
EVENT_OSPF_UP   = "ospf_adjacency_up"
EVENT_BGP_NOTIF = "bgp_notification"

# States that mean the session is lost
_BGP_DOWN_STATES  = {"idle", "active", "connect", "opensent", "openconfirm", "down"}  # "down" for syslog ADJCHANGE
_OSPF_DOWN_STATES = {"down", "attempt", "init", "2-way", "exstart", "exchange", "loading"}


class SyslogParser:
    """
    Parses FRR log lines and emits structured routing-instability events.
    Maintains a rolling event buffer (routing_instability_channel) that the
    data_collector / inference_engine can poll.
    """

    def __init__(self, buffer_size=200):
        self.events: deque = deque(maxlen=buffer_size)
        self._bgp_peer_states: dict[str, str] = {}
        self._ospf_nbr_states: dict[str, str] = {}

    def parse_line(self, line: str) -> dict | None:
        line = line.strip()
        if not line:
            return None

        ts_match = TIMESTAMP_RE.match(line)
        ts = ts_match.group(1) if ts_match else datetime.now(timezone.utc).isoformat()

        event = None

        # BGP state change (native FRR format OR syslog adjchange format)
        m = BGP_STATE_RE.search(line) or BGP_ADJCHANGE_RE.search(line)
        if m:
            peer = m.group("peer")
            to_state = m.group("to_state").lower()
            try:
                from_state = m.group("from_state").lower()
            except IndexError:
                from_state = self._bgp_peer_states.get(peer, "unknown")
            prev = self._bgp_peer_states.get(peer, "unknown")
            self._bgp_peer_states[peer] = to_state
            if to_state.lower() in _BGP_DOWN_STATES:
                event = {
                    "type": EVENT_BGP_DOWN, "peer": peer,
                    "from_state": from_state, "to_state": to_state,
                    "timestamp": ts, "severity": "HIGH",
                    "routing_instability_score": 1.0,
                }
            elif to_state in ("established", "up"):
                event = {
                    "type": EVENT_BGP_UP, "peer": peer,
                    "from_state": from_state, "to_state": to_state,
                    "timestamp": ts, "severity": "LOW",
                    "routing_instability_score": 0.1,
                }
            if event:
                self.events.append(event)
                return event

        # OSPF state change
        m = OSPF_STATE_RE.search(line)
        if m:
            nbr = m.group("neighbor")
            to_state = m.group("to_state").lower()
            from_state = m.group("from_state").lower()
            self._ospf_nbr_states[nbr] = to_state
            if to_state in _OSPF_DOWN_STATES and from_state == "full":
                event = {
                    "type": EVENT_OSPF_DOWN, "neighbor": nbr,
                    "from_state": from_state, "to_state": to_state,
                    "timestamp": ts, "severity": "HIGH",
                    "routing_instability_score": 0.85,
                }
            elif to_state == "full":
                event = {
                    "type": EVENT_OSPF_UP, "neighbor": nbr,
                    "from_state": from_state, "to_state": to_state,
                    "timestamp": ts, "severity": "LOW",
                    "routing_instability_score": 0.05,
                }
            if event:
                self.events.append(event)
                return event

        # BGP NOTIFICATION (session-level error)
        m = BGP_NOTIF_RE.search(line)
        if m:
            event = {
                "type": EVENT_BGP_NOTIF, "peer": m.group("peer"),
                "timestamp": ts, "severity": "MEDIUM",
                "routing_instability_score": 0.6,
                "raw": line[:200],
            }
            self.events.append(event)
            return event

        return None

    def get_recent_instability_score(self, window_seconds=30) -> float:
        """
        Returns a 0–1 routing instability score for the last `window_seconds`.
        Weighted sum of event scores, capped at 1.0.  Used to feed the
        routing_instability ML channel.
        """
        cutoff = time.time() - window_seconds
        score = 0.0
        for ev in reversed(self.events):
            try:
                ev_ts = datetime.fromisoformat(ev["timestamp"].replace("/", "-").replace(" ", "T"))
                ev_epoch = ev_ts.timestamp()
            except Exception:
                continue
            if ev_epoch < cutoff:
                break
            score += ev.get("routing_instability_score", 0.0)
        return min(score, 1.0)

    def tail_file(self, path: str, poll_interval=0.25, callback=None):
        """Tail a log file, calling callback(event) for each parsed event."""
        print(f"[syslog_parser] Tailing {path}...")
        with open(path, "r") as f:
            f.seek(0, 2)  # jump to end
            while True:
                line = f.readline()
                if line:
                    ev = self.parse_line(line)
                    if ev and callback:
                        callback(ev)
                else:
                    time.sleep(poll_interval)

    def parse_file(self, path: str) -> list[dict]:
        """Parse an entire existing log file and return all events."""
        events = []
        with open(path) as f:
            for line in f:
                ev = self.parse_line(line)
                if ev:
                    events.append(ev)
        return events


def _demo():
    print("[*] FRR Syslog Parser — demo mode\n")
    sample_lines = [
        "2024/06/27 12:00:00 BGP: 10.0.0.2 went from Established to Idle",
        "2024/06/27 12:00:05 BGP: 10.0.0.2 went from Idle to Active",
        "2024/06/27 12:00:10 OSPF: 192.168.1.1: 10.0.0.3 State change Full -> Down",
        "2024/06/27 12:00:15 BGP: %NOTIFICATION: sent to neighbor 10.0.0.4 3/3 (Update Message Error/Bad Attribute List) 0 bytes",
        "2024/06/27 12:00:30 BGP: 10.0.0.2 went from Active to OpenSent",
        "2024/06/27 12:00:35 BGP: 10.0.0.2 went from OpenSent to Established",
        "2024/06/27 12:00:40 OSPF: 192.168.1.1: 10.0.0.3 State change Down -> Full",
    ]
    parser = SyslogParser()
    for line in sample_lines:
        ev = parser.parse_line(line)
        if ev:
            print(f"  [{ev['severity']:6s}] {ev['type']:25s} — score: {ev['routing_instability_score']:.2f}")
            print(f"           {json.dumps({k: v for k, v in ev.items() if k not in ('severity', 'type', 'routing_instability_score')})}")
    print(f"\n  Rolling instability score (30s window): {parser.get_recent_instability_score():.3f}")


def main():
    parser = argparse.ArgumentParser(description="FRR Syslog Parser — BGP/OSPF adjacency events")
    parser.add_argument("--log", help="Path to FRR log file")
    parser.add_argument("--follow", action="store_true", help="Tail the log file (follow mode)")
    parser.add_argument("--demo", action="store_true", help="Run with synthetic demo lines")
    args = parser.parse_args()

    if args.demo:
        _demo()
        return

    if not args.log:
        print("[-] Specify --log <path> or --demo")
        sys.exit(1)

    sparser = SyslogParser()

    def on_event(ev):
        print(f"[{ev['severity']}] {ev['type']} — {json.dumps(ev)}")

    if args.follow:
        sparser.tail_file(args.log, callback=on_event)
    else:
        events = sparser.parse_file(args.log)
        for ev in events:
            on_event(ev)
        print(f"\n[*] Parsed {len(events)} routing events from {args.log}")


if __name__ == "__main__":
    main()
