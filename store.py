"""Shared key-value storage for deployments (Upstash Redis REST API, standard library only).

On a server with a persistent disk the app keeps its AI answers and saved analyses in
files under index/. Serverless hosts (Vercel) have no persistent disk, so when Redis
credentials are present the same data goes to Redis instead.

Environment (any of these pairs; the Vercel Upstash integration sets them):
    KV_REST_API_URL / KV_REST_API_TOKEN
    UPSTASH_REDIS_REST_URL / UPSTASH_REDIS_REST_TOKEN
    LCA_REDIS_PREFIX   key prefix (default "lca:")
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
import zlib

TIMEOUT_S = 10


def _config():
    url = os.environ.get("KV_REST_API_URL") or os.environ.get("UPSTASH_REDIS_REST_URL")
    token = os.environ.get("KV_REST_API_TOKEN") or os.environ.get("UPSTASH_REDIS_REST_TOKEN")
    return (url.rstrip("/"), token) if url and token else (None, None)


def enabled() -> bool:
    return _config()[0] is not None


def prefix() -> str:
    return os.environ.get("LCA_REDIS_PREFIX", "lca:")


def command(*args):
    """Run one Redis command, e.g. command("GET", "k"). Returns the result or raises."""
    url, token = _config()
    if not url:
        raise RuntimeError("Redis is not configured")
    req = urllib.request.Request(url, data=json.dumps([str(a) for a in args]).encode("utf-8"), method="POST",
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    if "error" in body:
        raise RuntimeError(f"Redis error: {body['error']}")
    return body.get("result")


def _pack(value) -> str:
    """JSON, compressed: saved analyses can be a few hundred KB of repetitive JSON."""
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return "z:" + base64.b64encode(zlib.compress(raw, 6)).decode("ascii")


def _unpack(text: str):
    if text.startswith("z:"):
        return json.loads(zlib.decompress(base64.b64decode(text[2:])).decode("utf-8"))
    return json.loads(text)


def get_json(key: str):
    """Value for key, or None if missing or Redis is unreachable."""
    try:
        text = command("GET", prefix() + key)
    except (OSError, RuntimeError, ValueError, urllib.error.URLError):
        return None
    if text is None:
        return None
    try:
        return _unpack(text)
    except (ValueError, zlib.error):
        return None


def set_json(key: str, value, ttl_s: int | None = None) -> bool:
    try:
        args = ["SET", prefix() + key, _pack(value)]
        if ttl_s:
            args += ["EX", ttl_s]
        command(*args)
        return True
    except (OSError, RuntimeError, ValueError, urllib.error.URLError):
        return False


def incr_with_ttl(key: str, ttl_s: int) -> int | None:
    """Increment a counter that expires ttl_s after its first increment (rate limits)."""
    try:
        n = int(command("INCR", prefix() + key))
        if n == 1:
            command("EXPIRE", prefix() + key, ttl_s)
        return n
    except (OSError, RuntimeError, ValueError, TypeError, urllib.error.URLError):
        return None
