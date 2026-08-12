"""Request-level hardening: CSRF origin check, headers, AI rate limits."""
import logging.handlers
import os
import time
from collections import defaultdict
from urllib.parse import urlparse

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse


_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def request_host(request: Request) -> str:
    return (request.headers.get("host") or "").lower()


def url_host(value: str) -> str:
    parsed = urlparse(value)
    return (parsed.netloc or "").lower()


def origin_allowed(request: Request) -> bool:
    """Allow same-host Origin/Referer; allow missing headers (curl / TestClient)."""
    if request.method in _SAFE_METHODS:
        return True
    host = request_host(request)
    origin = request.headers.get("origin")
    referer = request.headers.get("referer")
    if origin:
        if origin.strip().lower() == "null":
            return False
        return bool(host) and url_host(origin) == host
    if referer:
        return bool(host) and url_host(referer) == host
    return True


async def csrf_origin_middleware(request: Request, call_next):
    if not origin_allowed(request):
        return JSONResponse({"detail": "Cross-origin request blocked"}, status_code=403)
    return await call_next(request)


_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' https: data:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)


async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Content-Security-Policy"] = _CSP
    return response


# Hits per client IP in the last 60s. 0 disables (used by the test suite).
_AI_RATE_WINDOW_SEC = 60.0
_ai_hits: dict[str, list[float]] = defaultdict(list)


def reset_ai_rate_limit():
    _ai_hits.clear()


def require_ai_budget(request: Request):
    """Raise 429 if this client has exceeded FATHOM_AI_RATE_LIMIT per minute."""
    try:
        limit = int(os.environ.get("FATHOM_AI_RATE_LIMIT", "30"))
    except ValueError:
        limit = 30
    if limit <= 0:
        return
    ip = request.client.host if request.client else "unknown"
    now = time.monotonic()
    bucket = _ai_hits[ip]
    cutoff = now - _AI_RATE_WINDOW_SEC
    bucket[:] = [t for t in bucket if t > cutoff]
    if len(bucket) >= limit:
        raise HTTPException(status_code=429, detail="Too many AI requests, try again in a minute")
    bucket.append(now)


class PrivateRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """Rotating file handler that keeps the log file owner-readable only."""

    def _open(self):
        stream = super()._open()
        try:
            os.chmod(self.baseFilename, 0o600)
        except OSError:
            pass
        return stream


def clamp_limit(limit: int, *, lo: int = 1, hi: int = 500) -> int:
    """Keep user-supplied SQL LIMIT values in a sane positive range."""
    try:
        n = int(limit)
    except (TypeError, ValueError):
        return lo
    return min(max(n, lo), hi)


def tighten_env_file_mode(path: str | os.PathLike):
    """chmod 600 an existing .env so the API key is not world-readable."""
    try:
        if os.path.isfile(path):
            os.chmod(path, 0o600)
    except OSError:
        pass
