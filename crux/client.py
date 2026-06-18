"""Thin HTTP client for team mode.

When `CRUX_SERVER` is configured, the hook and CLI talk to a shared CRUX server
over HTTP instead of the local SQLite DB — so a whole team pulls from (and writes
to) one intent graph. Stdlib only (urllib); no extra dependencies on the client.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request


def _post(server: str, path: str, body: dict, timeout: float = 8.0) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(server.rstrip("/") + path, data=data,
                                 headers={"content-type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode() or "{}")


def retrieve(server: str, prompt: str, *, session: str = "", user: str = "",
             limit: int = 5) -> dict:
    """Ask the shared server for the directive brief for a prompt; the server
    retrieves, formats, and records usage (tagged with user/session)."""
    return _post(server, "/retrieve",
                 {"prompt": prompt, "session": session, "user": user, "limit": limit})


def capture(server: str, content: str, *, type: str | None = None,
            source: str = "agent", user: str = "", proposed: bool = True) -> dict:
    """Capture into the shared graph. proposed=True → Review; False → private WM."""
    return _post(server, "/capture",
                 {"content": content, "type": type, "source": source,
                  "user": user, "proposed": proposed})


def ingest(server: str, content: str, *, source_ref: str = "clipboard",
           user: str = "") -> dict:
    return _post(server, "/ingest",
                 {"content": content, "source_ref": source_ref,
                  "source_type": "paste", "user": user})


def ping(server: str) -> bool:
    try:
        with urllib.request.urlopen(server.rstrip("/") + "/stats", timeout=5) as r:
            return r.status == 200
    except Exception:
        return False
