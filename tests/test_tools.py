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
