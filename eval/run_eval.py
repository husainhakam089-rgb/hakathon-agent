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


def build_db(base: dict, extra: dict):
    """Fresh database from base_setup + the case's own setup. Returns (conn, ids, session_tx_ids)."""
    conn = db.connect(":memory:")
    ids = {}
    for name in base.get("customers", []) + extra.get("customers", []):
        ids[name] = db.add_customer(conn, name)
    session_txs = []
    for row in base.get("transactions", []) + extra.get("transactions", []):
        name, tx_type, amount, currency, *rest = row
        tx_id = db.add_transaction(conn, ids[name], tx_type, amount, currency)
        if rest and rest[0]:
            session_txs.append(tx_id)
    return conn, ids, session_txs


def check(case: dict, result: dict, conn, ids: dict, before_max_id: int, seeded: list) -> list[str]:
    """Returns the list of failed checks (empty = pass)."""
    exp = case["expect"]
    names = {v: k for k, v in ids.items()}
    failures = []

    new_rows = conn.execute(
        "SELECT customer_id, type, amount, currency FROM transactions WHERE id > ? AND undone = 0 ORDER BY id",
        (before_max_id,)).fetchall()
    got = sorted([names.get(r["customer_id"], f"#{r['customer_id']}"), r["type"], r["amount"], r["currency"]]
                 for r in new_rows)
    want = sorted(list(t) for t in exp.get("transactions", []))
    if got != want:
        failures.append(f"القيود: المتوقع {want or 'ولا قيد'}، الموجود {got or 'ولا قيد'}")

    for name in exp.get("undone", []):
        undone = conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE id IN ({}) AND customer_id = ? AND undone = 1".format(
                ",".join("?" * len(seeded)) or "NULL"), (*seeded, ids[name])).fetchone()[0]
        if not undone:
            failures.append(f"قيد {name} ما انلغى")

    called = [c["tool"] for c in result["tool_calls"]]
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
