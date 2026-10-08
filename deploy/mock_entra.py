"""A stand-in for the Microsoft Entra ID token endpoint. Development only.

Lets the whole `entra` path be exercised -- acquire, cache, send, refresh on
401 -- without a real tenant. It validates the client credentials so a
misconfigured OpsTrail fails here the way it would against Entra, rather than
appearing to work.

Deliberately NOT part of the application image: it is wired into
docker-compose.yml for the dev stack only.
"""

import json
import os
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs

EXPECTED_CLIENT_ID = os.getenv("MOCK_CLIENT_ID", "mock-client-id")
EXPECTED_SECRET = os.getenv("MOCK_CLIENT_SECRET", "mock-client-secret")
TOKEN_TTL = int(os.getenv("MOCK_TOKEN_TTL", "3600"))

# Counted so a test can assert the token really is cached rather than fetched
# per alert.
issued = 0


class Handler(BaseHTTPRequestHandler):
    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/stats"):
            self._json(200, {"tokens_issued": issued})
        else:
            self._json(404, {"error": "not_found"})

    def do_POST(self):  # noqa: N802
        global issued
        if not self.path.startswith("/token"):
            return self._json(404, {"error": "not_found"})

        length = int(self.headers.get("Content-Length", 0))
        form = parse_qs(self.rfile.read(length).decode())
        get = lambda k: (form.get(k) or [""])[0]  # noqa: E731

        if get("grant_type") != "client_credentials":
            return self._json(400, {
                "error": "unsupported_grant_type",
                "error_description": "only client_credentials is supported",
            })
        if get("client_id") != EXPECTED_CLIENT_ID or get("client_secret") != EXPECTED_SECRET:
            return self._json(401, {
                "error": "invalid_client",
                "error_description": "AADSTS7000215: Invalid client secret provided.",
            })
        # The real endpoint rejects a single-slash scope with this exact
        # error; reproducing it here means the trap is caught in the demo.
        scope = get("scope")
        if scope and "flow.microsoft.com" in scope and "//.default" not in scope:
            return self._json(400, {
                "error": "invalid_scope",
                "error_description": (
                    "MisMatchingOAuthClaims: the flow scope requires a double "
                    "slash before .default"
                ),
            })

        issued += 1
        self._json(200, {
            "token_type": "Bearer",
            "expires_in": TOKEN_TTL,
            "access_token": f"mock.{uuid.uuid4().hex}.{int(time.time())}",
        })

    def log_message(self, fmt, *args):
        print(f"mock-entra: {fmt % args}", flush=True)


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8081"))
    print(f"mock-entra listening on :{port}", flush=True)
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()
