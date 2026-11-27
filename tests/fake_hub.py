"""A stand-in for the CLIProxyAPI hub's management API on a local port: the routes Cockpit reads and the ones its
controls change (SPEC UI-81, UI-85). A test sets what the hub holds on the object and reads back what Cockpit sent."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

V8 = "/v8/management"
V0 = "/v0/management"


def error_log(method: str, path: str, status: int) -> str:
    """One request the hub answered with an error, in the form of the hub's own log file of it."""
    return (
        "=== REQUEST INFO ===\nVersion: 8.0.53\n"
        f"URL: {path}\nMethod: {method}\nDownstream Transport: http\nTimestamp: 2026-11-09T11:58:00Z\n\n"
        "=== HEADERS ===\nAuthorization: Bearer caller-key-in-the-log\nUser-Agent: a-client\n\n"
        "=== REQUEST BODY ===\nwhat the caller asked, which stays in the log\n\n"
        f"=== RESPONSE ===\nStatus: {status}\nContent-Type: text/plain\n\n404 page not found\n"
    )


class FakeHub:
    """`credentials` are the accounts, `error_logs` the failed requests as {file name: (modified, text)}, `gone` the
    paths it answers 404. `asked` collects the paths of the reads and `changes` every change Cockpit sent, as
    (method, path, body). `relay_answers` are the providers' answers to a request relayed through an account, as
    {auth index: (status, body)}, a provider's 404 for an account without one; `relayed` collects the relayed
    requests as Cockpit sent them."""

    def __init__(self, key: str) -> None:
        self.credentials: list[dict[str, Any]] = []
        self.routing = "round-robin"
        self.client_keys = ["client-key-1", "client-key-2"]
        self.version = "8.0.53"
        self.latest = "v8.0.63"
        self.error_logs: dict[str, tuple[float, str]] = {}
        self.changes: list[tuple[str, str, Any]] = []
        self.asked: list[str] = []
        self.gone: set[str] = set()
        self.relay_answers: dict[str, tuple[int, Any]] = {}
        self.relayed: list[dict[str, Any]] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def send(self, status: int, data: bytes, kind: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("X-Cpa-Version", fake.version)
                self.end_headers()
                self.wfile.write(data)

            def answer(self, status: int, body: Any) -> None:
                self.send(status, json.dumps(body).encode(), "application/json")

            def refused(self) -> bool:
                if self.headers.get("Authorization") == f"Bearer {key}":
                    return False
                self.answer(401, {"error": "invalid management key"})
                return True

            def body(self) -> Any:
                return json.loads(self.rfile.read(int(self.headers["Content-Length"])))

            def do_GET(self) -> None:
                if self.refused():
                    return None
                path = urlsplit(self.path).path
                fake.asked.append(path)
                if path in fake.gone:
                    return self.answer(404, {"error": "not found"})
                if path == f"{V8}/credentials":
                    return self.answer(200, {"files": fake.credentials, "observed_at": "now"})
                if path == f"{V0}/routing/strategy":
                    return self.answer(200, {"strategy": fake.routing})
                if path == f"{V0}/api-keys":
                    return self.answer(200, {"api-keys": fake.client_keys})
                if path == f"{V0}/latest-version":
                    return self.answer(200, {"latest-version": fake.latest})
                if path == f"{V0}/request-error-logs":
                    return self.answer(200, {"files": [
                        {"name": name, "size": len(text), "modified": modified}
                        for name, (modified, text) in fake.error_logs.items()
                    ]})
                name = unquote(path.removeprefix(f"{V0}/request-error-logs/"))
                if name in fake.error_logs:
                    return self.send(200, fake.error_logs[name][1].encode(), "text/plain")
                return self.answer(404, {"error": "not found"})

            def do_POST(self) -> None:
                if self.refused():
                    return None
                path, body = urlsplit(self.path).path, self.body()
                if path == f"{V8}/routing/cooldown/reset":
                    fake.changes.append(("POST", path, body))
                    rested = {"unavailable": False, "quota_blocked": False, "next_retry_after": None}
                    fake.credentials = [each | rested if str(each["auth_index"]) == body["auth_index"] else each
                                        for each in fake.credentials]
                    return self.answer(200, {"status": "ok"})
                if path == f"{V8}/requests/api-call":
                    fake.relayed.append(body)
                    status, answer = fake.relay_answers.get(body["authIndex"], (404, {"error": "not found"}))
                    # The hub gives the provider's answer as text.
                    return self.answer(200, {"status_code": status, "header": {}, "body": json.dumps(answer)})
                return self.answer(404, {"error": "not found"})

            def do_PATCH(self) -> None:
                if self.refused():
                    return None
                path, body = urlsplit(self.path).path, self.body()
                if path != f"{V0}/auth-files/status" or body["name"] not in [each["id"] for each in fake.credentials]:
                    return self.answer(404, {"error": "not found"})
                fake.changes.append(("PATCH", path, body))
                fake.credentials = [each | {"disabled": body["disabled"]} if each["id"] == body["name"] else each
                                    for each in fake.credentials]
                return self.answer(200, {"status": "ok", "disabled": body["disabled"]})

            def do_PUT(self) -> None:
                if self.refused():
                    return None
                path, body = urlsplit(self.path).path, self.body()
                if path != f"{V0}/routing/strategy":
                    return self.answer(404, {"error": "not found"})
                fake.changes.append(("PUT", path, body))
                fake.routing = body["value"]
                return self.answer(200, {"status": "ok"})

            def do_DELETE(self) -> None:
                if self.refused():
                    return None
                parts = urlsplit(self.path)
                [name] = parse_qs(parts.query)["name"]
                if parts.path != f"{V0}/auth-files" or name not in [each["id"] for each in fake.credentials]:
                    return self.answer(404, {"error": "not found"})
                fake.changes.append(("DELETE", parts.path, name))
                fake.credentials = [each for each in fake.credentials if each["id"] != name]
                return self.answer(200, {"status": "ok"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
