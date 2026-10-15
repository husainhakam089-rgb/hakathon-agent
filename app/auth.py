"""Stage 22: a password for the program once it is online (APP_PASSWORD).

Without APP_PASSWORD nothing changes (the program on the shop's own computer). With it, every
page and API needs the login cookie, except what a customer or a health check opens:
the payment pages (/pay/...), /login and /version. Wrong passwords are limited per address AND
for everyone together, so faking addresses (X-Forwarded-For) does not buy more tries.
The cookie is signed with the password (and SECRET_KEY if set), so changing the password logs
everyone out.
"""

import hashlib
import hmac
import os
import time
from html import escape

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

COOKIE = "dafter_login"
DAYS = 30
OPEN_PREFIXES = ("/pay/", "/login", "/version")
MAX_TRIES = 5          # wrong passwords per address in TRY_WINDOW
MAX_TRIES_ALL = 20     # wrong passwords from everyone together in TRY_WINDOW: the backstop
TRY_WINDOW = 600  # seconds
ALL = "*all*"
WRONG_DELAY = 1.0  # seconds a wrong password waits

_tries: dict[str, list[float]] = {}


def password() -> str:
    return os.getenv("APP_PASSWORD") or ""


def enabled() -> bool:
    return bool(password())


def _key() -> bytes:
    return hashlib.sha256(f"debt-agent:{password()}:{os.getenv('SECRET_KEY', '')}".encode()).digest()


def make_cookie(now: float | None = None) -> str:
    expires = int((time.time() if now is None else now) + DAYS * 86400)
    sig = hmac.new(_key(), str(expires).encode(), hashlib.sha256).hexdigest()
    return f"{expires}.{sig}"


def valid_cookie(value: str | None, now: float | None = None) -> bool:
    try:
        expires, sig = (value or "").split(".")
        good = hmac.new(_key(), expires.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(good, sig) and int(expires) > (time.time() if now is None else now)
    except ValueError:
        return False


def _client(request: Request) -> str:
    """Who is trying. X-Forwarded-For is written by whoever sends the request, so it is only
    read behind a proxy we run (TRUST_PROXY=1, set in the Dockerfile), and then only its LAST
    entry, the one the proxy itself added. Anything the sender put before it is ignored."""
    if os.getenv("TRUST_PROXY"):
        fwd = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
        if fwd:
            return fwd[-1]
    return request.client.host if request.client else "?"


def _recent(key: str, now: float) -> list[float]:
    _tries[key] = [t for t in _tries.get(key, []) if now - t < TRY_WINDOW]
    return _tries[key]


def too_many(request: Request, now: float | None = None) -> bool:
    """Five wrong tries from one address, or twenty from everyone together, close the login for
    10 minutes. The owner is not locked out of what he already opened: his cookie lasts 30 days."""
    now = time.time() if now is None else now
    return len(_recent(_client(request), now)) >= MAX_TRIES or len(_recent(ALL, now)) >= MAX_TRIES_ALL


def failed(request: Request) -> None:
    now = time.time()
    _tries.setdefault(_client(request), []).append(now)
    _tries.setdefault(ALL, []).append(now)


def is_open(path: str) -> bool:
    return any(path == p.rstrip("/") or path.startswith(p) for p in OPEN_PREFIXES)


async def middleware(request: Request, call_next):
    if not enabled() or is_open(request.url.path) or valid_cookie(request.cookies.get(COOKIE)):
        return await call_next(request)
    if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
        return RedirectResponse("/login", status_code=303)
    return JSONResponse({"error": "login_required", "message": "لازم تدخل كلمة السر."}, status_code=401)


def login_page(message: str = "") -> HTMLResponse:
    note = f"<p class=bad>{escape(message)}</p>" if message else ""
    return HTMLResponse(f"""<!doctype html><html lang="ar" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>دخول: مساعد التاجر</title>
<style>body{{font-family:system-ui,'Segoe UI',Tahoma,sans-serif;background:#0f172a;color:#e2e8f0;margin:0;
display:flex;min-height:100vh;align-items:center;justify-content:center}}
form{{background:#1e293b;border-radius:16px;padding:24px;width:min(340px,90vw);display:flex;flex-direction:column;gap:12px}}
h1{{font-size:20px;margin:0}} input,button{{font:inherit;padding:12px;border-radius:10px;border:1px solid #334155}}
input{{background:#0f172a;color:inherit}} button{{background:#38bdf8;color:#0f172a;font-weight:700;border:0;cursor:pointer}}
.bad{{color:#f87171;margin:0}}</style></head><body>
<form method="post" action="/login"><h1>🧾 مساعد التاجر</h1>{note}
<input type="password" name="password" placeholder="كلمة السر" autofocus autocomplete="current-password">
<button>ادخل</button></form></body></html>""", headers={"Cache-Control": "no-store"})
