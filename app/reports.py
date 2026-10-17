"""Stage 18: the shop's accounts, all computed by code (the model only explains them).

- profit:      sales - cost of the goods sold - expenses (withdrawals), for a period
- week:        a summary of the last 7 days
- forecast:    the cash expected in the next N days, from the last weeks
- statement:   one customer's account for a period, line by line, with the running balance

Every report says what it is built on (how many entries), so a figure can be traced back.
Dates are the shop's local time, as stored ('YYYY-MM-DD HH:MM:SS').
"""

import calendar
from datetime import date, timedelta

from app import db

PERIODS = ("today", "yesterday", "week", "month", "last_month", "all")
PERIOD_WORDS = {"today": "اليوم", "yesterday": "البارحة", "week": "آخر 7 أيام", "month": "هالشهر",
                "last_month": "الشهر الماضي", "all": "من البداية"}


def period_range(period: str, today: date | None = None) -> tuple[str, str]:
    """[start, end) as 'YYYY-MM-DD' strings for a period name."""
    today = today or date.today()
    if period == "today":
        start, end = today, today + timedelta(days=1)
    elif period == "yesterday":
        start, end = today - timedelta(days=1), today
    elif period == "week":
        start, end = today - timedelta(days=6), today + timedelta(days=1)
    elif period == "last_month":
        first = today.replace(day=1)
        start, end = (first - timedelta(days=1)).replace(day=1), first
    elif period == "all":
        start, end = date(2000, 1, 1), today + timedelta(days=1)
    else:  # month
        start, end = today.replace(day=1), today + timedelta(days=1)
    return start.isoformat(), end.isoformat()


# Money the owner takes for himself is not a cost of the shop: it is left out of the expenses
# and shown apart ("سحبت لنفسك"), or the profit would look like a loss every day.
OWNER_DRAW_WORDS = ("بيت", "شخصي", "شخصيه", "لنفسي", "خاص")


def is_owner_draw(reason_key: str) -> bool:
    words = reason_key.replace("لل", "ال ").replace("ال", " ").split()
    return any(w in OWNER_DRAW_WORDS for w in words) or reason_key in ("للبيت", "لنفسي")


def _zero():
    return {c: 0 for c in db.CURRENCIES}


def profit(conn, period: str = "month", today: date | None = None) -> dict:
    """Net profit = (sales - their cost) - expenses (not what the owner took for himself). Sales without a known cost and cash
    written as "وارد" (no items) are counted as income but give no profit figure: they are
    listed apart so nothing is made up."""
    start, end = period_range(period, today)
    rng = (start, end)
    revenue, cost, unknown, sales_count = _zero(), _zero(), _zero(), 0
    for r in conn.execute(
            """SELECT s.currency, l.line_total, l.qty, l.unit_cost FROM sale_lines l JOIN sales s ON s.id = l.sale_id
               WHERE s.undone = 0 AND s.created_at >= ? AND s.created_at < ?""", rng):
        revenue[r["currency"]] += r["line_total"]
        if r["unit_cost"] is None:
            unknown[r["currency"]] += r["line_total"]
        else:
            cost[r["currency"]] += round(r["qty"] * r["unit_cost"])
    sales_count = conn.execute("SELECT COUNT(*) FROM sales WHERE undone = 0 AND created_at >= ? AND created_at < ?",
                               rng).fetchone()[0]
    expenses, by_reason, owner = _zero(), {}, _zero()
    wd_count = 0
    for r in conn.execute(
            """SELECT reason, reason_key, currency, SUM(amount) s, COUNT(*) n FROM withdrawals
               WHERE undone = 0 AND created_at >= ? AND created_at < ? GROUP BY reason_key, currency
               ORDER BY s DESC""", rng):
        if is_owner_draw(r["reason_key"]):
            owner[r["currency"]] += r["s"]
            continue
        expenses[r["currency"]] += r["s"]
        by_reason.setdefault(r["reason_key"], {"reason": r["reason"], "amounts": _zero()})["amounts"][r["currency"]] += r["s"]
        wd_count += r["n"]
    cash_ins, ci_count = _zero(), 0
    for r in conn.execute("""SELECT currency, SUM(amount) s, COUNT(*) n FROM cash_ins
                             WHERE undone = 0 AND created_at >= ? AND created_at < ? GROUP BY currency""", rng):
        cash_ins[r["currency"]] += r["s"]
        ci_count += r["n"]
    gross = {c: revenue[c] - unknown[c] - cost[c] for c in db.CURRENCIES}
    net = {c: gross[c] - expenses[c] for c in db.CURRENCIES}
    notes = []
    if any(unknown.values()):
        notes.append("بيع بمواد كلفتها مو مسجلة ما ينحسب ربحه")
    if any(cash_ins.values()):
        notes.append("الوارد بدون مواد (مثل وارد محمد) دخل، بس ما نعرف كلفته فما داخل بالربح")
    if any(owner.values()):
        notes.append("اللي سحبته لنفسك (البيت، شخصي) مو مصروف للمحل، فما انطرح من الربح")
    if not sales_count:
        notes.append("ما كو بيع مسجل بالمواد بهالفترة، فالربح ما ينحسب صح")
    return {"period": period, "period_words": PERIOD_WORDS.get(period, period), "from": start, "to": end,
            "sales": revenue, "cost_of_goods": cost, "sales_without_cost": unknown, "gross_profit": gross,
            "expenses": expenses, "expenses_by_reason": list(by_reason.values()), "net_profit": net,
            "owner_draws": owner,
            "cash_in_without_items": cash_ins,
            "based_on": {"sales": sales_count, "withdrawals": wd_count, "cash_ins": ci_count}, "notes": notes}


def week_summary(conn, today: date | None = None) -> dict:
    """The last 7 days at a glance."""
    return period_summary(conn, "week", today)


def period_summary(conn, period: str = "week", today: date | None = None) -> dict:
    """A period at a glance (the week summary, and the daily report for "today")."""
    start, end = period_range(period, today)
    rng = (start, end)
    p = profit(conn, period, today)
    pay = {k: _zero() for k in ("cash", "debt", "electronic")}
    for r in conn.execute("""SELECT payment, currency, SUM(total) s FROM sales
                             WHERE undone = 0 AND created_at >= ? AND created_at < ? GROUP BY payment, currency""", rng):
        pay[r["payment"]][r["currency"]] = r["s"]
    flows = {"customer_payments": _zero(), "new_customer_debts": _zero(), "supplier_purchases": _zero(),
             "supplier_payments": _zero()}
    counts = {k: 0 for k in flows}
    for r in conn.execute(
            """SELECT c.kind, t.type, t.currency, SUM(t.amount) s, COUNT(*) n FROM transactions t
               JOIN customers c ON c.id = t.customer_id
               WHERE t.undone = 0 AND t.batch_id IS NULL AND t.created_at >= ? AND t.created_at < ?
               GROUP BY c.kind, t.type, t.currency""", rng):
        key = {("customer", "payment"): "customer_payments", ("customer", "debt"): "new_customer_debts",
               ("supplier", "debt"): "supplier_purchases", ("supplier", "payment"): "supplier_payments"}[(r["kind"], r["type"])]
        flows[key][r["currency"]] += r["s"]
        counts[key] += r["n"]
    top = [{"name": r["name"], "qty": r["qty"], "revenue": r["revenue"]} for r in conn.execute(
        """SELECT i.name, SUM(l.qty) qty, SUM(l.line_total) revenue FROM sale_lines l
           JOIN sales s ON s.id = l.sale_id JOIN items i ON i.id = l.item_id
           WHERE s.undone = 0 AND s.created_at >= ? AND s.created_at < ?
           GROUP BY i.id ORDER BY revenue DESC LIMIT 5""", rng)]
    days = {}
    for r in conn.execute("""SELECT date(created_at) d, SUM(total) s FROM sales WHERE undone = 0 AND currency = 'IQD'
                             AND created_at >= ? AND created_at < ? GROUP BY d""", rng):
        days[r["d"]] = r["s"]
    best = max(days.items(), key=lambda kv: kv[1]) if days else None
    closes = [c for c in db.drawer_closes(conn, 100) if start <= c["counted"]["at"][:10] < end]
    short = [{"at": c["counted"]["at"], "difference": c["difference"]} for c in closes
             if any(v < 0 for v in c.get("difference", {}).values())]
    return {"from": start, "to": end, "sales_by_payment": pay, "sales_total": p["sales"], "profit": p,
            **flows, "counts": counts, "top_items": top,
            "best_day": {"date": best[0], "sales": best[1]} if best else None,
            "drawer_counts": len(closes), "drawer_short": short}


def cash_forecast(conn, days: int = 30, weeks: int = 4, today: date | None = None) -> dict:
    """Cash expected to come in and go out over the next `days`, from the daily average of the
    last `weeks` weeks (cash sales, cash in, customer payments vs. paid to suppliers and
    withdrawals from the drawer), plus the fixed monthly expenses that fall due. It knows
    nothing about seasons or one-off events: it says so."""
    today = today or date.today()
    start = (today - timedelta(days=7 * weeks)).isoformat()
    end = today.isoformat()  # full days only: today is not finished
    rng = (start, end)
    first = conn.execute(
        """SELECT MIN(d) FROM (SELECT MIN(created_at) d FROM sales WHERE undone = 0
                              UNION ALL SELECT MIN(created_at) FROM transactions WHERE undone = 0 AND batch_id IS NULL
                              UNION ALL SELECT MIN(created_at) FROM cash_ins WHERE undone = 0
                              UNION ALL SELECT MIN(created_at) FROM withdrawals WHERE undone = 0)""").fetchone()[0]
    if not first or first[:10] >= end:
        return {"ok": False, "reason": "ما كو أيام كاملة مسجلة بعد حتى أبني عليها توقع"}
    span = (today - max(date.fromisoformat(first[:10]), date.fromisoformat(start))).days
    if span < 7:
        return {"ok": False, "reason": f"بس {span} يوم مسجل، أحتاج أسبوع على الأقل حتى يطلع التوقع معقول",
                "days_of_data": span}
    one = lambda sql: {r[0]: r[1] for r in conn.execute(sql, rng)}  # noqa: E731
    cin = _zero()
    for part in (one("""SELECT currency, SUM(total) FROM sales WHERE undone = 0 AND payment = 'cash'
                        AND created_at >= ? AND created_at < ? GROUP BY currency"""),
                 one("""SELECT currency, SUM(amount) FROM cash_ins WHERE undone = 0
                        AND created_at >= ? AND created_at < ? GROUP BY currency"""),
                 one("""SELECT t.currency, SUM(t.amount) FROM transactions t JOIN customers c ON c.id = t.customer_id
                        WHERE t.undone = 0 AND t.batch_id IS NULL AND t.from_drawer = 1 AND c.kind = 'customer'
                        AND t.type = 'payment' AND t.created_at >= ? AND t.created_at < ? GROUP BY t.currency""")):
        for cur, v in part.items():
            cin[cur] += v or 0
    cout = _zero()
    for part in (one("""SELECT t.currency, SUM(t.amount) FROM transactions t JOIN customers c ON c.id = t.customer_id
                        WHERE t.undone = 0 AND t.batch_id IS NULL AND t.from_drawer = 1 AND c.kind = 'supplier'
                        AND t.type = 'payment' AND t.created_at >= ? AND t.created_at < ? GROUP BY t.currency"""),
                 ):
        for cur, v in part.items():
            cout[cur] += v or 0
    # withdrawals: fixed monthly ones are added below from their own list (not averaged), and
    # what the owner takes for himself is his income, not the shop's spending
    for r in conn.execute("""SELECT reason_key, currency, SUM(amount) s FROM withdrawals WHERE undone = 0
                             AND from_drawer = 1 AND COALESCE(note, '') != 'صرفية ثابتة'
                             AND created_at >= ? AND created_at < ? GROUP BY reason_key, currency""", rng):
        if not is_owner_draw(r["reason_key"]):
            cout[r["currency"]] += r["s"]
    # sold and not written down (a drawer count found more than expected) is income too
    for c in db.drawer_closes(conn, 200):
        if start <= c["counted"]["at"][:10] < end:
            for cur, diff in c.get("difference", {}).items():
                if diff > 0:
                    cin[cur] += diff
    per_day_in = {c: round(cin[c] / span) for c in db.CURRENCIES}
    per_day_out = {c: round(cout[c] / span) for c in db.CURRENCIES}
    horizon_end = today + timedelta(days=days)
    fixed, fixed_total = [], _zero()
    for r in db.list_recurring(conn):
        if r["period"] == "week":  # every week's day in the window, but not one already recorded
            d = today + timedelta(days=(r["day_of_week"] - today.weekday()) % 7)
            while d < horizon_end:
                if r.get("done_key") != d.isoformat():
                    fixed.append({"reason": r["reason"], "amount": r["amount"], "currency": r["currency"],
                                  "due": d.isoformat()})
                    fixed_total[r["currency"]] += r["amount"]
                d += timedelta(days=7)
            continue
        d = today
        while d < horizon_end:  # every due date in the window, at most one per month
            last = calendar.monthrange(d.year, d.month)[1]
            due = d.replace(day=min(r["day_of_month"], last))
            month_done = r["done_month"] == due.strftime("%Y-%m")
            if today <= due < horizon_end and not month_done:
                fixed.append({"reason": r["reason"], "amount": r["amount"], "currency": r["currency"],
                              "due": due.isoformat()})
                fixed_total[r["currency"]] += r["amount"]
            d = (d.replace(day=1) + timedelta(days=32)).replace(day=1)
    expected_in = {c: per_day_in[c] * days for c in db.CURRENCIES}
    expected_out = {c: per_day_out[c] * days + fixed_total[c] for c in db.CURRENCIES}
    return {"ok": True, "days": days, "based_on_days": span, "from": max(start, first[:10]), "to": end,
            "per_day_in": per_day_in, "per_day_out": per_day_out, "expected_in": expected_in,
            "expected_out": expected_out, "fixed_expenses": fixed,
            "expected_net": {c: expected_in[c] - expected_out[c] for c in db.CURRENCIES},
            "caveat": "مبني على معدل آخر الأسابيع. ما يعرف المواسم والأعياد والأشياء اللي تصير مرة وحدة."}


def customer_statement(conn, customer_id: int, period: str = "month", today: date | None = None) -> dict:
    """One customer's account (only theirs): the balance before the period, every entry in it
    with the running balance, and the balance after. Undone entries are left out."""
    customer = db.get_customer(conn, customer_id)
    start, end = period_range(period, today)
    opening = _zero()
    for r in conn.execute(
            """SELECT currency, SUM(CASE WHEN type = 'debt' THEN amount ELSE -amount END) b FROM transactions
               WHERE customer_id = ? AND undone = 0 AND created_at < ? GROUP BY currency""", (customer_id, start)):
        opening[r["currency"]] = r["b"]
    running = dict(opening)
    lines = []
    for r in conn.execute(
            """SELECT id, type, amount, currency, note, created_at FROM transactions
               WHERE customer_id = ? AND undone = 0 AND created_at >= ? AND created_at < ? ORDER BY created_at, id""",
            (customer_id, start, end)):
        running[r["currency"]] += r["amount"] if r["type"] == "debt" else -r["amount"]
        lines.append({"at": r["created_at"], "type": r["type"], "amount": r["amount"], "currency": r["currency"],
                      "note": r["note"] or "", "balance": running[r["currency"]]})
    debts = {c: sum(l["amount"] for l in lines if l["type"] == "debt" and l["currency"] == c) for c in db.CURRENCIES}
    paid = {c: sum(l["amount"] for l in lines if l["type"] == "payment" and l["currency"] == c) for c in db.CURRENCIES}
    return {"customer_id": customer["id"], "name": customer["name"], "kind": customer.get("kind", "customer"),
            "period": period, "period_words": PERIOD_WORDS.get(period, period), "from": start, "to": end,
            "opening": opening, "lines": lines, "debts": debts, "payments": paid, "closing": running,
            "based_on": {"entries": len(lines)}}
