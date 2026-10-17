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
# Bumped by "خروج من كل الأجهزة": every cookie signed before it stops working. Kept in the
# notebook (settings.login_epoch); main loads it at start.
EPOCH = "0"
# A password set from inside the program (☰ ← 🔒): "salt$hash" (PBKDF2), kept in the notebook's
# settings; main loads it at start. APP_PASSWORD in .env, when there, wins over it.
STORED = ""
ITERATIONS = 200_000
MIN_LENGTH = 6


def password() -> str:
    return os.getenv("APP_PASSWORD") or ""


def from_env() -> bool:
    return bool(password())


def enabled() -> bool:
    return from_env() or bool(STORED)


def hash_password(pw: str, salt: str | None = None) -> str:
    salt = salt or os.urandom(16).hex()
    digest = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), ITERATIONS).hex()
    return f"{salt}${digest}"


def check_password(given: str) -> bool:
    if from_env():
        return hmac.compare_digest(given.encode(), password().encode())
    if STORED and "$" in STORED:
        salt = STORED.split("$", 1)[0]
        return hmac.compare_digest(hash_password(given, salt), STORED)
    return False


def _key() -> bytes:
    secret = password() or STORED  # a new password = a new key: everyone else is logged out
    return hashlib.sha256(f"debt-agent:{secret}:{os.getenv('SECRET_KEY', '')}:{EPOCH}".encode()).digest()


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
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+Arabic:wght@400;600;700&display=swap" rel="stylesheet">
<style>*{{box-sizing:border-box}} body{{font-family:"IBM Plex Sans Arabic",system-ui,'Segoe UI',Tahoma,sans-serif;color:#f5f5f4;margin:0;
display:flex;min-height:100vh;align-items:center;justify-content:center;
background:radial-gradient(900px 500px at 100% -10%,rgba(245,158,11,.14),transparent 60%),#0a0a0a}}
form{{background:#141414;border:1px solid rgba(255,255,255,.08);border-radius:24px;padding:28px 24px;width:min(360px,90vw);
display:flex;flex-direction:column;gap:14px;box-shadow:0 30px 60px -25px rgba(0,0,0,.8)}}
.logo{{width:52px;height:52px;border-radius:16px;background:linear-gradient(135deg,#f59e0b,#ea580c);display:grid;place-items:center;
color:#fff;font-size:24px;font-weight:700;box-shadow:0 12px 26px -10px rgba(234,88,12,.6)}}
h1{{font-size:21px;margin:4px 0 0}} .sub{{color:#a1a1aa;font-size:13px;margin:-6px 0 4px}}
input,button{{font:inherit;padding:13px 14px;border-radius:14px;border:1px solid rgba(255,255,255,.12)}}
input{{background:rgba(255,255,255,.05);color:inherit;outline:none}} input:focus{{border-color:rgba(245,158,11,.55);box-shadow:0 0 0 4px rgba(245,158,11,.18)}}
button{{background:linear-gradient(135deg,#f59e0b,#ea580c);color:#fff;font-weight:700;border:0;cursor:pointer;
box-shadow:0 12px 24px -12px rgba(234,88,12,.6)}}
.bad{{color:#fb7185;margin:0;background:rgba(244,63,94,.1);padding:8px 12px;border-radius:10px}}</style></head><body>
<form method="post" action="/login"><div class=logo>م</div><h1>مساعد التاجر</h1><p class=sub>اكتب كلمة السر حتى تدخل</p>{note}
<input type="password" name="password" placeholder="كلمة السر" autofocus autocomplete="current-password">
<button>ادخل</button></form></body></html>""", headers={"Cache-Control": "no-store"})
