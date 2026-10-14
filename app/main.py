"""Web server: chat page + API. Run: python -m app.main  (or: uvicorn app.main:app)"""

import os
import re
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

load_dotenv()

from app import db  # noqa: E402  (after load_dotenv: DB_PATH comes from .env)
from app import auth, backup, daily, export, extraction, payments, reports, spreadsheet  # noqa: E402
from app.agent import Agent  # noqa: E402
from app.llm import LLMError  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "static"
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR") or ROOT / "uploads")
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_PDF_PAGES = 5
MIGRATED_NOTE = "رصيد منقول من الدفتر"
APP_VERSION = "18"  # bump when a change must not be served by an older copy still running

# Accepted uploads, recognised by their first bytes (not by name or browser header).
FILE_KINDS = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp", "pdf": "application/pdf",
              "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "csv": "text/csv"}
SHEET_KINDS = {"xlsx", "csv"}
FILE_ID_RE = re.compile(r"^[0-9a-f]{32}$")

BACKUP_CHECK_SECONDS = 3600


def _daily_backups(stop: threading.Event) -> None:
    """Today's copy of the notebook when the program starts, then checked every hour
    (so a program left running for days still makes one each day)."""
    while True:
        try:
            with agent.lock:
                made = backup.ensure_daily(agent.conn)
            if made:
                print(f"نسخة احتياطية: {made}")
        except Exception as e:  # a full disk or a missing USB stick must not stop the shop
            print(f"ما گدرت أسوي نسخة احتياطية: {e}")
        if stop.wait(BACKUP_CHECK_SECONDS):
            return


REPORT_CHECK_SECONDS = 300


def _daily_report(stop: threading.Event) -> None:
    """Stage 22: the day's report at the owner's hour, by itself (Telegram if set, and in the program)."""
    while True:
        try:
            with agent.lock:
                made = daily.ensure_today(agent.conn)
            if made:
                print("انكتب تقرير اليوم" + (" وانبعث بالتلگرام" if daily.telegram_ready() else ""))
        except Exception as e:
            print(f"ما گدرت أسوي تقرير اليوم: {e}")
        if stop.wait(REPORT_CHECK_SECONDS):
            return


@asynccontextmanager
async def lifespan(_app):
    if os.getenv("REQUIRE_PASSWORD") and not auth.enabled():
        # online (the Docker image sets REQUIRE_PASSWORD): never serve the notebook without a password
        raise RuntimeError("APP_PASSWORD is not set: the notebook would be open to anyone on the internet.")
    stop = threading.Event()
    threading.Thread(target=_daily_backups, args=(stop,), daemon=True).start()
    threading.Thread(target=_daily_report, args=(stop,), daemon=True).start()
    yield
    stop.set()


app = FastAPI(title="دفتر الديون", lifespan=lifespan)
app.middleware("http")(auth.middleware)
agent = Agent(db.connect())


@app.get("/login")
def login_form():
    return auth.login_page() if auth.enabled() else RedirectResponse("/", status_code=303)


@app.post("/login")
async def login(request: Request):
    if auth.too_many(request):
        return auth.login_page("محاولات هواية. انتظر 10 دقايق.")
    form = (await request.body()).decode("utf-8", "replace")
    from urllib.parse import parse_qs
    given = (parse_qs(form).get("password") or [""])[0]
    import hmac
    if not auth.enabled() or not hmac.compare_digest(given.encode(), auth.password().encode()):
        auth.failed(request)
        return auth.login_page("كلمة السر غلط.")
    resp = RedirectResponse("/", status_code=303)
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    resp.set_cookie(auth.COOKIE, auth.make_cookie(), max_age=auth.DAYS * 86400, httponly=True, samesite="lax", secure=secure)
    return resp


class ChatRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=2000)
    source: Literal["chat", "voice"] = "chat"  # "voice": dictated with the mic, then read and sent


@app.get("/")
def index():
    # no-cache: after an update the browser must not keep showing the old page
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/version")
def version():
    return {"app": "debt-agent", "version": APP_VERSION}


@app.post("/chat")
def chat(req: ChatRequest, request: Request):
    agent.session(req.session_id).tools.base_url = str(request.base_url)
    return agent.chat(req.session_id, req.message, req.source)


def _ledger(kind: str) -> dict:
    """Everyone in this ledger with a non-zero balance, biggest first. For customers a
    positive balance is owed to the shop; for suppliers it is what the shop owes."""
    with agent.lock:
        rows = db.all_balances(agent.conn, kind)
    rows = [r for r in rows if any(r["balances"].values())]
    rows.sort(key=lambda r: (r["balances"]["IQD"], r["balances"]["USD"]), reverse=True)
    totals = {cur: sum(max(r["balances"][cur], 0) for r in rows) for cur in db.CURRENCIES}
    return {"debtors": rows, "totals": totals}


@app.get("/debtors")
def debtors():
    return _ledger("customer")


@app.get("/suppliers")
def suppliers():
    return _ledger("supplier")


@app.get("/sessions")
def sessions():
    """Saved conversations, newest first (stage 13)."""
    with agent.lock:
        return {"sessions": db.list_sessions(agent.conn)}


@app.get("/sessions/{session_id}")
def session_messages(session_id: str):
    with agent.lock:
        messages = db.get_messages(agent.conn, session_id)
    return {"id": session_id, "messages": messages}


@app.get("/memories")
def memories():
    with agent.lock:
        return {"memories": db.list_memories(agent.conn)}


@app.delete("/memories/{memory_id}")
def forget_memory(memory_id: int):
    with agent.lock:
        gone = db.forget_memory(agent.conn, memory_id)
    if gone is None:
        return _error(404, "ما لگيت هالشي بالذاكرة.")
    return {"ok": True, "forgot": gone}


@app.get("/withdrawals")
def withdrawals():
    """This month by reason (same shape as the ledgers, for the same table) + today's total."""
    with agent.lock:
        month = db.withdrawals_summary(agent.conn, "month")
        today = db.withdrawals_summary(agent.conn, "today")
    rows = [{"id": r["reason"], "name": r["reason"], "balances": r["totals"], "last_activity": r["last"]}
            for r in month["by_reason"]]
    return {"debtors": rows, "totals": month["totals"], "today": today["totals"], "latest": month["latest"]}


@app.get("/drawer")
def drawer():
    """Closing counts with each day's cash sales (stage 15), newest first, and what moved
    since the last count."""
    with agent.lock:
        return {"closes": db.drawer_closes(agent.conn, 30), "since_last_count": db.drawer_report(agent.conn),
                "now": db.drawer_details(agent.conn)}


@app.get("/drawer/details")
def drawer_details(count_id: int | None = None):
    """One closing count line by line (in / out / left), or, without count_id, since the last count."""
    with agent.lock:
        count = db.get_drawer_count(agent.conn, count_id) if count_id is not None else None
        if count_id is not None and (count is None or count["undone"] or count["kind"] != "close"):
            return _error(404, "ما لگيت هالعدّة.")
        return db.drawer_details(agent.conn, count)


# ---------- stage 17: items and the sale screen ----------

class ItemRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    price: int = Field(gt=0, le=1_000_000_000)
    cost: int | None = Field(default=None, ge=0, le=1_000_000_000)
    unit: str | None = Field(default=None, max_length=30)
    currency: Literal["IQD", "USD"] = "IQD"


class ItemUpdate(BaseModel):
    price: int | None = Field(default=None, gt=0, le=1_000_000_000)
    cost: int | None = Field(default=None, ge=0, le=1_000_000_000)
    unit: str | None = Field(default=None, max_length=30)
    active: bool | None = None


class SaleLine(BaseModel):
    item_id: int
    qty: float = Field(default=1, gt=0, le=100000)
    unit_price: int | None = Field(default=None, gt=0, le=1_000_000_000)


class SaleRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=100)
    payment: Literal["cash", "debt", "electronic"]
    customer_id: int | None = None
    lines: list[SaleLine] = Field(min_length=1, max_length=100)
    confirmed: bool = False


PAYMENT_WORDS = {"cash": "كاش", "debt": "دين", "electronic": "إلكتروني"}


@app.get("/items")
def items():
    with agent.lock:
        return {"items": db.all_items(agent.conn)}


@app.post("/items")
def add_item(req: ItemRequest):
    name = " ".join(req.name.split())
    with agent.lock:
        if any(m["normalized_name"] == db.normalize(name) for m in db.find_items(agent.conn, name)):
            return _error(409, "أكو مادة بنفس الاسم.")
        item_id = db.add_item(agent.conn, name, req.price, req.cost, req.unit, req.currency)
        return {"ok": True, "item": db.get_item(agent.conn, item_id)}


@app.patch("/items/{item_id}")
def update_item(item_id: int, req: ItemUpdate):
    with agent.lock:
        if not db.get_item(agent.conn, item_id):
            return _error(404, "ما لگيت المادة.")
        db.update_item(agent.conn, item_id, price=req.price, cost=req.cost, unit=req.unit,
                       active=None if req.active is None else int(req.active))
        return {"ok": True, "item": db.get_item(agent.conn, item_id)}


@app.get("/customers")
def customers():
    """All customers (for picking who a debt sale goes to)."""
    with agent.lock:
        return {"customers": [{"id": c["id"], "name": c["name"]} for c in db.all_customers(agent.conn)]}


@app.post("/sales")
def sale(req: SaleRequest):
    """A sale from the sale screen: same checks as the chat (record_sale), recorded in this
    conversation so "شطب آخر وحدة" can undo it."""
    with agent.lock:
        tools = agent.session(req.session_id).tools
        tools.source, tools.source_message = "screen", None
        result = tools.execute("record_sale", {"lines": [l.model_dump() for l in req.lines], "payment": req.payment,
                                               "customer_id": req.customer_id, "confirmed": req.confirmed})
    if not result.get("ok"):
        status = 409 if result.get("error") == "confirmation_required" else 422
        return JSONResponse({**result, "message": result.get("message")}, status_code=status)
    reply = f"سجلت بيعة {result['what']} بـ {_money(result['total'], result['currency'])} {PAYMENT_WORDS[req.payment]}"
    if req.payment == "debt":
        reply += f" على {result['customer_name']}، صار عليه {_money(result['balance'][result['currency']], result['currency'])}"
    reply += "."
    agent.record_event(req.session_id, f"(بعت من شاشة البيع: {result['what']}، {PAYMENT_WORDS[req.payment]})",
                       f"{reply} (sale_id: {result['sale_id']})", shown=reply)
    return {**result, "reply": reply}


@app.get("/sales")
def sales_today():
    with agent.lock:
        return {"recent": db.recent_sales(agent.conn, 8), "today": db.sales_summary(agent.conn, "today")}


# ---------- stage 18: reports ----------

@app.get("/reports")
def reports_view(period: str = "month"):
    period = period if period in reports.PERIODS else "month"
    with agent.lock:
        forecast = reports.cash_forecast(agent.conn, 30)
        return {"profit": reports.profit(agent.conn, period), "week": reports.week_summary(agent.conn),
                "forecast": forecast}


def _statement_html(st: dict) -> str:
    from html import escape

    def money(v, cur="IQD"):
        return f"{v:,}" + (" $" if cur == "USD" else "")
    word = {"debt": "دين", "payment": "تسديد"}
    rows = "".join(
        f"<tr><td class=d>{escape(l['at'][:16])}</td><td>{word[l['type']]}</td><td>{escape(l['note'])}</td>"
        f"<td class=n>{money(l['amount'], l['currency']) if l['type'] == 'debt' else ''}</td>"
        f"<td class=n>{money(l['amount'], l['currency']) if l['type'] == 'payment' else ''}</td>"
        f"<td class=n>{money(l['balance'], l['currency'])}</td></tr>" for l in st["lines"]) \
        or "<tr><td colspan=6 style='text-align:center;color:#777'>ما كو قيود بهالفترة</td></tr>"
    from datetime import date, timedelta
    last_day = (date.fromisoformat(st["to"]) - timedelta(days=1)).isoformat()
    usd = st["opening"]["USD"] or st["closing"]["USD"]
    def both(d):
        return money(d["IQD"]) + (f" و {money(d['USD'], 'USD')}" if usd else "")
    return f"""<!doctype html><html lang="ar" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>كشف حساب {escape(st['name'])}</title>
<style>body{{font-family:system-ui,'Segoe UI',Tahoma,sans-serif;margin:24px;color:#111;background:#fff}}
h1{{font-size:20px;margin:0 0 4px}} .sub{{color:#555;font-size:13px;margin-bottom:16px}}
table{{width:100%;border-collapse:collapse;font-size:14px}} th,td{{border-bottom:1px solid #ddd;padding:8px 6px;text-align:right}}
th{{background:#f5f5f5;font-size:12px}} .n{{font-variant-numeric:tabular-nums;white-space:nowrap}}
td.d{{white-space:nowrap;font-size:12px;color:#444;direction:ltr;text-align:right}}
.sum{{display:flex;gap:12px;flex-wrap:wrap;margin:12px 0}} .sum div{{border:1px solid #ddd;border-radius:10px;padding:8px 12px}}
.sum b{{display:block;font-size:18px}} button{{margin-top:16px;padding:10px 18px;font-size:15px;cursor:pointer}}
@media print{{button{{display:none}} body{{margin:0}}}}</style></head><body>
<h1>كشف حساب: {escape(st['name'])}</h1>
<div class="sub">{escape(st['period_words'])} · من {st['from']} لـ {last_day} · {st['based_on']['entries']} قيد</div>
<div class="sum"><div>الرصيد قبل الفترة<b class=n>{both(st['opening'])}</b></div>
<div>ديون بالفترة<b class=n>{both(st['debts'])}</b></div><div>تسديدات بالفترة<b class=n>{both(st['payments'])}</b></div>
<div>الرصيد هسه<b class=n>{both(st['closing'])}</b></div></div>
<table><thead><tr><th>التاريخ</th><th>النوع</th><th>التفاصيل</th><th>دين</th><th>تسديد</th><th>الرصيد بعده</th></tr></thead>
<tbody>{rows}</tbody></table>
<button onclick="print()">🖨 اطبع</button></body></html>"""


@app.get("/statement/{customer_id}")
def statement(customer_id: int, period: str = "month"):
    """A printable page with one customer's account only (to show or send to that customer)."""
    period = period if period in reports.PERIODS else "month"
    with agent.lock:
        customer = db.get_customer(agent.conn, customer_id)
        if not customer or customer.get("kind", "customer") != "customer":
            return _error(404, "ما لگيت هالزبون.")
        st = reports.customer_statement(agent.conn, customer_id, period)
    return HTMLResponse(_statement_html(st), headers={"Cache-Control": "no-store"})


# ---------- stage 19: reminders and alerts ----------

class RecurringRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=60)
    amount: int = Field(gt=0, le=1_000_000_000)
    currency: Literal["IQD", "USD"] = "IQD"
    day_of_month: int = Field(ge=1, le=31)
    from_drawer: bool = True


class RecurringAction(BaseModel):
    session_id: str = Field(min_length=1, max_length=100)
    skip: bool = False


class SettingsRequest(BaseModel):
    drawer_hour: int | None = Field(default=None, ge=0, le=23)
    overdue_days: int | None = Field(default=None, ge=1, le=365)
    report_hour: int | None = Field(default=None, ge=0, le=23)
    shop_name: str | None = Field(default=None, min_length=1, max_length=60)


def _settings() -> dict:
    return {k: int(db.get_setting(agent.conn, k)) for k in db.DEFAULT_SETTINGS}


def _extra_settings() -> dict:
    return {"report_hour": int(db.get_setting(agent.conn, "report_hour")),
            "shop_name": db.get_setting(agent.conn, "shop_name")}


@app.get("/alerts")
def alerts():
    with agent.lock:
        return db.alerts(agent.conn)


@app.get("/recurring")
def recurring():
    with agent.lock:
        return {"recurring": db.list_recurring(agent.conn), "settings": _settings(), "extra": _extra_settings()}


@app.post("/recurring")
def add_recurring(req: RecurringRequest):
    with agent.lock:
        rid = db.add_recurring(agent.conn, " ".join(req.reason.split()), req.amount, req.currency, req.day_of_month,
                               req.from_drawer)
        return {"ok": True, "recurring": db.get_recurring(agent.conn, rid)}


@app.delete("/recurring/{rec_id}")
def remove_recurring(rec_id: int):
    with agent.lock:
        if not db.get_recurring(agent.conn, rec_id):
            return _error(404, "ما لگيت هالصرفية.")
        db.remove_recurring(agent.conn, rec_id)
    return {"ok": True}


@app.post("/recurring/{rec_id}/record")
def record_recurring(rec_id: int, req: RecurringAction):
    """The owner said yes (or "not this month") to a fixed expense from the alerts."""
    with agent.lock:
        tools = agent.session(req.session_id).tools
        tools.source, tools.source_message = "screen", None
        result = tools.execute("record_recurring_expense", {"recurring_id": rec_id, "skip": req.skip})
    if not result.get("ok"):
        return JSONResponse(result, status_code=422)
    if result.get("skipped"):
        return {**result, "reply": f"تمام، {result['reason']} ما تنسجل هالشهر."}
    reply = f"سجلت سحب {_money(result['amount'], result['currency'])} ({result['reason']})، صرفية ثابتة."
    agent.record_event(req.session_id, f"(وافقت على الصرفية الثابتة: {result['reason']})", reply)
    return {**result, "reply": reply}


@app.post("/settings")
def settings(req: SettingsRequest):
    with agent.lock:
        if req.drawer_hour is not None:
            db.set_setting(agent.conn, "drawer_hour", req.drawer_hour)
        if req.overdue_days is not None:
            db.set_setting(agent.conn, "overdue_days", req.overdue_days)
        if req.report_hour is not None:
            db.set_setting(agent.conn, "report_hour", req.report_hour)
        if req.shop_name:
            db.set_setting(agent.conn, "shop_name", req.shop_name.strip())
        return {"ok": True, "settings": _settings(), "extra": _extra_settings()}


# ---------- stage 22: Zain Cash links, reminders, the daily report ----------

class LinkRequest(BaseModel):
    customer_id: int
    amount: int | None = Field(default=None, gt=0, le=1_000_000_000)
    note: str | None = Field(default=None, max_length=200)


class PhoneRequest(BaseModel):
    phone: str = Field(min_length=1, max_length=30)


class SentRequest(BaseModel):
    customer_id: int
    text: str = Field(min_length=1, max_length=2000)
    link_id: int | None = None
    channel: Literal["whatsapp", "copy"] = "whatsapp"


class SeenRequest(BaseModel):
    ids: list[int]


def _owner_tools(request: Request):
    tools = agent.session("_owner_screens").tools
    tools.base_url = str(request.base_url)
    return tools


def _tool_response(result: dict):
    if not result.get("ok"):
        return JSONResponse(result, status_code=422)
    return result


@app.post("/payment-links")
def payment_link(req: LinkRequest, request: Request):
    with agent.lock:
        return _tool_response(_owner_tools(request).execute(
            "create_payment_link", {"customer_id": req.customer_id, "amount": req.amount}))


@app.post("/reminders/draft")
def draft_reminder(req: LinkRequest, request: Request):
    """The button under an overdue customer: the reminder written, ready to send with one press."""
    with agent.lock:
        return _tool_response(_owner_tools(request).execute(
            "draft_reminder", {"customer_id": req.customer_id, "note": req.note}))


@app.post("/reminders/sent")
def reminder_sent(req: SentRequest):
    """The owner pressed send: kept, so the next reminder says when the last one went."""
    with agent.lock:
        if not db.get_customer(agent.conn, req.customer_id):
            return _error(404, "ما لگيت هالزبون.")
        rid = db.add_reminder(agent.conn, req.customer_id, req.text, req.link_id, req.channel)
    return {"ok": True, "id": rid}


@app.post("/customers/{customer_id}/phone")
def customer_phone(customer_id: int, req: PhoneRequest):
    with agent.lock:
        return _tool_response(agent.session("_owner_screens").tools.execute(
            "set_customer_phone", {"customer_id": customer_id, "phone": req.phone}))


@app.post("/alerts/seen-payments")
def seen_payments(req: SeenRequest):
    with agent.lock:
        db.mark_announced(agent.conn, req.ids)
    return {"ok": True}


@app.get("/daily-reports")
def daily_reports():
    with agent.lock:
        return {"reports": daily.recent(agent.conn), "telegram": daily.telegram_ready(),
                "report_hour": int(db.get_setting(agent.conn, "report_hour"))}


@app.post("/daily-reports/now")
def daily_report_now():
    """Make today's report now (and send it), without waiting for its hour."""
    with agent.lock:
        text = daily.build(agent.conn)
        sent = daily.send_telegram(text)
        daily.save(agent.conn, datetime.now().date().isoformat(), text, "telegram" if sent else None)
    return {"ok": True, "text": text, "sent": sent, "telegram": daily.telegram_ready()}


def _notify_paid(name: str, amount: int) -> None:
    """Tell the owner on Telegram straight away (if set); the program shows it in the alerts anyway."""
    text = f"💳 {name} دفع {amount:,} دينار بزين كاش، وانسجل تسديد بحسابه وحده."
    threading.Thread(target=daily.send_telegram, args=(text,), daemon=True).start()


def _pay_html(link: dict, customer: dict, shop: str, lines: list[dict], message: str = "") -> str:
    from html import escape
    simulated = link["provider"] == "simulated"
    balance = db.get_balance(agent.conn, customer["id"])["IQD"]
    banner = ("<div class=test>🧪 وضع تجربة: هاي محاكاة لزين كاش. ما تتحرك فلوس حقيقية، بس التسديد ينسجل "
              "بالدفتر بالضبط مثل الحقيقي.</div>" if simulated else "")
    if link["status"] == "paid":
        body = f"<div class=ok>✓ انسجل التسديد: {link['amount']:,} دينار<br><small>{escape(link['paid_at'] or '')}</small></div>"
    elif link["status"] == "cancelled":
        body = "<div class=bad>هذا الرابط قديم وانلغى. اطلب رابط جديد من المحل.</div>"
    elif balance <= 0:
        body = "<div class=ok>حسابك صافي، ما عليك شي هسه 🌷</div>"
    elif simulated:
        body = (f"<form method=post action='/pay/{link['token']}/confirm'><button>ادفع {link['amount']:,} دينار بزين كاش "
                f"(تجربة)</button></form>")
    else:
        body = f"<a class=btn href='/pay/{link['token']}/go'>ادفع {link['amount']:,} دينار بزين كاش</a>"
    word = {"debt": "دين", "payment": "تسديد"}
    # date, kind and amount only: the owner's own notes stay in the shop
    rows = "".join(f"<tr><td class=d>{escape(l['at'][:10])}</td><td>{word[l['type']]}</td>"
                   f"<td class=n>{l['amount']:,}</td></tr>" for l in lines if l["currency"] == "IQD")
    table = (f"<details><summary>آخر الحركات بحسابك</summary><table>{rows}</table></details>" if rows else "")
    note = f"<div class=msg>{escape(message)}</div>" if message else ""
    return f"""<!doctype html><html lang="ar" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>تسديد حساب: {escape(shop)}</title>
<style>body{{font-family:system-ui,'Segoe UI',Tahoma,sans-serif;margin:0;background:#f4f4f6;color:#111}}
.card{{max-width:440px;margin:24px auto;background:#fff;border-radius:16px;padding:20px;box-shadow:0 2px 12px #0001}}
h1{{font-size:18px;margin:0 0 4px}} .sub{{color:#666;font-size:14px}} .amt{{font-size:30px;font-weight:700;margin:16px 0}}
.test{{background:#fff4d6;border:1px solid #f0c040;border-radius:10px;padding:10px;font-size:13px;margin-bottom:14px}}
button,.btn{{display:block;width:100%;box-sizing:border-box;text-align:center;padding:14px;font-size:17px;border:0;border-radius:12px;
background:#e4007c;color:#fff;cursor:pointer;text-decoration:none;font-family:inherit}}
.ok{{background:#e7f7ec;border-radius:10px;padding:14px;text-align:center;font-size:17px}}
.bad{{background:#fde8e8;border-radius:10px;padding:14px;text-align:center}} .msg{{margin-top:10px;color:#a00;font-size:14px}}
details{{margin-top:16px;font-size:13px}} table{{width:100%;border-collapse:collapse;margin-top:8px}}
td{{border-bottom:1px solid #eee;padding:6px 4px}} .n{{white-space:nowrap;text-align:left}} .d{{white-space:nowrap;color:#555}}
</style></head><body><div class=card>{banner}
<h1>{escape(shop)}</h1><div class=sub>حساب: {escape(customer['name'])}</div>
<div class=amt>{link['amount']:,} دينار</div>{body}{note}{table}</div></body></html>"""


def _pay_page(token: str, message: str = "", status: int = 200):
    link = db.get_payment_link(agent.conn, token=token)
    if not link:
        return HTMLResponse("<p dir=rtl style='font-family:sans-serif;padding:24px'>الرابط غلط أو انتهى.</p>", status_code=404)
    customer = db.get_customer(agent.conn, link["customer_id"])
    lines = reports.customer_statement(agent.conn, customer["id"], "all")["lines"][-8:]
    return HTMLResponse(_pay_html(link, customer, db.get_setting(agent.conn, "shop_name"), lines, message),
                        status_code=status, headers={"Cache-Control": "no-store"})


@app.get("/pay/{token}")
def pay_page(token: str):
    """Public: what the customer opens from the reminder. It shows their own account only."""
    with agent.lock:
        return _pay_page(token)


@app.post("/pay/{token}/confirm")
def pay_confirm(token: str):
    """The simulated checkout: pressing pay records the payment by itself."""
    with agent.lock:
        link = db.get_payment_link(agent.conn, token=token)
        if not link:
            return _pay_page(token)
        if link["provider"] != "simulated":
            return _pay_page(token, "الدفع يصير من صفحة زين كاش.", 400)
        if link["status"] == "pending" and db.get_balance(agent.conn, link["customer_id"])["IQD"] > 0:
            db.pay_link(agent.conn, link, source="zaincash_test")
            _notify_paid(db.get_customer(agent.conn, link["customer_id"])["name"], link["amount"])
        return _pay_page(token)


@app.get("/pay/{token}/go")
def pay_go(token: str, request: Request):
    """Real Zain Cash: off to their checkout; they send the customer back to /callback."""
    with agent.lock:
        link = db.get_payment_link(agent.conn, token=token)
        if not link or link["status"] != "pending" or link["provider"] != "zaincash":
            return _pay_page(token)
    back = f"{payments.base_url(str(request.base_url))}/pay/{token}/callback"
    try:
        return RedirectResponse(payments.zaincash_checkout(link, back), status_code=303)
    except Exception as e:
        print(f"زين كاش: {e}")
        with agent.lock:
            return _pay_page(token, "زين كاش ما ردّت هسه. جرب بعد شوية.", 502)


@app.get("/pay/{token}/callback")
def pay_callback(token: str, request: Request):
    result = payments.zaincash_result(request.query_params.get("token", ""))
    with agent.lock:
        link = db.get_payment_link(agent.conn, token=token)
        if not link or not result or result.get("orderid") != token:
            return _pay_page(token, "ما گدرت أتأكد من الدفع. إذا انسحبت الفلوس راجع المحل.", 400)
        if result.get("status") == "success" and link["status"] == "pending":
            db.pay_link(agent.conn, link, source="zaincash")
            _notify_paid(db.get_customer(agent.conn, link["customer_id"])["name"], link["amount"])
            return _pay_page(token)
        return _pay_page(token, "الدفع ما تم. تگدر تعيد.")


# ---------- backups and Excel export ----------

@app.get("/backups")
def backups():
    return {"folder": str(backup.backup_dir()), "backups": backup.list_backups()}


@app.post("/backups")
def backup_now():
    try:
        with agent.lock:
            path = backup.make_backup(agent.conn)
    except OSError as e:
        return _error(500, f"ما گدرت أسوي النسخة: {e}")
    return {"ok": True, "name": path.name, "folder": str(path.parent)}


@app.get("/backups/{name}")
def download_backup(name: str):
    path = backup.backup_path(name)
    if not path:
        return _error(404, "ما لگيت هالنسخة.")
    return FileResponse(path, media_type="application/octet-stream", filename=name)


@app.get("/export.xlsx")
def export_excel():
    from datetime import date

    with agent.lock:
        data = export.build_workbook(agent.conn)
    name = f"debt-{date.today():%Y-%m-%d}.xlsx"
    return Response(data, media_type=FILE_KINDS["xlsx"],
                    headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


# ---------- stage 6: photos of the notebook ----------

def _error(status: int, message: str, **extra):
    return JSONResponse({"ok": False, "message": message, **extra}, status_code=status)


def _sniff(head: bytes) -> str | None:
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"PK\x03\x04"):
        return "zip"  # maybe xlsx: checked in _is_xlsx
    return None


def _is_xlsx(data: bytes) -> bool:
    import io
    import zipfile

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            return "xl/workbook.xml" in z.namelist()
    except zipfile.BadZipFile:
        return False


def _pdf_pages(data: bytes) -> int:
    # Good enough for a limit check without a PDF library: count page objects.
    return len(re.findall(rb"/Type\s*/Page(?![a-zA-Z])", data))


def _uploaded_file(file_id: str) -> tuple[Path, str] | None:
    if not FILE_ID_RE.match(file_id or ""):
        return None
    for ext, mime in FILE_KINDS.items():
        path = UPLOAD_DIR / f"{file_id}.{ext}"
        if path.is_file():
            return path, mime
    return None


@app.post("/upload")
async def upload(request: Request):
    """Raw file in the body (the browser already shrank photos). Saved under uploads/."""
    data = bytearray()
    async for chunk in request.stream():
        data += chunk
        if len(data) > MAX_UPLOAD_BYTES:
            return _error(413, "الملف أكبر من 10 ميگا.")
    kind = _sniff(bytes(data[:16]))
    if kind == "zip":
        kind = "xlsx" if _is_xlsx(bytes(data)) else None
    elif kind is None:
        # CSV has no signature: trust the .csv name only for text without NUL bytes.
        from urllib.parse import unquote
        name = unquote(request.headers.get("x-file-name", "")).lower()
        if name.endswith(".csv") and data and b"\x00" not in data[:4096]:
            kind = "csv"
    if kind is None:
        return _error(415, "نوع الملف مو مدعوم. دز صورة (jpg أو png أو webp) أو pdf، أو ملف Excel (xlsx) أو CSV.")
    if kind == "pdf" and _pdf_pages(bytes(data)) > MAX_PDF_PAGES:
        return _error(413, f"الـ PDF بيه أكثر من {MAX_PDF_PAGES} صفحات.")
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    file_id = uuid.uuid4().hex
    (UPLOAD_DIR / f"{file_id}.{kind}").write_bytes(bytes(data))
    return {"ok": True, "file_id": file_id, "kind": kind, "size": len(data)}


class ExtractRequest(BaseModel):
    file_id: str = Field(min_length=1, max_length=64)
    note: str | None = Field(default=None, max_length=500)


@app.post("/extract")
def extract(req: ExtractRequest):
    """Read the page into a review table. Writes nothing to the database."""
    found = _uploaded_file(req.file_id)
    if not found:
        return _error(404, "ما لگيت الملف، ارفعه من جديد.")
    path, mime = found
    if path.suffix[1:] in SHEET_KINDS:
        return _error(400, "هذا ملف جدول، مو صورة.")
    try:
        result = extraction.extract_file(agent.get_llm(), path.read_bytes(), mime, req.note)
        with agent.lock:
            review = extraction.build_review(agent.conn, result)
    except extraction.ExtractionNotReady as e:
        return _error(409, e.message)
    except NotImplementedError:
        return _error(409, "ميزة الصور بعدها مو جاهزة: لازم تنكتب مطابقة الأسماء "
                           "(match_extracted_rows بملف app/extraction.py).")
    except LLMError as e:
        return _error(502, e.user_message)
    return {"ok": True, "file_id": req.file_id, **review}


# ---------- stage 7: Excel / CSV ----------

class FileRequest(BaseModel):
    file_id: str = Field(min_length=1, max_length=64)


def _sheet(file_id: str):
    found = _uploaded_file(file_id)
    if not found or found[0].suffix[1:] not in SHEET_KINDS:
        return None
    path, _ = found
    return spreadsheet.read_table(path.read_bytes(), path.suffix[1:])


@app.post("/import/file")
def import_file(req: FileRequest):
    """Columns + first rows + which column is which. Known names map directly;
    otherwise the model suggests (from names + 3 rows) and the owner must confirm."""
    try:
        table = _sheet(req.file_id)
    except spreadsheet.SpreadsheetError as e:
        return _error(422, e.message)
    if table is None:
        return _error(404, "ما لگيت الملف، ارفعه من جديد.")
    headers, rows = table
    mapping = spreadsheet.guess_mapping(headers)
    source = "known"
    if not spreadsheet.is_complete(mapping):
        source = "model"
        try:
            mapping = spreadsheet.suggest_mapping(agent.get_llm(), headers, rows)
        except LLMError:
            source = "none"  # the owner picks the columns by hand
    return {"ok": True, "file_id": req.file_id, "headers": headers,
            "sample": spreadsheet.sample(headers, rows), "total_rows": len(rows),
            "mapping": mapping, "mapping_source": source,
            "needs_confirmation": source != "known"}


class FileRowsRequest(BaseModel):
    file_id: str = Field(min_length=1, max_length=64)
    mapping: dict[str, str | None]


@app.post("/import/file/rows")
def import_file_rows(req: FileRowsRequest):
    """The confirmed mapping -> the review table (same as photos). Writes nothing."""
    try:
        table = _sheet(req.file_id)
    except spreadsheet.SpreadsheetError as e:
        return _error(422, e.message)
    if table is None:
        return _error(404, "ما لگيت الملف، ارفعه من جديد.")
    headers, rows = table
    mapping = {role: req.mapping.get(role) or None for role in spreadsheet.ROLES}
    if any(h is not None and h not in headers for h in mapping.values()):
        return _error(422, "عمود مو موجود بالملف.")
    used = [h for h in mapping.values() if h]
    if len(used) != len(set(used)):
        return _error(422, "نفس العمود مختار لأكثر من شي.")
    if not spreadsheet.is_complete(mapping):
        return _error(422, "لازم تختار عمود الاسم وعمود المبلغ.")
    result = spreadsheet.rows_from_table(headers, rows, mapping)
    try:
        with agent.lock:
            review = extraction.build_review(agent.conn, result)
    except NotImplementedError:
        return _error(409, "لازم تنكتب مطابقة الأسماء (match_extracted_rows) أول.")
    return {"ok": True, "file_id": req.file_id, "source_kind": "file", **review}


class CommitRow(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    customer_id: int | None = None
    amount: int | str | None = None
    currency: str | None = None
    type: str | None = None


class CommitRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=100)
    file_id: str | None = Field(default=None, max_length=64)
    rows: list[CommitRow] = Field(max_length=extraction.MAX_ROWS)
    migrated: bool = False  # "these are balances carried over from the old notebook"
    source_kind: Literal["image", "file"] = "image"


def _money(amount: int, currency: str) -> str:
    return f"{amount:,} " + ("دولار" if currency == "USD" else "دينار")


@app.post("/import/commit")
def import_commit(req: CommitRequest):
    """Save the rows the owner reviewed and ticked, in one transaction."""
    if req.file_id and not FILE_ID_RE.match(req.file_id):
        return _error(400, "رقم الملف غلط.")
    with agent.lock:
        rows, errors = extraction.prepare_commit(agent.conn, [r.model_dump() for r in req.rows])
        if errors:
            return _error(422, "صلّح هالصفوف وعيد:", errors=errors)
        batch_id = uuid.uuid4().hex[:12]
        source = f"{req.source_kind}:{req.file_id}" if req.file_id else req.source_kind
        db.import_batch(agent.conn, rows, batch_id, source, MIGRATED_NOTE if req.migrated else None)

    totals: dict[tuple[str, str], int] = {}
    for r in rows:
        totals[(r["type"], r["currency"])] = totals.get((r["type"], r["currency"]), 0) + r["amount"]
    parts = []
    for (tx_type, cur), amount in sorted(totals.items()):
        parts.append(("ديون " if tx_type == "debt" else "تسديدات ") + _money(amount, cur))
    reply = (f"سجلت {len(rows)} قيد: {'، '.join(parts)}. "
             "إذا شي غلط گلي 'ألغِ آخر دفعة'.")
    what = "رفعت ملف Excel/CSV" if req.source_kind == "file" else "رفعت صورة من الدفتر"
    agent.record_event(req.session_id, f"({what} وراجعت الجدول وسجلته)",
                       f"{reply} (batch_id: {batch_id})", shown=reply)
    return {"ok": True, "batch_id": batch_id, "count": len(rows), "reply": reply}


def port_in_use(port: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def running_version(url: str) -> str | None:
    """Version of the debt agent answering on url, or None (older copy / something else)."""
    import json
    import urllib.request

    try:
        with urllib.request.urlopen(url + "/version", timeout=2) as r:
            data = json.loads(r.read())
        return data.get("version") if data.get("app") == "debt-agent" else None
    except Exception:
        return None


def main() -> None:
    import threading
    import webbrowser

    import uvicorn

    port = int(os.getenv("PORT") or 8000)
    # HOST=0.0.0.0 opens it to the phone on the shop's Wi-Fi (or online); only with a password
    host = os.getenv("HOST") or "127.0.0.1"
    if host != "127.0.0.1" and not auth.enabled():
        print("HOST يفتح البرنامج لغير هالحاسبة: لازم تحط APP_PASSWORD بملف .env أول.")
        raise SystemExit(1)
    url = f"http://127.0.0.1:{port}"
    if port_in_use(port):
        if running_version(url) == APP_VERSION:
            # Same version already running (e.g. start.bat clicked twice): just show it.
            print("البرنامج شغال من قبل بنافذة ثانية، فتحتلك المتصفح عليه.")
            if not os.getenv("NO_BROWSER"):
                webbrowser.open(url)
            return
        # Otherwise the browser would open an older copy that is still running.
        print(f"المنفذ {port} مشغول: أكو نسخة قديمة من البرنامج بعدها شغالة.")
        print("سدها أول، أو اكتب بـ PowerShell:  taskkill /F /IM python.exe /IM py.exe")
        print("وبعدين شغّل start.bat من جديد.")
        raise SystemExit(1)
    print(f"دفتر الديون شغال على: {url}  (للإيقاف: Ctrl+C)")
    if not os.getenv("NO_BROWSER"):
        threading.Timer(1.5, webbrowser.open, [url]).start()
    if host != "127.0.0.1":
        print(f"ومن الموبايل على نفس الواي فاي: http://<IP مال هالحاسبة>:{port}  (ويطلب كلمة السر)")
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
