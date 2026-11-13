"""Small state responses for a page that already holds the previous state.

Only field hashes live here. A missing checkpoint (including after a restart)
returns a complete state, so the page always catches up from authoritative data.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
from collections import OrderedDict
from typing import Any

from fastapi.responses import Response


def _encode(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()


def _object(fields: dict[str, bytes]) -> bytes:
    return b"{" + b",".join(_encode(key) + b":" + value for key, value in fields.items()) + b"}"


class StateVersions:
    def __init__(self) -> None:
        self._versions: OrderedDict[tuple[str, str], dict[str, bytes]] = OrderedDict()
        self._lock = threading.Lock()

    def response(self, state: dict[str, Any], since: str | None, scope: str) -> Response:
        with self._lock:
            previous = self._versions.get((scope, since)) if since else None
        # Hash each value once; retain its bytes only when the client needs them.
        fields = {}
        fingerprints = {}
        for key, value in state.items():
            encoded = _encode(value)
            fingerprint = hashlib.sha256(encoded).digest()
            fingerprints[key] = fingerprint
            if previous is None or fingerprint != previous.get(key):
                fields[key] = encoded
        cursor = secrets.token_urlsafe(18)
        with self._lock:
            self._versions[scope, cursor] = fingerprints
            while len(self._versions) > 64:
                self._versions.popitem(last=False)
        headers = {"X-Cockpit-State": cursor}
        if previous is None:
            return Response(_object(fields), media_type="application/json", headers=headers)
        assert since is not None
        headers["X-Cockpit-Base"] = since
        content = b'{"changes":' + _object(fields) + b',"removed":' + _encode(list(previous.keys() - fingerprints.keys())) + b"}"
        return Response(content, media_type="application/json", headers=headers)
