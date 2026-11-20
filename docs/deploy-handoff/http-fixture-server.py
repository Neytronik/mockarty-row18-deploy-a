#!/usr/bin/env python3
"""Tiny acceptance-surface fixture for test_acceptance_and_handoff.sh.

Serves /ok with 200 and /unavailable with 503 on an ephemeral loopback port so
the acceptance script's a1 branch can be exercised hermetically. The port is
written to the path given as argv[1]. Not used against the deploy stand.
"""
import http.server
import socketserver
import sys


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - http.server API
        if self.path.startswith("/unavailable"):
            body, status = b"deploy stand unavailable", 503
        else:
            body, status = b"ok", 200
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # keep the fixture quiet
        pass


def main():
    server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    with open(sys.argv[1], "w") as handle:
        handle.write(str(server.server_address[1]))
    server.serve_forever()


if __name__ == "__main__":
    main()
