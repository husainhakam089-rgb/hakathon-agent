"""Stage 22: the daily report, made and sent by itself (no model: every figure is computed).

At the hour the owner chose (report_hour, 23 by default) the program writes the day's report.
If TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set it is sent to the owner's Telegram; it is
always kept in the program (☰ ← التقارير) too. One report a day: a restart does not resend it.
"""

import os
from datetime import date, datetime, timedelta

import httpx

from app import db, reports


def _m(d: dict) -> str:
    parts = [f"{d.get('IQD', 0):,} دينار"] + ([f"{d['USD']:,} دولار"] if d.get("USD") else [])
    return " + ".join(parts)


def build(conn, today: date | None = None) -> str:
    today = today or date.today()
    s = reports.period_summary(conn, "today", today)
    p = s["profit"]
    shop = db.get_setting(conn, "shop_name")
    pay = s["sales_by_payment"]
    lines = [f"📊 تقرير {shop}: {today.isoformat()}", ""]
    if s["sales_total"].get("IQD") or s["sales_total"].get("USD"):
        lines.append(f"🛒 البيع: {_m(s['sales_total'])} (كاش {pay['cash']['IQD']:,}، دين {pay['debt']['IQD']:,}، "
                     f"إلكتروني {pay['electronic']['IQD']:,})")
        if not p["sales_without_cost"].get("IQD"):
            lines.append(f"💰 الربح الصافي: {p['net_profit']['IQD']:,} دينار")
    else:
        lines.append("🛒 ما كو بيع مسجل بالمواد اليوم")
    if s["counts"]["customer_payments"]:
        lines.append(f"✅ تسديدات: {_m(s['customer_payments'])} ({s['counts']['customer_payments']} تسديد)")
    if s["counts"]["new_customer_debts"]:
        lines.append(f"📝 ديون جديدة: {_m(s['new_customer_debts'])}")
    if any(p["expenses"].values()):
        lines.append(f"💸 الصرفيات: {_m(p['expenses'])}")
    zc = conn.execute("""SELECT COUNT(*) n, COALESCE(SUM(amount), 0) s FROM payment_links
                         WHERE status = 'paid' AND date(paid_at) = ?""", (today.isoformat(),)).fetchone()
    if zc["n"]:
        lines.append(f"💳 زين كاش: {zc['n']} دفعة من الروابط بـ {zc['s']:,} دينار (تسجّلت وحدها)")
    closes = [c for c in db.drawer_closes(conn, 5) if c["counted"]["at"][:10] == today.isoformat()]
    if closes:
        diff = closes[0].get("difference") or {}
        d = diff.get("IQD")
        lines.append("🧾 الجرار: انعد" + ("" if d is None else
                     (" ومضبوط ✓" if d == 0 else f"، الفرق {d:+,} دينار")))
    else:
        lines.append("🧾 الجرار: ما انعد اليوم")
    overdue = db.overdue_debts(conn, None, datetime.combine(today, datetime.max.time()))
    if overdue:
        top = "، ".join(f"{o['name']} ({o['balance']['IQD']:,})" for o in overdue[:3])
        lines.append(f"⏰ {len(overdue)} متأخرين بالدين: {top}. التذكير جاهز بضغطة من التنبيهات.")
    tomorrow = datetime.combine(today + timedelta(days=1), datetime.min.time())
    due = [r for r in db.recurring_due(conn, tomorrow)
           if (r["day_of_week"] == tomorrow.weekday() if r["period"] == "week" else r["day_of_month"] == tomorrow.day)]
    for r in due:
        lines.append(f"📅 باچر موعد {r['reason']}: {r['amount']:,} {'دولار' if r['currency'] == 'USD' else 'دينار'}")
    return "\n".join(lines)


def telegram_ready() -> bool:
    return bool(os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"))


def send_telegram(text: str, client: httpx.Client | None = None) -> bool:
    """True when Telegram took it. Never raises: a message that did not go is still in the program."""
    if not telegram_ready():
        return False
    try:
        http = client or httpx.Client(timeout=15)
        r = http.post(f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/sendMessage",
                      json={"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": text})
        return r.status_code == 200
    except Exception:
        return False


def save(conn, day: str, text: str, sent_via: str | None) -> None:
    conn.execute("""INSERT INTO daily_reports (day, text, sent_via) VALUES (?, ?, ?)
                    ON CONFLICT(day) DO UPDATE SET text = excluded.text, sent_via = excluded.sent_via,
                    created_at = datetime('now','localtime')""", (day, text, sent_via))
    conn.commit()


def ensure_today(conn, now: datetime | None = None, send=send_telegram) -> str | None:
    """Make (and send) today's report once its hour has come. Returns the text when it was made now."""
    now = now or datetime.now()
    if now.hour < int(db.get_setting(conn, "report_hour")):
        return None
    day = now.date().isoformat()
    if conn.execute("SELECT 1 FROM daily_reports WHERE day = ?", (day,)).fetchone():
        return None
    text = build(conn, now.date())
    save(conn, day, text, "telegram" if send(text) else None)
    return text


def notify_due_reminders(conn, now: datetime | None = None, send=send_telegram) -> int:
    """"ذكرني" reminders whose time came: to the owner's Telegram (once each), so they reach
    him even with the program's page closed. Returns how many went."""
    if not telegram_ready():
        return 0
    sent = 0
    for r in db.due_owner_reminders(conn, now):
        if not r["notified"] and send(f"⏰ تذكير: {r['text']}"):
            conn.execute("UPDATE owner_reminders SET notified = 1 WHERE id = ?", (r["id"],))
            sent += 1
    conn.commit()
    return sent


def recent(conn, limit: int = 14) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM daily_reports ORDER BY day DESC LIMIT ?", (limit,))]
