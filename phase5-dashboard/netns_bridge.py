#!/usr/bin/env python3
# =============================================================================
# netns_bridge.py — loopback-only ingress bridge for './run.sh --clab --airgap'
#
# In that mode the Aether stack runs inside a zero-egress network namespace
# (loopback only), so the host browser cannot reach the namespaced 127.0.0.1:8080
# directly. Unix-domain sockets are filesystem objects and cross network
# namespaces, so we chain:
#
#   host browser → TCP 127.0.0.1:8080 (host side) → unix socket → (netns side)
#                → TCP 127.0.0.1:8080 (dashboard inside the namespace)
#
# This is INGRESS-ONLY: the namespaced stack gains no outbound network route
# from it, and the host side binds strictly to 127.0.0.1. Zero dependencies.
#
# Usage:
#   netns_bridge.py --tcp-to-unix 127.0.0.1:8080 /path/to/dash.sock   (host side)
#   netns_bridge.py --unix-to-tcp /path/to/dash.sock 127.0.0.1:8080   (netns side)
# =============================================================================
import os
import socket
import sys
import threading


def pump(src: socket.socket, dst: socket.socket) -> None:
    """Copy bytes src→dst until EOF, then half-close the write side."""
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def splice(a: socket.socket, b: socket.socket) -> None:
    t = threading.Thread(target=pump, args=(a, b), daemon=True)
    t.start()
    pump(b, a)
    t.join()
    for s in (a, b):
        try:
            s.close()
        except OSError:
            pass


def serve(listener: socket.socket, connect):
    """Accept forever; per connection, dial the other side and splice."""
    listener.listen(16)
    while True:
        conn, _ = listener.accept()
        def handle(c=conn):
            try:
                peer = connect()
            except OSError as e:
                sys.stderr.write(f"[bridge] upstream connect failed: {e}\n")
                c.close()
                return
            splice(c, peer)
        threading.Thread(target=handle, daemon=True).start()


def parse_hostport(s: str):
    host, _, port = s.rpartition(":")
    return host, int(port)


def main() -> int:
    if len(sys.argv) != 4 or sys.argv[1] not in ("--tcp-to-unix", "--unix-to-tcp"):
        sys.stderr.write(__doc__ or "usage: see header\n")
        return 2

    mode = sys.argv[1]
    if mode == "--tcp-to-unix":
        host, port = parse_hostport(sys.argv[2])
        path = sys.argv[3]
        if host not in ("127.0.0.1", "localhost"):
            sys.stderr.write("[bridge] refusing to listen on a non-loopback address\n")
            return 2
        ls = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        ls.bind((host, port))
        print(f"[bridge] host side: {host}:{port} → {path}", flush=True)

        def connect():
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(path)
            return s
    else:  # --unix-to-tcp
        path = sys.argv[2]
        host, port = parse_hostport(sys.argv[3])
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        ls = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        ls.bind(path)
        os.chmod(path, 0o600)
        print(f"[bridge] netns side: {path} → {host}:{port}", flush=True)

        def connect():
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.connect((host, port))
            return s

    serve(ls, connect)
    return 0


if __name__ == "__main__":
    sys.exit(main())
