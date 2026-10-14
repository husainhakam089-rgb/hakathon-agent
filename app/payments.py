"""Stage 22: pay by Zain Cash from a link, and the reminder that carries it.

The customer gets a link to a page on this program (/pay/<token>). What happens there depends
on the provider:

- simulated (the default): a page marked clearly "وضع تجربة". Pressing pay records the payment in
  the notebook by itself, exactly as a real one would. No money moves.
- zaincash: set ZAINCASH_MERCHANT_ID, ZAINCASH_SECRET and ZAINCASH_MSISDN (and ZAINCASH_ENV=live
  for real money; test by default). The page sends the customer to Zain Cash's own checkout
  (their merchant API: transaction/init, then transaction/pay), and when Zain Cash sends them
  back with a signed result, a "success" records the payment. Written from their public merchant
  API; it has not been run against their servers here (that needs a merchant account).

Either way the payment is electronic: it goes to the customer's account and NOT into the drawer.
"""

import base64
import hashlib
import hmac
import json
import os
import re
import time
from urllib.parse import quote

import httpx

MIN_AMOUNT = 250  # Zain Cash refuses less than 250 dinars

ZAINCASH_HOSTS = {"test": "https://test.zaincash.iq", "live": "https://api.zaincash.iq"}


def provider_name() -> str:
    keys = ("ZAINCASH_MERCHANT_ID", "ZAINCASH_SECRET", "ZAINCASH_MSISDN")
    return "zaincash" if all(os.getenv(k) for k in keys) else "simulated"


def base_url(fallback: str = "") -> str:
    """Where customers reach this program. BASE_URL when it is online; otherwise the address
    the owner opened it on (fine to try, but a customer cannot open 127.0.0.1)."""
    return (os.getenv("BASE_URL") or fallback or "http://127.0.0.1:8000").rstrip("/")


def pay_url(token: str, base: str) -> str:
    return f"{base}/pay/{token}"


# ---------- phones and WhatsApp ----------

def normalize_phone(phone: str | None) -> str | None:
    """An Iraqi mobile as 07XXXXXXXXX, or None when it is not one."""
    if not phone:
        return None
    digits = re.sub(r"\D", "", phone.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")))
    if digits.startswith("00964"):
        digits = digits[5:]
    elif digits.startswith("964"):
        digits = digits[3:]
    if digits.startswith("7") and len(digits) == 10:
        digits = "0" + digits
    return digits if re.fullmatch(r"07[3-9]\d{8}", digits) else None


def whatsapp_url(phone: str | None, text: str) -> str:
    """One press opens WhatsApp with the message written; the owner presses send. Without a
    number WhatsApp asks which chat."""
    p = normalize_phone(phone)
    to = ("964" + p[1:]) if p else ""
    return f"https://wa.me/{to}?text={quote(text)}"


def _money(n: int) -> str:
    return f"{n:,}"


def reminder_text(name: str, amount: int, url: str, shop: str, note: str | None = None) -> str:
    """The reminder, written by code so the amount and the link are exactly right. The agent
    may add a short line of the owner's own (note)."""
    lines = [f"هلا {name}، تحية طيبة من {shop}.",
             f"حسابك عدنا {_money(amount)} دينار.",
             f"تگدر تسدده من هنا بزين كاش بدون ما تجي: {url}"]
    if note and note.strip():
        lines.append(note.strip()[:200])
    lines.append("وإذا سددت گبل، اعتبر الرسالة ما وصلت. شكرًا إلك 🌷")
    return "\n".join(lines)


# ---------- Zain Cash merchant API (only used when its keys are set) ----------

def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def jwt_encode(payload: dict, secret: str) -> str:
    head = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(secret.encode(), f"{head}.{body}".encode(), hashlib.sha256).digest()
    return f"{head}.{body}.{_b64(sig)}"


def jwt_decode(token: str, secret: str) -> dict | None:
    """The payload when the signature is right, else None (a forged "success" is refused)."""
    try:
        head, body, sig = token.split(".")
        good = hmac.new(secret.encode(), f"{head}.{body}".encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(good, _unb64(sig)):
            return None
        return json.loads(_unb64(body))
    except (ValueError, json.JSONDecodeError):
        return None


def _host() -> str:
    return ZAINCASH_HOSTS["live" if os.getenv("ZAINCASH_ENV") == "live" else "test"]


def zaincash_checkout(link: dict, redirect_url: str, client: httpx.Client | None = None) -> str:
    """Start a Zain Cash transaction for this link and return their checkout page."""
    now = int(time.time())
    token = jwt_encode({"amount": link["amount"], "serviceType": f"تسديد حساب #{link['id']}",
                        "msisdn": int(os.environ["ZAINCASH_MSISDN"]), "orderId": link["token"],
                        "redirectUrl": redirect_url, "iat": now, "exp": now + 4 * 3600},
                       os.environ["ZAINCASH_SECRET"])
    http = client or httpx.Client(timeout=20)
    r = http.post(f"{_host()}/transaction/init",
                  data={"token": token, "merchantId": os.environ["ZAINCASH_MERCHANT_ID"], "lang": "ar"})
    tx_id = r.json().get("id")
    if not tx_id:
        raise RuntimeError(f"Zain Cash refused the transaction: {r.text[:300]}")
    return f"{_host()}/transaction/pay?id={tx_id}"


def zaincash_result(token: str) -> dict | None:
    """What Zain Cash sent back: {'status': 'success'|'failed'|..., 'orderid': ...}, or None if forged."""
    return jwt_decode(token, os.environ.get("ZAINCASH_SECRET", ""))
