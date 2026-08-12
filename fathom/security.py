"""Request-level hardening: CSRF origin check, headers, AI rate limits."""
from urllib.parse import urlparse

from fastapi import Request
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
