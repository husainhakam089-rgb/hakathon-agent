"""Stage 17: items and sales (sale screen and chat)."""

import pytest

from app import db


@pytest.fixture
def shop(conn):
    ali = db.add_customer(conn, "أبو علي")
    flour = db.add_item(conn, "طحين", 25000, 22000, "كيس")
    oil = db.add_item(conn, "زيت", 4000, 3000, "قنينة")
    soap = db.add_item(conn, "صابون", 1000, None, "قطعة")  # cost unknown
    return {"ali": ali, "flour": flour, "oil": oil, "soap": soap}


def test_cash_sale_total_and_profit_by_code(conn, tools, shop):
    r = tools.execute("record_sale", {"payment": "cash", "lines": [
        {"item_id": shop["flour"], "qty": 2}, {"item_id": shop["oil"], "qty": 3}]})
    assert r["ok"] and r["total"] == 62000 and r["profit"] == 62000 - 53000
    assert r["what"] == "طحين ×2، زيت ×3"
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0  # no ledger touched


def test_debt_sale_goes_to_the_customer(conn, tools, shop):
    r = tools.execute("record_sale", {"payment": "debt", "customer_id": shop["ali"],
                                      "lines": [{"item_id": shop["flour"], "qty": 1}]})
    assert r["ok"] and r["balance"]["IQD"] == 25000 and r["customer_name"] == "أبو علي"
    hist = db.get_history(conn, shop["ali"])
    assert hist[0]["type"] == "debt" and "طحين" in hist[0]["note"]


def test_debt_sale_needs_a_customer(tools, shop):
    r = tools.execute("record_sale", {"payment": "debt", "lines": [{"item_id": shop["flour"]}]})
    assert r["error"] == "customer_required"


def test_price_override_and_unknown_cost(conn, tools, shop):
    r = tools.execute("record_sale", {"payment": "cash", "lines": [
        {"item_id": shop["flour"], "qty": 1, "unit_price": 24000}, {"item_id": shop["soap"], "qty": 5}]})
    assert r["total"] == 29000 and "profit" not in r  # a cost is unknown: no made-up profit
    s = db.sales_summary(conn, "today")
    soap = next(i for i in s["items"] if i["name"] == "صابون")
    flour = next(i for i in s["items"] if i["name"] == "طحين")
    assert soap["profit"] is None and flour["profit"] == 2000


def test_bad_sales_are_refused(tools, shop):
    assert tools.execute("record_sale", {"payment": "cash", "lines": []})["error"] == "no_lines"
    assert tools.execute("record_sale", {"payment": "cash", "lines": [{"item_id": 999}]})["error"] == "item_not_found"
    assert tools.execute("record_sale", {"payment": "cash", "lines": [{"item_id": shop["oil"], "qty": 0}]})["error"] == "invalid_qty"
    assert tools.execute("record_sale", {"payment": "card", "lines": [{"item_id": shop["oil"]}]})["error"] == "invalid_payment"
    big = tools.execute("record_sale", {"payment": "cash", "lines": [{"item_id": shop["flour"], "qty": 50}]})
    assert big["error"] == "confirmation_required"
    assert tools.execute("record_sale", {"payment": "cash", "confirmed": True,
                                         "lines": [{"item_id": shop["flour"], "qty": 50}]})["ok"]


def test_undo_a_debt_sale_undoes_the_debt(conn, tools, shop):
    tools.execute("record_sale", {"payment": "debt", "customer_id": shop["ali"],
                                  "lines": [{"item_id": shop["oil"], "qty": 2}]})
    r = tools.execute("undo_last", {})
    assert r["ok"] and r["undone"]["type"] == "sale" and r["balance"]["IQD"] == 0
    assert db.sales_summary(conn, "today")["totals"]["IQD"] == 0


def test_item_sales_this_month(conn, tools, shop):
    for qty in (2, 3):
        tools.execute("record_sale", {"payment": "cash", "lines": [{"item_id": shop["flour"], "qty": qty}]})
    tools.execute("record_sale", {"payment": "electronic", "lines": [{"item_id": shop["oil"], "qty": 1}]})
    r = tools.execute("item_sales", {"period": "month", "item_id": shop["flour"]})
    flour = r["items"][0]
    assert flour["qty"] == 5 and flour["revenue"] == 125000 and flour["profit"] == 15000 and flour["sales"] == 2
    assert r["by_payment"]["cash"]["IQD"] == 125000 and r["by_payment"]["electronic"]["IQD"] == 0
    everything = tools.execute("item_sales", {"period": "week"})
    assert everything["by_payment"]["electronic"]["IQD"] == 4000 and everything["totals"]["IQD"] == 129000


def test_cash_sales_go_into_the_drawer_electronic_do_not(conn, tools, shop):
    tools.execute("set_drawer_opening", {"iqd": 0})
    tools.execute("record_sale", {"payment": "cash", "lines": [{"item_id": shop["flour"], "qty": 2}]})
    tools.execute("record_sale", {"payment": "electronic", "lines": [{"item_id": shop["oil"], "qty": 1}]})
    tools.execute("record_sale", {"payment": "debt", "customer_id": shop["ali"], "lines": [{"item_id": shop["oil"]}]})
    c = tools.execute("count_drawer", {"iqd": 50000})
    assert c["expected"]["IQD"] == 50000 and c["difference"]["IQD"] == 0
    assert c["sales_cash"]["IQD"] == 50000 and c["sales_electronic"]["IQD"] == 4000 and c["credit_sales"]["IQD"] == 4000
    d = tools.execute("drawer_details", {})
    assert [x["what"] for x in d["in"]][1:] == ["بيع: طحين ×2"]


def test_add_item_rules(conn, tools):
    tools.source_message = "ضيف مادة رز، الكيس بـ 30"
    r = tools.execute("add_item", {"name": "رز", "price": 30000, "unit": "كيس"})
    assert r["ok"] and r["cost"] is None
    assert tools.execute("add_item", {"name": "رز", "price": 1})["error"] == "item_exists"
    # the model must not add an item the owner did not ask for
    tools.source_message = "بعت كيس سكر"
    tools.turn = 1
    assert tools.execute("find_item", {"query": "سكر"})["count"] == 0
    assert tools.execute("add_item", {"name": "سكر", "price": 20000})["error"] == "ask_first"
    tools.turn = 2  # the owner answered in the next message
    tools.source_message = "اي بـ 20"
    assert tools.execute("add_item", {"name": "سكر", "price": 20000})["ok"]


def test_update_item(conn, tools, shop):
    r = tools.execute("update_item", {"item_id": shop["flour"], "price": 26000})
    assert r["ok"] and r["before"]["price"] == 25000 and r["price"] == 26000
    assert tools.execute("update_item", {"item_id": shop["flour"]})["error"] == "nothing_to_change"
    # old sales keep their price
    assert tools.execute("record_sale", {"payment": "cash", "lines": [{"item_id": shop["flour"]}]})["total"] == 26000


def test_sales_come_back_with_the_conversation(conn, tools, shop):
    tools.session_id = "s1"
    tools.execute("record_sale", {"payment": "cash", "lines": [{"item_id": shop["soap"]}]})
    assert db.session_entries(conn, "s1") == [("sale", 1)]


def test_sale_screen_endpoints(conn, monkeypatch):
    from fastapi.testclient import TestClient

    from app import main as main_mod
    from app.agent import Agent

    monkeypatch.setattr(main_mod, "agent", Agent(conn, llm=object(), log_path=None))
    client = TestClient(main_mod.app)
    ali = db.add_customer(conn, "أبو علي")
    r = client.post("/items", json={"name": "طحين", "price": 25000, "cost": 22000, "unit": "كيس"}).json()
    flour = r["item"]["id"]
    assert client.post("/items", json={"name": "طحين", "price": 1}).status_code == 409
    assert client.patch(f"/items/{flour}", json={"price": 26000}).json()["item"]["price"] == 26000
    assert [c["name"] for c in client.get("/customers").json()["customers"]] == ["أبو علي"]
    s = client.post("/sales", json={"session_id": "s1", "payment": "debt", "customer_id": ali,
                                    "lines": [{"item_id": flour, "qty": 2}]}).json()
    assert s["ok"] and s["total"] == 52000 and "أبو علي" in s["reply"]
    assert db.get_balance(conn, ali)["IQD"] == 52000
    # it is in the conversation: the chat can undo it
    assert main_mod.agent.session("s1").tools.execute("undo_last", {})["undone"]["type"] == "sale"
    assert db.get_balance(conn, ali)["IQD"] == 0
    assert db.get_messages(conn, "s1")[0]["text"].startswith("(بعت من شاشة البيع")
    big = client.post("/sales", json={"session_id": "s1", "payment": "cash", "lines": [{"item_id": flour, "qty": 100}]})
    assert big.status_code == 409 and big.json()["error"] == "confirmation_required"
    assert client.post("/sales", json={"session_id": "s1", "payment": "debt",
                                       "lines": [{"item_id": flour}]}).status_code == 422
    today = client.get("/sales").json()
    assert today["today"]["totals"]["IQD"] == 0 and today["recent"] == []


def test_item_totals_count_only_that_item(conn, tools, shop):
    """Found by the mall eval: a sale of flour + oil counted the oil in "شكد بعنا طحين؟"."""
    tools.execute("record_sale", {"payment": "cash", "lines": [{"item_id": shop["flour"], "qty": 1},
                                                               {"item_id": shop["oil"], "qty": 5}]})
    r = tools.execute("item_sales", {"period": "month", "item_id": shop["flour"]})
    assert r["items"][0]["revenue"] == 25000
    assert r["by_payment"]["cash"]["IQD"] == 25000 and r["totals"]["IQD"] == 25000  # not 45,000


def test_last_month_and_yesterday_periods(conn, tools, shop):
    """Found by the mall eval: "الشهر الماضي" was answered with this month."""
    old = db.add_sale(conn, "cash", [{"item_id": shop["flour"], "qty": 2, "unit_price": 25000, "unit_cost": 22000}])
    conn.execute("UPDATE sales SET created_at = datetime('now','localtime','start of month','-10 days') WHERE id = ?", (old,))
    y = db.add_sale(conn, "cash", [{"item_id": shop["flour"], "qty": 1, "unit_price": 25000, "unit_cost": 22000}])
    conn.execute("UPDATE sales SET created_at = datetime('now','localtime','-1 day') WHERE id = ?", (y,))
    conn.commit()
    assert tools.execute("item_sales", {"period": "last_month", "item_id": shop["flour"]})["items"][0]["revenue"] == 50000
    assert tools.execute("item_sales", {"period": "yesterday"})["totals"]["IQD"] == 25000
    w = db.add_withdrawal(conn, 9000, "IQD", "نقل")
    conn.execute("UPDATE withdrawals SET created_at = datetime('now','localtime','start of month','-3 days') WHERE id = ?", (w,))
    conn.commit()
    assert tools.execute("list_withdrawals", {"period": "last_month"})["totals"]["IQD"] == 9000
