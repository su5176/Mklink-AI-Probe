"""Shared static file serving for MKLink visualization servers.

Serves files from the mklink/static/ directory with correct MIME types.
Prevents path traversal. Adds cache headers for browser caching.
"""

from __future__ import annotations

import mimetypes
import os

_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

_MIME_OVERRIDES = {
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
}


def serve_static(handler, filename: str) -> bool:
    """Serve a file from mklink/static/. Returns True if served, False if not found."""
    filename = filename.split("?", 1)[0]
    safe = os.path.basename(filename)
    path = os.path.join(_STATIC_DIR, safe)
    if not os.path.isfile(path):
        return False
    ext = os.path.splitext(safe)[1].lower()
    mime = _MIME_OVERRIDES.get(ext) or mimetypes.guess_type(safe)[0] or "application/octet-stream"
    with open(path, "rb") as f:
        data = f.read()
    handler.send_response(200)
    handler.send_header("Content-Type", mime)
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "no-cache")
    handler.end_headers()
    handler.wfile.write(data)
    return True
