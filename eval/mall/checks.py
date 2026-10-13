"""Stage 20 checks that need no model (fast, the same every run):

1. The paper month: last month added up from the events alone vs. the app's own reports.
2. 20 planted errors (and 20 clean twins): how many were caught, and how many false alarms.
3. The cash forecast: built on the first two months, compared with the third.

    python eval/mall/checks.py          # prints the results and writes eval/mall/checks.json
"""

import json
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mall import CUSTOMERS, DAYS, ITEMS, LATE_PAYERS, SHORT_DAYS, SUPPLIERS, day_date, mall  # noqa: E402

from app import db, reports  # noqa: E402
from app.agent import Agent  # noqa: E402
from app.llm import LLMResponse, ToolCall  # noqa: E402
from app.tools import DebtTools  # noqa: E402

OUT = Path(__file__).with_name("checks.json")


# ---------- 1. the paper month ----------

def paper_month(paper, conn, ids) -> dict:
    """Last month (a whole month) line by line. Each row: what, paper, app, equal?"""
    rows = []

    def row(what, paper_value, app_value):
        rows.append({"what": what, "paper": paper_value, "app": app_value, "equal": paper_value == app_value})

    p = reports.profit(conn, "last_month")
    row("البيع كله", paper.sales_total("last_month"), p["sales"]["IQD"])
    for pay, word in (("cash", "كاش"), ("debt", "دين"), ("electronic", "إلكتروني")):
        app_pay = conn.execute(
            """SELECT COALESCE(SUM(total), 0) FROM sales WHERE undone = 0 AND payment = ?
               AND created_at >= ? AND created_at < ?""", (pay, p["from"], p["to"])).fetchone()[0]
        row(f"البيع {word}", paper.sales_total("last_month", pay), app_pay)
    row("الربح الصافي", paper.profit("last_month"), p["net_profit"]["IQD"])
    for reason in ("مولدة", "إيجار", "رواتب", "أكل", "نقل", "البيت"):
        app_value = conn.execute(
            """SELECT COALESCE(SUM(amount), 0) FROM withdrawals WHERE undone = 0 AND reason_key = ?
               AND created_at >= ? AND created_at < ?""", (db.reason_key(reason), p["from"], p["to"])).fetchone()[0]
        row(f"سحوبات: {reason}", paper.withdrawals("last_month", reason), app_value)
    for name, *_ in ITEMS[:8]:
        qty, revenue = paper.item_sales(name, "last_month")
        app_rev = conn.execute(
            """SELECT COALESCE(SUM(l.line_total), 0) FROM sale_lines l JOIN sales s ON s.id = l.sale_id
               WHERE s.undone = 0 AND l.item_id = ? AND s.created_at >= ? AND s.created_at < ?""",
            (ids["item"][name], p["from"], p["to"])).fetchone()[0]
        row(f"بيع {name}", revenue, app_rev)
    # balances now (every entry, all months)
    balances = {r["name"]: r["balances"]["IQD"] for r in db.all_balances(conn, "customer")}
    for c in CUSTOMERS:
        row(f"رصيد {c}", paper.customer[c], balances[c])
    sup = {r["name"]: r["balances"]["IQD"] for r in db.all_balances(conn, "supplier")}
    for s in SUPPLIERS:
        row(f"عليّ لـ {s}", paper.supplier[s], sup[s])
    # every drawer count of last month
    closes = {c["counted"]["at"][:10]: c for c in db.drawer_closes(conn, 200)}
    for c in paper.closes:
        d = day_date(c["day"], paper.today)
        if d.month == (paper.today.replace(day=1) - timedelta(days=1)).month:
            app_c = closes[d.isoformat()]
            row(f"الجرار {d.isoformat()}: الفرق", c["difference"], app_c["difference"]["IQD"])
    equal = sum(r["equal"] for r in rows)
    return {"rows": rows, "equal": equal, "total": len(rows)}


# ---------- 2. planted errors ----------

def planted_errors(paper, conn, ids) -> dict:
    """20 errors put in on purpose, and 20 clean twins (an alarm on a twin is a false alarm)."""
    found, twins = [], []

    def case(kind, what, caught, twin_alarm):
        found.append({"kind": kind, "what": what, "caught": caught})
        twins.append({"kind": kind, "what": what, "false_alarm": twin_alarm})

    # a) 4 drawer shortages planted in the data (and 4 normal days next to them)
    closes = {c["counted"]["at"][:10]: c for c in db.drawer_closes(conn, 200)}
    for day in SHORT_DAYS:
        bad = closes[day_date(day, paper.today).isoformat()]
        good = closes[day_date(day + 1, paper.today).isoformat()]
        case("فرق بالجرار", f"يوم {day}: ناقص {-paper.close_on(day)['difference']:,}", bad["shortage"], good["shortage"])

    # b) 3 customers who stopped paying (+1 who pays): the overdue alert
    late = {o["name"] for o in db.overdue_debts(conn, 30)}
    # the twin: someone who owes money but paid within the last 10 days (an alarm on them is wrong)
    last_paid = {}
    for e in paper.events:
        if e["kind"] == "payment":
            last_paid[e["customer"]] = e["day"]
    payer = next(c for c in CUSTOMERS if c not in LATE_PAYERS and paper.customer[c] > 0
                 and last_paid.get(c, -99) >= DAYS - 10)
    for name in LATE_PAYERS:
        case("دين قديم", f"{name} ما سدد من أكثر من شهر", name in late, payer in late)
    case("دين قديم", "دين قديم مكتوب قبل 60 يوم بدون أي تسديد",
         *_old_debt_case(conn, ids))

    # c) 4 resent messages (the same payment twice within minutes) and 4 different payments
    for i, name in enumerate(["أبو علي", "أم حسين", "كرار", "سجاد"]):
        tools = DebtTools(conn)
        tools.source_message = f"{name} دفع {10 + i}"
        tools.turn = 1
        cid = ids["customer"][name]
        first = tools.execute("record_payment", {"customer_id": cid, "amount": (10 + i) * 1000, "currency": "IQD"})
        tools.turn = 2
        tools._turn_entries = set()
        again = tools.execute("record_payment", {"customer_id": cid, "amount": (10 + i) * 1000, "currency": "IQD"})
        other = tools.execute("record_payment", {"customer_id": cid, "amount": (10 + i) * 1000 + 500, "currency": "IQD"})
        case("دفعة مكررة", f"{name} {(10 + i) * 1000:,} مرتين", first["ok"] and again.get("error") == "possible_duplicate",
             other.get("error") == "possible_duplicate")
        for r in (first, other):  # leave the notebook as it was
            if r.get("ok"):
                db.mark_undone(conn, r["transaction_id"])

    # d) 4 invented figures in replies (and 4 replies with only real figures)
    for i, (fake, real) in enumerate([(125000, 25000), (980000, 50000), (77000, 10000), (3400000, 360000)]):
        caught = _invented(conn, ids, real, fake)
        false = _invented(conn, ids, real, None)
        case("رقم مخترع", f"الرد يگول {fake:,} والحساب {real:,}", caught, false)

    # e) 4 times "دفعت لـ (زبون)" = the owner paid, never a customer's payment (and 4 "X دفع")
    for name in ["أبو علي", "أم زينب", "الحجي كريم", "أبو أحمد"]:
        tools = DebtTools(conn)
        tools.source_message = f"دفعت لـ{name} 20 ألف"
        wrong = tools.execute("record_payment", {"customer_id": ids["customer"][name], "amount": 20000, "currency": "IQD"})
        tools.source_message = f"{name} دفع 20 ألف"
        right = tools.execute("record_payment", {"customer_id": ids["customer"][name], "amount": 20000, "currency": "IQD",
                                                 "confirmed": True})
        case("دفتر غلط", f"دفعت لـ{name}: مو تسديد زبون", wrong.get("error") == "wrong_ledger",
             right.get("error") == "wrong_ledger")
        if right.get("ok"):
            db.mark_undone(conn, right["transaction_id"])

    caught = sum(f["caught"] for f in found)
    alarms = sum(t["false_alarm"] for t in twins)
    by_kind = {}
    for f, t in zip(found, twins):
        k = by_kind.setdefault(f["kind"], {"planted": 0, "caught": 0, "false_alarms": 0})
        k["planted"] += 1
        k["caught"] += f["caught"]
        k["false_alarms"] += t["false_alarm"]
    return {"planted": len(found), "caught": caught, "false_alarms": alarms, "twins": len(twins),
            "by_kind": by_kind, "cases": found}


def _old_debt_case(conn, ids):
    """A new customer with one debt 60 days ago and nothing since (+ one with a debt yesterday)."""
    old = db.add_customer(conn, "زبون قديم (مزروع)")
    t = db.add_transaction(conn, old, "debt", 30000)
    conn.execute("UPDATE transactions SET created_at = datetime('now','localtime','-60 days') WHERE id = ?", (t,))
    new = db.add_customer(conn, "زبون جديد (مزروع)")
    t2 = db.add_transaction(conn, new, "debt", 30000)
    conn.execute("UPDATE transactions SET created_at = datetime('now','localtime','-1 days') WHERE id = ?", (t2,))
    conn.commit()
    late = {o["name"] for o in db.overdue_debts(conn, 30)}
    return "زبون قديم (مزروع)" in late, "زبون جديد (مزروع)" in late


class _Scripted:
    def __init__(self, *steps):
        self.steps = list(steps)

    def complete(self, system, messages, tools):
        return self.steps.pop(0)


def _invented(conn, ids, real, fake) -> bool:
    """The model reads a balance (real) and replies with fake (or with real when fake is None).
    True = the code flagged the reply. Each check has its own customer (the notebook is untouched)."""
    cid = db.add_customer(conn, "زبون فحص " + "أبجد"[len(str(real)) % 4] + ("ـ" * (fake is None)))  # no digits in the name
    db.add_transaction(conn, cid, "debt", real)
    reply = f"عليه {(fake if fake is not None else real):,} دينار."
    steps = [LLMResponse(text="", tool_calls=[ToolCall("a", "get_balance", {"customer_id": cid})]),
             LLMResponse(text=reply), LLMResponse(text=reply)]  # the same again after the note
    result = Agent(conn, llm=_Scripted(*steps), log_path=None).chat(f"planted-{cid}", "شكد عليه؟")
    return any(c["tool"] == "number_check" for c in result["tool_calls"])


# ---------- 3. the forecast ----------

def forecast_backtest(paper, conn) -> dict:
    """The forecast as it would have said on day 60, for days 60-89, vs. what happened."""
    cut = 60
    cut_date = day_date(cut, paper.today)
    horizon = DAYS - cut
    f = reports.cash_forecast(conn, days=horizon, weeks=8, today=cut_date)
    real_in = real_out = 0
    for e in paper.events:
        if e["day"] < cut:
            continue
        k = e["kind"]
        if k == "sale" and e["payment"] == "cash":
            real_in += e["total"]
        elif k in ("payment", "cash_in"):
            real_in += e["amount"]
        elif k == "supplier_payment":
            real_out += e["amount"]
        elif k == "withdrawal" and not e.get("take_home"):
            real_out += e["amount"]
        elif k == "close" and e["extra"] > 0:
            real_in += e["extra"]
    pred_out = f["expected_out"]["IQD"]  # money taken home is the owner's, not the shop's: left out on both sides
    def err(pred, real):
        return round(100 * (pred - real) / real, 1) if real else None
    return {"from_day": cut, "days": horizon, "based_on_days": f["based_on_days"],
            "predicted_in": f["expected_in"]["IQD"], "real_in": real_in, "error_in_pct": err(f["expected_in"]["IQD"], real_in),
            "predicted_out": pred_out, "real_out": real_out, "error_out_pct": err(pred_out, real_out),
            "fixed_expenses": f["fixed_expenses"]}


def main() -> int:
    events, paper, conn, ids = mall()
    month = paper_month(paper, conn, ids)
    forecast = forecast_backtest(paper, conn)
    planted = planted_errors(paper, conn, ids)
    out = {"paper_month": month, "planted": planted, "forecast": forecast}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"الحساب الورقي: {month['equal']}/{month['total']} رقم مطابق")
    for r in month["rows"]:
        if not r["equal"]:
            print(f"   ⚠ {r['what']}: الورق {r['paper']:,}، البرنامج {r['app']:,}")
    print(f"الأخطاء المزروعة: انمسك {planted['caught']}/{planted['planted']}، "
          f"وتنبيه غلط {planted['false_alarms']}/{planted['twins']}")
    for k, v in planted["by_kind"].items():
        print(f"   {k}: {v['caught']}/{v['planted']}، تنبيه غلط {v['false_alarms']}")
    print(f"التوقع: من يوم {forecast['from_day']} لـ {forecast['days']} يوم — الداخل المتوقع {forecast['predicted_in']:,} "
          f"والحقيقي {forecast['real_in']:,} (خطأ {forecast['error_in_pct']}%)، الطالع المتوقع {forecast['predicted_out']:,} "
          f"والحقيقي {forecast['real_out']:,} (خطأ {forecast['error_out_pct']}%)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
