"""
CASE file/memory server - exposes case_tools.py's read-only functions
(list_directory, read_file, search_files, memory_reflect) over plain HTTP,
for a client that has NO local copy of the project files at all.

Why this exists: case_cli.py already reaches a remote model over the LAN
(the Mac mini's Bionic server) with no file access needed on that end -
this is the same idea applied to files. It's the missing piece for a real
Android/Termux port: a phone has no copy of Sati's project/memory files
and porting them there would just create a second, immediately-stale
copy. Instead, case_tools.py can be told (via CASE_FILE_SERVER_URL) to
proxy these four calls over HTTP to whichever machine actually has the
real files - normally this PC - so a phone client stays a thin, always-
current window onto the real data, the same way it's always been a thin
window onto the real model.

Run this ON THE MACHINE THAT HAS THE REAL FILES (this PC), bound to the
LAN so a phone/other machine on the same network can reach it:
    python case_file_server.py
Stdlib only (http.server) - no new dependency for a capability this small.

Security note: no auth, plain HTTP - fine for a private home LAN serving
read-only data (same trust model CASE already uses for the Mac mini's
inference endpoint), NOT meant to be exposed past your router.
"""

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import case_tools

PORT = 8765
HOST = "0.0.0.0"  # LAN-reachable, not just localhost - see module docstring

_ENDPOINTS = {
    "/list_directory": lambda q: case_tools.list_directory(q.get("path", [""])[0]),
    "/read_file": lambda q: case_tools.read_file(q.get("path", [""])[0]),
    "/search_files": lambda q: case_tools.search_files(q.get("query", [""])[0], q.get("path", [""])[0]),
    "/memory_reflect": lambda q: case_tools.memory_reflect(q.get("query", [""])[0]),
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # quiet by default - case_tools' own guards are what matter, not access logging

    def do_GET(self):
        parsed = urlparse(self.path)
        fn = _ENDPOINTS.get(parsed.path)
        if fn is None:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'{"error": "unknown endpoint"}')
            return
        query = parse_qs(parsed.query)
        try:
            result = fn(query)
        except Exception as e:
            result = f"ERROR: {type(e).__name__}: {e}"
        body = json.dumps({"result": result}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"CASE file server listening on http://{HOST}:{PORT} (Ctrl+C to stop)")
    print(f"Serving: {case_tools.get_project_root()} and {case_tools.get_memory_root()}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(0)


if __name__ == "__main__":
    main()
