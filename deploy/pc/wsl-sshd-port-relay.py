#!/usr/bin/env python3
"""WSL sshd port relay: 10.66.0.3:2222 -> 127.0.0.1:2222 (WSL via localhost forwarding).

Non-admin bridge so DBB3 can reach the WSL sshd over the WireGuard interface.
Runs in the foreground; supervise with Task Scheduler or a startup shortcut.

All endpoints are configurable via environment so the same file deploys to
any PC/WSL pair:
  WSL_RELAY_LISTEN_HOST (default 10.66.0.3, the WireGuard interface)
  WSL_RELAY_LISTEN_PORT (default 2222)
  WSL_RELAY_TARGET_HOST (default 127.0.0.1, WSL2 localhost forwarding)
  WSL_RELAY_TARGET_PORT (default 2222, the WSL sshd port)
"""
import os
import select
import socket
import socketserver
import sys

LISTEN_HOST = os.environ.get("WSL_RELAY_LISTEN_HOST", "10.66.0.3")
LISTEN_PORT = int(os.environ.get("WSL_RELAY_LISTEN_PORT", "2222"))
TARGET_HOST = os.environ.get("WSL_RELAY_TARGET_HOST", "127.0.0.1")
TARGET_PORT = int(os.environ.get("WSL_RELAY_TARGET_PORT", "2222"))


class _Relay(socketserver.BaseRequestHandler):
    def handle(self):
        client = self.request
        try:
            upstream = socket.create_connection(
                (TARGET_HOST, TARGET_PORT), timeout=10
            )
        except OSError:
            client.close()
            return
        try:
            client.setblocking(False)
            upstream.setblocking(False)
            sockets = [client, upstream]
            while True:
                readable, _, _ = select.select(sockets, [], [], 60)
                if not readable:
                    break
                for sock in readable:
                    try:
                        data = sock.recv(65536)
                    except (OSError, BlockingIOError):
                        continue
                    if not data:
                        client.close()
                        upstream.close()
                        return
                    peer = upstream if sock is client else client
                    try:
                        peer.sendall(data)
                    except OSError:
                        client.close()
                        upstream.close()
                        return
        except (OSError, TimeoutError):
            pass
        finally:
            client.close()
            try:
                upstream.close()
            except OSError:
                pass


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> int:
    try:
        server = _Server((LISTEN_HOST, LISTEN_PORT), _Relay)
    except OSError as exc:
        print(f"relay: cannot bind {LISTEN_HOST}:{LISTEN_PORT}: {exc}", flush=True)
        return 1
    print(f"relay: {LISTEN_HOST}:{LISTEN_PORT} -> {TARGET_HOST}:{TARGET_PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
