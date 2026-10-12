"""Run the agent on eval/cases.json and check the database after each message.

    python eval/run_eval.py              # all cases
    python eval/run_eval.py 1 3 4        # some cases
    python eval/run_eval.py -v           # also print tool calls

Each case gets a fresh in-memory database, so the real debt.db is never touched.
Uses the real model from .env (LLM_PROVIDER etc.).
"""

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from app import db  # noqa: E402
from app.agent import Agent  # noqa: E402
from app.llm import make_llm  # noqa: E402

CASES_PATH = Path(__file__).with_name("cases.json")
RESULTS_PATH = Path(__file__).with_name("last_results.json")


SUPPLIER = "مورد:"


def build_db(base: dict, extra: dict):
    """Fresh database from base_setup + the case's own setup. Returns (conn, ids, session_tx_ids)."""
    conn = db.connect(":memory:")
    ids = {}  # suppliers are keyed "مورد:<name>", so a name can be in both ledgers
    for name in base.get("customers", []) + extra.get("customers", []):
        ids[name] = db.add_customer(conn, name)
    for name in base.get("suppliers", []) + extra.get("suppliers", []):
        ids[SUPPLIER + name] = db.add_customer(conn, name, kind="supplier")
    session_txs = []
    for row in base.get("transactions", []) + extra.get("transactions", []):
        name, tx_type, amount, currency, *rest = row
        tx_id = db.add_transaction(conn, ids[name], tx_type, amount, currency)
        if rest and rest[0]:
            session_txs.append(tx_id)
    # stage 17: [name, price, cost, unit]; past sales: [item name, qty, payment] (cash/electronic)
    for name, price, cost, unit in base.get("items", []) + extra.get("items", []):
        ids["مادة:" + name] = db.add_item(conn, name, price, cost, unit)
    for item, qty, payment in extra.get("sales", []):
        it = db.get_item(conn, ids["مادة:" + item])
        db.add_sale(conn, payment, [{"item_id": it["id"], "qty": qty, "unit_price": it["price"], "unit_cost": it["cost"]}])
    for text in base.get("memories", []) + extra.get("memories", []):
        db.add_memory(conn, text)
    for row in base.get("withdrawals", []) + extra.get("withdrawals", []):
        amount, currency, reason, *rest = row
        wd_id = db.add_withdrawal(conn, amount, currency, reason)
        if rest and rest[0]:
            session_txs.append(("withdrawal", wd_id))
    # stage 15: a day at the drawer. The opening covers everything above (old balances),
    # then the day's own entries, then optionally a close.
    day = extra.get("drawer_day")
    if day:
        upto = db._max_ids(conn)
        conn.execute("INSERT INTO drawer_counts (kind, iqd, usd, tx_upto, wd_upto, ci_upto) VALUES ('open', ?, NULL, ?, ?, ?)",
                     (day["opening"], upto["tx_upto"], upto["wd_upto"], upto["ci_upto"]))
        conn.commit()
        for name, tx_type, amount, currency in day.get("transactions", []):
            db.add_transaction(conn, ids[name], tx_type, amount, currency)
        for amount, currency, reason in day.get("withdrawals", []):
            db.add_withdrawal(conn, amount, currency, reason)
        for amount, currency, label in day.get("cash_ins", []):
            db.add_cash_in(conn, amount, currency, label)
        if day.get("close") is not None:
            db.add_drawer_count(conn, "close", day["close"], None)
    return conn, ids, session_txs


def check(case: dict, result: dict, conn, ids: dict, before_max_id: int, seeded: list) -> list[str]:
    """Returns the list of failed checks (empty = pass)."""
    exp = case["expect"]
    names = {v: k for k, v in ids.items() if not k.startswith("مادة:")}  # item ids are another table
    failures = []

    new_rows = conn.execute(
        "SELECT customer_id, type, amount, currency FROM transactions WHERE id > ? AND undone = 0 ORDER BY id",
        (before_max_id,)).fetchall()
    got = sorted([names.get(r["customer_id"], f"#{r['customer_id']}"), r["type"], r["amount"], r["currency"]]
                 for r in new_rows)
    options = exp.get("transactions_any_of") or [exp.get("transactions", [])]
    wanted = [sorted(list(t) for t in option) for option in options]
    if got not in wanted:
        shown = " أو ".join(str(w or "ولا قيد") for w in wanted)
        failures.append(f"القيود: المتوقع {shown}، الموجود {got or 'ولا قيد'}")

    # withdrawals (stage 11): [amount, currency, word the reason must contain]
    before_wd = case.get("_before_wd", 0)
    new_wd = conn.execute("SELECT amount, currency, reason_key FROM withdrawals WHERE id > ? AND undone = 0 ORDER BY id",
                          (before_wd,)).fetchall()
    want_wd = exp.get("withdrawals", [])
    got_ok = len(new_wd) == len(want_wd) and all(
        r["amount"] == a and r["currency"] == c and db.reason_key(w) in r["reason_key"]
        for r, (a, c, w) in zip(new_wd, want_wd))
    if not got_ok:
        got = [[r["amount"], r["currency"], r["reason_key"]] for r in new_wd]
        failures.append(f"السحوبات: المتوقع {want_wd or 'ولا سحب'}، الموجود {got or 'ولا سحب'}")
    if "withdrawals_undone" in exp:
        n = conn.execute("SELECT COUNT(*) FROM withdrawals WHERE id <= ? AND undone = 1", (before_wd,)).fetchone()[0]
        if n != exp["withdrawals_undone"]:
            failures.append(f"السحوبات الملغية: المتوقع {exp['withdrawals_undone']}، الموجود {n}")

    if "memories_added" in exp:
        added = conn.execute("SELECT COUNT(*) FROM memories WHERE active = 1").fetchone()[0] - case.get("_before_mem", 0)
        if added != exp["memories_added"]:
            failures.append(f"الذاكرة: المتوقع ينحفظ {exp['memories_added']}، انحفظ {added}")

    # stage 15: the cash drawer
    before_dr = case.get("_before_dr", 0)
    new_counts = [dict(r) for r in conn.execute(
        "SELECT * FROM drawer_counts WHERE id > ? AND undone = 0 ORDER BY id", (before_dr,))]
    if "drawer_counts_added" in exp and len(new_counts) != exp["drawer_counts_added"]:
        failures.append(f"عدّات الجرار: المتوقع {exp['drawer_counts_added']}، الموجود {len(new_counts)}")
    if "drawer_opening" in exp:
        opens = [c["iqd"] for c in new_counts if c["kind"] == "open"]
        if opens[-1:] != [exp["drawer_opening"]]:
            failures.append(f"الفكة: المتوقع {exp['drawer_opening']}، الموجود {opens or 'ولا شي'}")
    if "drawer_cash_sales" in exp:
        closes = [c for c in new_counts if c["kind"] == "close"]
        got_sales = db.drawer_report(conn, closes[-1])["cash_sales"] if closes else None
        if got_sales != exp["drawer_cash_sales"]:
            failures.append(f"البيع النقدي: المتوقع {exp['drawer_cash_sales']}، الموجود {got_sales or 'ما انعد الجرار'}")
    if "sales" in exp:  # [[payment, total, customer or null, {item: qty}], ...] the new sales
        got_sales = []
        for r in conn.execute("SELECT id FROM sales WHERE id > ? AND undone = 0 ORDER BY id", (case.get("_before_sa", 0),)):
            sale = db.get_sale(conn, r["id"])
            who = names.get(sale["customer_id"]) if sale["customer_id"] else None
            got_sales.append([sale["payment"], sale["total"], who, {l["name"]: l["qty"] for l in sale["lines"]}])
        if got_sales != exp["sales"]:
            failures.append(f"البيع: المتوقع {exp['sales'] or 'ولا بيعة'}، الموجود {got_sales or 'ولا بيعة'}")
    if "items_added" in exp:
        n = conn.execute("SELECT COUNT(*) FROM items WHERE id > ?", (case.get("_before_it", 0),)).fetchone()[0]
        if n != exp["items_added"]:
            failures.append(f"المواد الجديدة: المتوقع {exp['items_added']}، الموجود {n}")
    if "cash_ins" in exp:  # [amount, currency, a word the label must contain]
        got_ci = [[r["amount"], r["currency"], r["label"]] for r in conn.execute(
            "SELECT * FROM cash_ins WHERE id > ? AND undone = 0 ORDER BY id", (case.get("_before_ci", 0),))]
        ok_ci = len(got_ci) == len(exp["cash_ins"]) and all(
            g[0] == a and g[1] == c and db.normalize(w) in db.normalize(g[2]) for g, (a, c, w) in zip(got_ci, exp["cash_ins"]))
        if not ok_ci:
            failures.append(f"الوارد: المتوقع {exp['cash_ins'] or 'ولا وارد'}، الموجود {got_ci or 'ولا وارد'}")
    if "from_drawer" in exp:
        last = conn.execute("SELECT from_drawer FROM transactions WHERE id > ? AND undone = 0 ORDER BY id DESC LIMIT 1",
                            (before_max_id,)).fetchone()
        if not last or bool(last["from_drawer"]) != exp["from_drawer"]:
            failures.append(f"من الجرار: المتوقع {exp['from_drawer']}، الموجود {None if not last else bool(last['from_drawer'])}")

    seeded_tx = [x for x in seeded if not isinstance(x, tuple)]
    for name in exp.get("undone", []):
        undone = conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE id IN ({}) AND customer_id = ? AND undone = 1".format(
                ",".join("?" * len(seeded_tx)) or "NULL"), (*seeded_tx, ids[name])).fetchone()[0]
        if not undone:
            failures.append(f"قيد {name} ما انلغى")

    # A call the tool refused (ok=false) did nothing, so only successful calls count.
    called = [c["tool"] for c in result["tool_calls"] if c["result"].get("ok")]
    for tool in exp.get("tools_called", []):
        if tool not in called:
            failures.append(f"ما استدعى {tool}")
    for tool in exp.get("tools_not_called", []):
        if tool in called:
            failures.append(f"استدعى {tool} وهو ممنوع")

    reply = result["reply"]
    if exp.get("reply_has_any") and not any(s in reply for s in exp["reply_has_any"]):
        failures.append(f"الرد ما بيه أي من {exp['reply_has_any']}")
    for s in exp.get("reply_has_all", []):
        if s not in reply:
            failures.append(f"الرد ما بيه {s!r}")
    return failures


def run_case(case: dict, base: dict, llm) -> dict:
    conn, ids, seeded = build_db(base, case.get("setup", {}))
    agent = Agent(conn, llm=llm, log_path=None)
    session = agent.session("eval")
    session.tools.session_tx_ids = list(seeded)
    before_max_id = conn.execute("SELECT COALESCE(MAX(id), 0) FROM transactions").fetchone()[0]
    case = {**case, "_before_wd": conn.execute("SELECT COALESCE(MAX(id), 0) FROM withdrawals").fetchone()[0],
            "_before_mem": conn.execute("SELECT COUNT(*) FROM memories WHERE active = 1").fetchone()[0],
            "_before_dr": conn.execute("SELECT COALESCE(MAX(id), 0) FROM drawer_counts").fetchone()[0],
            "_before_ci": conn.execute("SELECT COALESCE(MAX(id), 0) FROM cash_ins").fetchone()[0],
            "_before_sa": conn.execute("SELECT COALESCE(MAX(id), 0) FROM sales").fetchone()[0],
            "_before_it": conn.execute("SELECT COALESCE(MAX(id), 0) FROM items").fetchone()[0]}

    started = time.perf_counter()
    result = agent.chat("eval", case["message"])
    seconds = round(time.perf_counter() - started, 1)

    failures = check(case, result, conn, ids, before_max_id, seeded)
    conn.close()
    return {"id": case["id"], "message": case["message"], "passed": not failures, "failures": failures,
            "reply": result["reply"], "tools": [c["tool"] for c in result["tool_calls"]],
            "tool_calls": result["tool_calls"], "seconds": seconds,
            "model": getattr(llm, "model", None)}


def main() -> int:
    parser = argparse.ArgumentParser(description="تقييم وكيل دفتر الديون")
    parser.add_argument("ids", nargs="*", type=int, help="أرقام الحالات (فارغ = الكل)")
    parser.add_argument("-v", "--verbose", action="store_true", help="اطبع تفاصيل الأدوات")
    args = parser.parse_args()

    spec = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    cases = [c for c in spec["cases"] if not args.ids or c["id"] in args.ids]
    llm = make_llm()  # one client for all cases (keeps the model fallback state)

    results = []
    for case in cases:
        r = run_case(case, spec["base_setup"], llm)
        results.append(r)
        mark = "✅ نجح" if r["passed"] else "❌ فشل"
        print(f"{r['id']:>2}  {mark}  {r['seconds']:>5}s  {r['message']}   [{r['model'] or ''}]")
        print(f"      الأدوات: {', '.join(r['tools']) or '—'}")
        print(f"      الرد: {r['reply']}")
        for f in r["failures"]:
            print(f"      ⚠ {f}")
        if args.verbose:
            for c in r["tool_calls"]:
                print(f"        {c['tool']} {json.dumps(c['input'], ensure_ascii=False)} -> "
                      f"{json.dumps(c['result'], ensure_ascii=False)[:300]}")

    passed = sum(r["passed"] for r in results)
    models = sorted({r["model"] for r in results if r["model"]})
    model = ", ".join(models) or "?"
    print(f"\nالنتيجة: {passed}/{len(results)} ({round(100 * passed / max(len(results), 1))}%)  النماذج: {model}")
    RESULTS_PATH.write_text(json.dumps({"model": model, "passed": passed, "total": len(results),
                                        "results": results}, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
