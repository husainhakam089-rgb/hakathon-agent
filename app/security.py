"""Security checks on every request (found by the security review, 15-10-2026).

1. Host: without a password the program trusts whoever reaches it, which must only be this
   computer. A web page can point its own name at 127.0.0.1 (DNS rebinding) and then read the
   notebook as if it were the shop owner: the request arrives with that page's name as Host.
   So without a password only 127.0.0.1 / localhost (and ALLOWED_HOSTS) are served. With a
   password, the login cookie protects instead (a rebound page never has it).
2. Origin: a page on another site can send a plain form POST to the program (no preflight).
   Any request that changes something and says it comes from another site is refused.
3. Headers: the program must not be shown inside another site's frame (a hidden button press,
   "clickjacking"), files are served as what they are, and no address leaks to other sites.
"""

import os
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import JSONResponse

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}
HEADERS = {
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
}


def _host_only(host: str) -> str:
    host = (host or "").strip().lower()
    if host.startswith("["):  # [::1]:8000
        return host[1:host.find("]")] if "]" in host else host
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def allowed_host(host: str, password_set: bool) -> bool:
    if password_set:
        return True
    extra = {h.strip().lower() for h in os.getenv("ALLOWED_HOSTS", "").split(",") if h.strip()}
    return _host_only(host) in LOCAL_HOSTS | extra


def same_origin(origin: str | None, host: str) -> bool:
    """No Origin (not a browser, or an old one) is fine; "null" or another site is not."""
    if origin is None:
        return True
    parts = urlsplit(origin)
    return bool(parts.netloc) and parts.netloc.lower() == (host or "").lower()


def make_middleware(password_set):
    async def middleware(request: Request, call_next):
        host = request.headers.get("host", "")
        if not allowed_host(host, password_set()):
            return JSONResponse({"error": "bad_host", "message": "هذا العنوان مو مسموح."}, status_code=400)
        if request.method in UNSAFE and not same_origin(request.headers.get("origin"), host):
            return JSONResponse({"error": "cross_site", "message": "طلب من موقع ثاني، انرفض."}, status_code=403)
        response = await call_next(request)
        for k, v in HEADERS.items():
            response.headers.setdefault(k, v)
        return response
    return middleware
