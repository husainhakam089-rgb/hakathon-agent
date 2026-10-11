import json

import pytest

from app import db
from app.tools import TOOL_SCHEMAS, DebtTools


@pytest.fixture
def abu_ali(conn):
    return db.add_customer(conn, "أبو علي")


def test_schemas_match_handlers(tools):
    for schema in TOOL_SCHEMAS:
        assert hasattr(tools, f"tool_{schema['name']}")


def test_unknown_tool_and_bad_args(tools):
    assert tools.execute("drop_table", {})["error"] == "unknown_tool"
    assert tools.execute("get_balance", {"nope": 1})["error"] == "bad_arguments"


def test_results_are_json(tools, abu_ali):
    tools.execute("record_debt", {"customer_id": abu_ali, "amount": 25000, "currency": "IQD"})
    for schema in TOOL_SCHEMAS:
        args = {"customer_id": abu_ali} if "customer_id" in schema["input_schema"].get("required", []) else {}
        if schema["name"] == "find_customer":
            args = {"query": "علي"}
        if schema["name"] == "add_customer":
            args = {"name": "سعد"}
        if schema["name"] in ("record_debt", "record_payment"):
            args.update(amount=1000, currency="IQD")
        json.dumps(tools.execute(schema["name"], args), ensure_ascii=False)


def test_find_customer(tools, abu_ali):
    tools.execute("record_debt", {"customer_id": abu_ali, "amount": 25000, "currency": "IQD"})
    r = tools.execute("find_customer", {"query": "ابوعلي"})
    assert r["ok"] and r["count"] == 1
    assert r["matches"][0] == {"id": abu_ali, "name": "أبو علي", "balance": {"IQD": 25000, "USD": 0}}
    assert tools.execute("find_customer", {"query": "زيد"})["count"] == 0


def test_add_customer(tools, conn):
    tools.turn = 1
    assert tools.execute("add_customer", {"name": "سعد", "phone": "0780"})["error"] == "ask_first"
    tools.turn = 2  # the owner answered "إي"
    r = tools.execute("add_customer", {"name": "سعد", "phone": "0780"})
    assert r["ok"] and db.get_customer(conn, r["customer_id"])["phone"] == "0780"
    dup = tools.execute("add_customer", {"name": "  سَعد "})
    assert not dup["ok"] and dup["error"] == "customer_exists"
    assert tools.execute("add_customer", {"name": "  "})["error"] == "invalid_name"


def test_record_debt_and_payment(tools, abu_ali):
    r = tools.execute("record_debt", {"customer_id": abu_ali, "amount": 25000, "currency": "IQD"})
    assert r["ok"] and r["balance"] == {"IQD": 25000, "USD": 0}
    assert r["customer_name"] == "أبو علي"
    r = tools.execute("record_payment", {"customer_id": abu_ali, "amount": 10000, "currency": "IQD"})
    assert r["ok"] and r["balance"]["IQD"] == 15000
    assert "overpayment" not in r


def test_currencies_are_separate(tools, abu_ali):
    tools.execute("record_debt", {"customer_id": abu_ali, "amount": 50000, "currency": "IQD"})
    r = tools.execute("record_payment", {"customer_id": abu_ali, "amount": 20, "currency": "usd"})
    assert r["currency"] == "USD"
    assert r["balance"] == {"IQD": 50000, "USD": -20}
    assert r["overpayment"] is True


def test_source_message_is_saved(tools, conn, abu_ali):
    tools.source_message = "سجل على ابو علي 25 الف"
    r = tools.execute("record_debt", {"customer_id": abu_ali, "amount": 25000, "currency": "IQD"})
    assert db.get_transaction(conn, r["transaction_id"])["source_message"] == "سجل على ابو علي 25 الف"


@pytest.mark.parametrize("amount", [0, -100, 2.5, "25000", True, None])
def test_invalid_amount(tools, abu_ali, amount):
    r = tools.execute("record_debt", {"customer_id": abu_ali, "amount": amount, "currency": "IQD"})
    assert r == {"ok": False, "error": "invalid_amount", "message": r["message"]}


def test_integral_float_amount_accepted(tools, abu_ali):
    assert tools.execute("record_debt", {"customer_id": abu_ali, "amount": 25000.0, "currency": "IQD"})["ok"]


def test_invalid_currency(tools, abu_ali):
    r = tools.execute("record_debt", {"customer_id": abu_ali, "amount": 100, "currency": "EUR"})
    assert r["error"] == "invalid_currency"


@pytest.mark.parametrize("cid", [999, "abc", None])
def test_missing_customer(tools, cid):
    for name in ("record_debt", "record_payment"):
        r = tools.execute(name, {"customer_id": cid, "amount": 100, "currency": "IQD"})
        assert r["error"] == "customer_not_found"
    assert tools.execute("get_balance", {"customer_id": cid})["error"] == "customer_not_found"
    assert tools.execute("get_history", {"customer_id": cid})["error"] == "customer_not_found"


def test_overpayment(tools, abu_ali):
    tools.execute("record_debt", {"customer_id": abu_ali, "amount": 10000, "currency": "IQD"})
    r = tools.execute("record_payment", {"customer_id": abu_ali, "amount": 15000, "currency": "IQD"})
    assert r["ok"] and r["overpayment"] is True and r["overpaid_by"] == 5000
    assert r["balance"]["IQD"] == -5000


def test_large_amount_requires_confirmation(tools, conn, abu_ali):
    args = {"customer_id": abu_ali, "amount": 3_000_000, "currency": "IQD"}
    r = tools.execute("record_debt", args)
    assert not r["ok"] and r["error"] == "confirmation_required"
    assert db.get_balance(conn, abu_ali)["IQD"] == 0
    r = tools.execute("record_debt", {**args, "confirmed": True})
    assert r["ok"] and r["balance"]["IQD"] == 3_000_000


def test_large_amount_boundaries(tools, abu_ali):
    ok = tools.execute("record_debt", {"customer_id": abu_ali, "amount": 1_000_000, "currency": "IQD"})
    assert ok["ok"]
    usd = tools.execute("record_payment", {"customer_id": abu_ali, "amount": 1001, "currency": "USD"})
    assert usd["error"] == "confirmation_required"
    assert tools.execute("record_debt", {"customer_id": abu_ali, "amount": 1000, "currency": "USD"})["ok"]


def test_large_amount_from_env(conn, monkeypatch):
    monkeypatch.setenv("LARGE_AMOUNT_IQD", "500000")
    assert DebtTools(conn).large_amount_iqd == 500000


def test_get_balance_and_history(tools, abu_ali):
    for a in (1000, 2000, 3000):
        tools.execute("record_debt", {"customer_id": abu_ali, "amount": a, "currency": "IQD"})
    assert tools.execute("get_balance", {"customer_id": abu_ali})["balance"]["IQD"] == 6000
    h = tools.execute("get_history", {"customer_id": abu_ali, "limit": 2})
    assert [t["amount"] for t in h["transactions"]] == [3000, 2000]


def test_list_debtors(tools, conn):
    a = db.add_customer(conn, "أبو علي")
    b = db.add_customer(conn, "حجي كريم")
    c = db.add_customer(conn, "سعد")
    d = db.add_customer(conn, "زيد")
    tools.execute("record_debt", {"customer_id": a, "amount": 25000, "currency": "IQD"})
    tools.execute("record_debt", {"customer_id": b, "amount": 75000, "currency": "IQD"})
    tools.execute("record_debt", {"customer_id": c, "amount": 50, "currency": "USD"})
    tools.execute("record_debt", {"customer_id": d, "amount": 5000, "currency": "IQD"})
    tools.execute("record_payment", {"customer_id": d, "amount": 5000, "currency": "IQD"})

    r = tools.execute("list_debtors", {})
    assert [x["name"] for x in r["debtors"]] == ["حجي كريم", "أبو علي", "سعد"]
    assert r["totals"] == {"IQD": 100000, "USD": 50}
    assert r["debtors"][0]["last_activity"] is not None
    assert len(tools.execute("list_debtors", {"limit": 1})["debtors"]) == 1


def test_undo_last(tools, conn, abu_ali):
    tools.execute("record_debt", {"customer_id": abu_ali, "amount": 25000, "currency": "IQD"})
    tools.execute("record_payment", {"customer_id": abu_ali, "amount": 10000, "currency": "IQD"})
    r = tools.execute("undo_last", {})
    assert r["ok"] and r["undone"]["type"] == "payment" and r["undone"]["amount"] == 10000
    assert r["balance"]["IQD"] == 25000
    r = tools.execute("undo_last", {})
    assert r["undone"]["type"] == "debt" and r["balance"]["IQD"] == 0
    assert tools.execute("undo_last", {})["error"] == "nothing_to_undo"
    # Nothing is deleted
    assert len(db.get_history(conn, abu_ali)) == 2


def test_undo_is_per_session(conn, abu_ali):
    s1, s2 = DebtTools(conn), DebtTools(conn)
    s1.execute("record_debt", {"customer_id": abu_ali, "amount": 1000, "currency": "IQD"})
    assert s2.execute("undo_last", {})["error"] == "nothing_to_undo"
    assert s1.execute("undo_last", {})["ok"]


def test_undo_batch(tools, conn):
    cid = db.add_customer(conn, "أبو علي")
    assert tools.execute("undo_batch", {})["error"] == "nothing_to_undo"
    db.import_batch(conn, [
        {"customer_id": cid, "type": "debt", "amount": 25000, "currency": "IQD"},
        {"customer_id": cid, "type": "debt", "amount": 5, "currency": "USD"},
    ], batch_id="b1", source="image:f")
    r = tools.execute("undo_batch", {})
    assert r["ok"] and r["undone_count"] == 2 and r["customers"] == ["أبو علي"]
    assert r["totals"] == {"debt_IQD": 25000, "debt_USD": 5}
    assert db.get_balance(conn, cid) == {"IQD": 0, "USD": 0}
    assert tools.execute("undo_batch", {"batch_id": "b1"})["error"] == "nothing_to_undo"



def test_add_customer_needs_a_later_turn(tools, conn):
    """Rule 1 in code: a name first seen in this turn cannot be added in this turn."""
    tools.turn = 1
    assert tools.execute("find_customer", {"query": "سعد"})["count"] == 0
    r = tools.execute("add_customer", {"name": "سعد"})
    assert r["error"] == "ask_first" and "أضيفه" in r["message"]
    assert db.all_customers(conn) == []
    tools.turn = 2
    assert tools.execute("add_customer", {"name": "سَعد"})["ok"]  # same name, normalized
    tools.turn = 3
    assert tools.execute("add_customer", {"name": "كريم"})["error"] == "ask_first"  # new name, new question


# ---------- suppliers ledger (stage 10) ----------

@pytest.fixture
def zain(conn):
    return db.add_customer(conn, "شركة الزين", kind="supplier")


def test_ledgers_are_separate(tools, conn, zain, abu_ali):
    db.add_customer(conn, "أبو علي", kind="supplier")  # same name, the other ledger
    assert [m["name"] for m in tools.execute("find_customer", {"query": "ابو علي"})["matches"]] == ["أبو علي"]
    assert tools.execute("find_customer", {"query": "الزين"})["count"] == 0
    sup = tools.execute("find_supplier", {"query": "ابو علي"})["matches"]
    assert len(sup) == 1 and sup[0]["id"] != abu_ali
    assert tools.execute("find_supplier", {"query": "الزين"})["matches"][0]["id"] == zain


def test_tools_refuse_the_other_ledger(tools, zain, abu_ali):
    assert tools.execute("record_debt", {"customer_id": zain, "amount": 1000, "currency": "IQD"})["error"] == "customer_not_found"
    assert tools.execute("record_payment", {"customer_id": zain, "amount": 1000, "currency": "IQD"})["error"] == "customer_not_found"
    assert tools.execute("get_balance", {"customer_id": zain})["error"] == "customer_not_found"
    assert tools.execute("record_purchase", {"supplier_id": abu_ali, "amount": 1000, "currency": "IQD"})["error"] == "supplier_not_found"
    assert tools.execute("get_supplier_balance", {"supplier_id": abu_ali})["error"] == "supplier_not_found"


def test_purchase_and_supplier_payment(tools, conn, zain):
    r = tools.execute("record_purchase", {"supplier_id": zain, "amount": 500000, "currency": "IQD"})
    assert r["ok"] and r["type"] == "purchase" and r["balance_owed_by_me"]["IQD"] == 500000
    r = tools.execute("record_supplier_payment", {"supplier_id": zain, "amount": 200000, "currency": "IQD"})
    assert r["type"] == "supplier_payment" and r["balance_owed_by_me"]["IQD"] == 300000
    r = tools.execute("record_supplier_payment", {"supplier_id": zain, "amount": 400000, "currency": "IQD"})
    assert r["overpayment"] and r["overpaid_by"] == 100000  # now the supplier owes the shop
    assert r["meaning"] == "المورد صار هو المطلوب لصاحب المحل 100,000 دينار"
    assert tools.execute("get_supplier_balance", {"supplier_id": zain})["balance_owed_by_me"]["IQD"] == -100000


def test_supplier_large_amount_needs_confirmation(tools, zain):
    r = tools.execute("record_purchase", {"supplier_id": zain, "amount": 3_000_000, "currency": "IQD"})
    assert r["error"] == "confirmation_required"
    assert tools.execute("record_purchase", {"supplier_id": zain, "amount": 3_000_000, "currency": "IQD",
                                             "confirmed": True})["ok"]


def test_list_suppliers_and_customers_dont_mix(tools, conn, zain, abu_ali):
    pepsi = db.add_customer(conn, "مندوب البيبسي", kind="supplier")
    tools.execute("record_purchase", {"supplier_id": zain, "amount": 500000, "currency": "IQD"})
    tools.execute("record_purchase", {"supplier_id": pepsi, "amount": 150000, "currency": "IQD"})
    tools.execute("record_purchase", {"supplier_id": pepsi, "amount": 40, "currency": "USD"})
    tools.execute("record_debt", {"customer_id": abu_ali, "amount": 25000, "currency": "IQD"})
    s = tools.execute("list_suppliers", {})
    assert [x["name"] for x in s["suppliers"]] == ["شركة الزين", "مندوب البيبسي"]
    assert s["total_owed_by_me"] == {"IQD": 650000, "USD": 40}
    d = tools.execute("list_debtors", {})
    assert [x["name"] for x in d["debtors"]] == ["أبو علي"] and d["totals"]["IQD"] == 25000


def test_add_supplier_asks_first_and_is_separate_from_customers(tools, conn):
    tools.turn = 1
    assert tools.execute("find_supplier", {"query": "سعد"})["count"] == 0
    assert tools.execute("add_supplier", {"name": "سعد"})["error"] == "ask_first"
    tools.turn = 2
    r = tools.execute("add_supplier", {"name": "سعد"})
    assert r["ok"] and db.get_customer(conn, r["supplier_id"])["kind"] == "supplier"
    assert tools.execute("add_customer", {"name": "سعد"})["error"] == "ask_first"  # a customer سعد is new
    tools.turn = 3
    assert tools.execute("add_customer", {"name": "سعد"})["ok"]
    assert tools.execute("add_supplier", {"name": "سعد"})["error"] == "supplier_exists"


def test_undo_last_supplier_entry(tools, zain):
    tools.execute("record_purchase", {"supplier_id": zain, "amount": 500000, "currency": "IQD"})
    r = tools.execute("undo_last", {})
    assert r["undone"]["ledger"] == "supplier" and r["undone"]["type"] == "purchase"
    assert r["undone"]["customer_name"] == "شركة الزين" and r["balance"]["IQD"] == 0


# ---------- direction checks on the owner's words (stage 10) ----------

from app.tools import _OWNER_PAID, _OWNER_TOOK  # noqa: E402


@pytest.mark.parametrize("message, owner_paid", [
    ("دفعت لأبو علي 10 آلاف", True), ("دفعت لشركة الزين 200 ألف", True), ("سددت للمندوب", True),
    ("أعطيت المندوب 50", True), ("اني دفعت للشركة مليون", True), ("اليوم دفعت لابو علي", True),
    ("ابو علي دفع عشرة", False), ("أبو علي دفعلي 10", False),
    ("ام حسين دفعت عشرين", False),            # "دفعت" = she paid
    ("دفعت لي ام حسين عشرين", False), ("دفعتلي ام حسين", False), ("الحجي دفع", False),
])
def test_owner_paid_pattern(message, owner_paid):
    assert bool(_OWNER_PAID.search(message)) == owner_paid


@pytest.mark.parametrize("message, owner_took", [
    ("أخذت من شركة الزين بضاعة بمليون", True), ("اخذت بضاعة من المندوب", True),
    ("أخذت من أبو علي 10 آلاف", True), ("خذيت من الشركة", True),
    ("ام حسين اخذت بضاعة بعشرين", False),     # "أخذت" = she took
    ("ابو علي اخذ بضاعة", False), ("أخذت من عندي ام حسين", False), ("سجل على ابو علي", False),
])
def test_owner_took_pattern(message, owner_took):
    assert bool(_OWNER_TOOK.search(message)) == owner_took


def test_owner_paid_is_never_a_customer_payment(tools, abu_ali):
    tools.source_message = "دفعت لأبو علي 10 آلاف"
    r = tools.execute("record_payment", {"customer_id": abu_ali, "amount": 10000, "currency": "IQD"})
    assert r["error"] == "wrong_ledger"
    tools.source_message = "ام حسين دفعت عشرين"  # a customer paying: allowed
    assert tools.execute("record_payment", {"customer_id": abu_ali, "amount": 20000, "currency": "IQD"})["ok"]


def test_owner_took_is_never_a_customer_debt(tools, abu_ali):
    tools.source_message = "أخذت من أبو علي بضاعة بعشرة"
    assert tools.execute("record_debt", {"customer_id": abu_ali, "amount": 10000, "currency": "IQD"})["error"] == "wrong_ledger"
    tools.source_message = "أخذت من أبو علي 10 آلاف"  # he paid me: a payment is fine
    assert tools.execute("record_payment", {"customer_id": abu_ali, "amount": 10000, "currency": "IQD"})["ok"]


# ---------- withdrawals (stage 11) ----------

def test_withdrawals_record_list_and_undo(tools, conn):
    r = tools.execute("record_withdrawal", {"amount": 50000, "currency": "IQD", "reason": "المولدة"})
    assert r["ok"] and r["today_total"]["IQD"] == 50000
    tools.execute("record_withdrawal", {"amount": 25000, "currency": "IQD", "reason": "مولده"})  # same reason
    tools.execute("record_withdrawal", {"amount": 200000, "currency": "IQD", "reason": "إيجار"})
    tools.execute("record_withdrawal", {"amount": 20, "currency": "USD", "reason": "شخصي"})
    s = tools.execute("list_withdrawals", {"period": "month"})
    assert s["totals"] == {"IQD": 275000, "USD": 20}
    assert [(x["totals"]["IQD"], x["totals"]["USD"]) for x in s["by_reason"]] == [(200000, 0), (75000, 0), (0, 20)]
    assert tools.execute("list_withdrawals", {"period": "today", "reason": "مولدة"})["totals"]["IQD"] == 75000

    u = tools.execute("undo_last", {})
    assert u["undone"]["ledger"] == "withdrawal" and u["undone"]["reason"] == "شخصي"
    assert tools.execute("list_withdrawals", {"period": "all"})["totals"] == {"IQD": 275000, "USD": 0}
    # withdrawals never touch the customers or suppliers ledgers
    assert db.all_balances(conn) == [] and conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0


def test_withdrawal_checks(tools):
    assert tools.execute("record_withdrawal", {"amount": 5000, "currency": "IQD"})["error"] == "reason_required"
    assert tools.execute("record_withdrawal", {"amount": 0, "currency": "IQD", "reason": "x"})["error"] == "invalid_amount"
    r = tools.execute("record_withdrawal", {"amount": 2_000_000, "currency": "IQD", "reason": "إيجار"})
    assert r["error"] == "confirmation_required"
    assert tools.execute("record_withdrawal", {"amount": 2_000_000, "currency": "IQD", "reason": "إيجار",
                                               "confirmed": True})["ok"]


def test_undo_last_mixes_ledgers_in_order(tools, abu_ali):
    tools.execute("record_debt", {"customer_id": abu_ali, "amount": 25000, "currency": "IQD"})
    tools.execute("record_withdrawal", {"amount": 50000, "currency": "IQD", "reason": "مولدة"})
    assert tools.execute("undo_last", {})["undone"]["ledger"] == "withdrawal"
    assert tools.execute("undo_last", {})["undone"]["customer_name"] == "أبو علي"
    assert tools.execute("undo_last", {})["error"] == "nothing_to_undo"


def test_withdrawal_periods(conn):
    db.add_withdrawal(conn, 1000, "IQD", "مولدة")
    old = db.add_withdrawal(conn, 9000, "IQD", "مولدة")
    conn.execute("UPDATE withdrawals SET created_at = '2020-01-15 10:00:00' WHERE id = ?", (old,))
    conn.commit()
    assert db.withdrawals_summary(conn, "today")["totals"]["IQD"] == 1000
    assert db.withdrawals_summary(conn, "month")["totals"]["IQD"] == 1000
    assert db.withdrawals_summary(conn, "all")["totals"]["IQD"] == 10000


# ---------- memory (stage 12) ----------

def test_remember_list_forget(tools, conn):
    tools.source_message = "تذكر إن الورقة يعني 10 آلاف"
    r = tools.execute("remember", {"fact": "الورقة = 10,000 دينار"})
    assert r["ok"]
    assert tools.execute("remember", {"fact": "الورقه = 10,000 دينار"})["error"] == "already_known"
    mems = tools.execute("list_memories", {})["memories"]
    assert [m["text"] for m in mems] == ["الورقة = 10,000 دينار"]
    assert tools.execute("forget", {"memory_id": r["memory_id"]})["forgot"]["text"] == "الورقة = 10,000 دينار"
    assert tools.execute("list_memories", {})["count"] == 0
    assert tools.execute("forget", {"memory_id": r["memory_id"]})["error"] == "memory_not_found"
    # forgetting never deletes the row
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1


def test_remember_only_when_the_owner_teaches(tools):
    tools.source_message = "سجل على ابو علي 25 الف"
    assert tools.execute("remember", {"fact": "أبو علي يحب يسجل بالدين"})["error"] == "ask_first"
    for message in ["من هسه الدفتر يعني مية ألف", "أبو علي نفسه علي النجار", "احفظ إن المندوب يجي الخميس"]:
        tools.source_message = message
        assert tools.execute("remember", {"fact": message})["ok"], message


def test_memory_limits(tools, conn, monkeypatch):
    tools.source_message = "تذكر"
    assert tools.execute("remember", {"fact": "x" * 201})["error"] == "too_long"
    assert tools.execute("remember", {"fact": "  "})["error"] == "invalid_fact"
    monkeypatch.setattr("app.tools.MAX_MEMORIES", 2)
    tools.execute("remember", {"fact": "أ"})
    tools.execute("remember", {"fact": "ب"})
    assert tools.execute("remember", {"fact": "ج"})["error"] == "memory_full"
