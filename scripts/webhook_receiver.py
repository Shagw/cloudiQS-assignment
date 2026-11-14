"""Tiny local webhook receiver for testing: verifies the HMAC signature and prints each alert.

    RISK_WEBHOOK_SECRET=dev-secret uv run python scripts/webhook_receiver.py
"""

import hashlib
import hmac
import json
import os
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

SECRET = os.environ.get("RISK_WEBHOOK_SECRET", "")
MAX_SKEW = 300


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if SECRET:
            ts = self.headers.get("X-Risk-Timestamp", "0")
            expected = "sha256=" + hmac.new(SECRET.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, self.headers.get("X-Risk-Signature", "")) or abs(time.time() - int(ts)) > MAX_SKEW:
                self.send_response(401)
                self.end_headers()
                print("REJECTED: bad signature")
                return
        alert = json.loads(body)
        print(f"[{alert['risk_level'].upper()}] {alert['transaction']['transaction_id']}: {'; '.join(alert['reasons'])}")
        self.send_response(204)
        self.end_headers()

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 9000))
    print(f"listening on http://localhost:{port} (signature check {'on' if SECRET else 'OFF'})")
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
