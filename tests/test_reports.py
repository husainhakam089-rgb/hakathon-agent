"""Stage 18: profit, week summary, cash forecast, customer statement, amps x price. All by code."""

from datetime import date, timedelta

import pytest

from app import db, reports


def _at(conn, table, row_id, days_ago):
    conn.execute(f"UPDATE {table} SET created_at = datetime('now','localtime','-{days_ago} days') WHERE id = ?", (row_id,))
    conn.commit()


@pytest.fixture
def shop(conn):
    flour = db.add_item(conn, "طحين", 25000, 22000, "كيس")
    soap = db.add_item(conn, "صابون", 1000, None, "قطعة")  # cost unknown
    ali = db.add_customer(conn, "أبو علي")
    return {"flour": flour, "soap": soap, "ali": ali}


def _sale(conn, item, qty, payment="cash", price=None, cost="item", customer=None):
    it = db.get_item(conn, item)
    return db.add_sale(conn, payment, [{"item_id": item, "qty": qty, "unit_price": price or it["price"],
                                        "unit_cost": it["cost"] if cost == "item" else cost}], customer_id=customer)


# ---------- profit ----------

def test_profit_worked_by_hand(conn, shop):
    _sale(conn, shop["flour"], 4)                       # 100,000 sold, cost 88,000
    _sale(conn, shop["flour"], 2, payment="debt", customer=shop["ali"])  # 50,000 / 44,000
    _sale(conn, shop["soap"], 10)                       # 10,000 with no cost
    db.add_withdrawal(conn, 5000, "IQD", "أكل")
    db.add_withdrawal(conn, 3000, "IQD", "أكل")
    db.add_cash_in(conn, 7000, "IQD", "محمد")
    p = reports.profit(conn, "month")
    assert p["sales"]["IQD"] == 160000 and p["cost_of_goods"]["IQD"] == 132000
    assert p["sales_without_cost"]["IQD"] == 10000
    assert p["gross_profit"]["IQD"] == 160000 - 10000 - 132000 == 18000
    assert p["expenses"]["IQD"] == 8000 and p["net_profit"]["IQD"] == 10000
    assert p["expenses_by_reason"] == [{"reason": "أكل", "amounts": {"IQD": 8000, "USD": 0}}]
    assert p["cash_in_without_items"]["IQD"] == 7000
    assert p["based_on"] == {"sales": 3, "withdrawals": 2, "cash_ins": 1}
    assert len(p["notes"]) == 2


def test_profit_periods_and_undone(conn, shop):
    old = _sale(conn, shop["flour"], 1)
    _at(conn, "sales", old, 40)
    gone = _sale(conn, shop["flour"], 1)
    db.mark_sale_undone(conn, gone)
    _sale(conn, shop["flour"], 1)
    assert reports.profit(conn, "today")["sales"]["IQD"] == 25000
    assert reports.profit(conn, "all")["sales"]["IQD"] == 50000
    assert reports.profit(conn, "today")["gross_profit"]["IQD"] == 3000


def test_period_ranges():
    d = date(2026, 3, 15)
    assert reports.period_range("today", d) == ("2026-03-15", "2026-03-16")
    assert reports.period_range("week", d) == ("2026-03-09", "2026-03-16")
    assert reports.period_range("month", d) == ("2026-03-01", "2026-03-16")
    assert reports.period_range("last_month", d) == ("2026-02-01", "2026-03-01")
    assert reports.period_range("yesterday", d) == ("2026-03-14", "2026-03-15")


# ---------- week ----------

def test_week_summary(conn, shop):
    _sale(conn, shop["flour"], 2)
    _sale(conn, shop["soap"], 3, payment="electronic")
    db.add_transaction(conn, shop["ali"], "debt", 30000)
    db.add_transaction(conn, shop["ali"], "payment", 10000)
    w = reports.week_summary(conn)
    assert w["sales_by_payment"]["cash"]["IQD"] == 50000 and w["sales_by_payment"]["electronic"]["IQD"] == 3000
    assert w["customer_payments"]["IQD"] == 10000 and w["new_customer_debts"]["IQD"] == 30000
    assert w["top_items"][0]["name"] == "طحين" and w["best_day"]["sales"] == 53000


# ---------- forecast ----------

def test_forecast_needs_a_week(conn, shop):
    _sale(conn, shop["flour"], 1)
    assert not reports.cash_forecast(conn)["ok"]  # only today, not a full day yet


def test_forecast_worked_by_hand(conn, shop):
    # 14 full days: every day 50,000 cash sales and 10,000 out for food; one rent paid (fixed: not averaged)
    for day in range(1, 15):
        _at(conn, "sales", _sale(conn, shop["flour"], 2), day)
        _at(conn, "withdrawals", db.add_withdrawal(conn, 10000, "IQD", "أكل"), day)
    _at(conn, "withdrawals", db.add_withdrawal(conn, 250000, "IQD", "إيجار", note="صرفية ثابتة"), 5)
    db.add_recurring(conn, "إيجار", 250000, "IQD", 1)
    f = reports.cash_forecast(conn, days=30, weeks=4)
    assert f["ok"] and f["based_on_days"] == 14
    assert f["per_day_in"]["IQD"] == 50000 and f["per_day_out"]["IQD"] == 10000
    assert f["expected_in"]["IQD"] == 1_500_000
    # the rent (day 1) falls due once in the next 30 days, twice if today is the 1st of a short month
    rents = [x for x in f["fixed_expenses"] if x["reason"] == "إيجار"]
    assert len(rents) in (1, 2) and f["expected_out"]["IQD"] == 300_000 + 250_000 * len(rents)
    assert f["expected_net"]["IQD"] == 1_500_000 - f["expected_out"]["IQD"]
    assert "المواسم" in f["caveat"]


# ---------- statement ----------

def test_customer_statement(conn, shop):
    ali = shop["ali"]
    old = db.add_transaction(conn, ali, "debt", 100000)
    _at(conn, "transactions", old, 20)
    db.add_transaction(conn, ali, "debt", 30000)
    db.add_transaction(conn, ali, "payment", 45000)
    gone = db.add_transaction(conn, ali, "debt", 999)
    db.mark_undone(conn, gone)
    _sale(conn, shop["flour"], 1, payment="debt", customer=ali)
    other = db.add_customer(conn, "أم حسين")
    db.add_transaction(conn, other, "debt", 5000)
    st = reports.customer_statement(conn, ali, "week")
    assert st["opening"]["IQD"] == 100000
    assert [(l["type"], l["amount"], l["balance"]) for l in st["lines"]] == [
        ("debt", 30000, 130000), ("payment", 45000, 85000), ("debt", 25000, 110000)]
    assert st["closing"]["IQD"] == 110000 == db.get_balance(conn, ali)["IQD"]
    assert st["lines"][2]["note"].startswith("بيع:") and st["based_on"]["entries"] == 3


# ---------- tools ----------

def test_amps_times_price_by_code(conn, tools):
    r = tools.execute("record_withdrawal", {"currency": "IQD", "reason": "مولدة", "quantity": 30, "unit_price": 12000,
                                            "unit": "أمبير"})
    assert r["ok"] and r["amount"] == 360000 and "30 أمبير × 12,000" in r["computed_from"]
    bad = tools.execute("record_withdrawal", {"amount": 350000, "currency": "IQD", "reason": "مولدة",
                                              "quantity": 30, "unit_price": 12000})
    assert bad["error"] == "amount_mismatch"  # the model's own multiplication is not trusted
    assert tools.execute("record_withdrawal", {"currency": "IQD", "reason": "مولدة"})["error"] == "invalid_amount"


def test_report_tools(conn, tools, shop):
    _sale(conn, shop["flour"], 2)
    assert tools.execute("profit_report", {"period": "today"})["net_profit"]["IQD"] == 6000
    assert tools.execute("week_summary", {})["ok"]
    assert tools.execute("cash_forecast", {})["error"] == "not_enough_data"
    st = tools.execute("customer_statement", {"customer_id": shop["ali"], "period": "all"})
    assert st["ok"] and st["page"] == f"/statement/{shop['ali']}?period=all"
    sup = db.add_customer(conn, "شركة الزين", kind="supplier")
    assert not tools.execute("customer_statement", {"customer_id": sup, "period": "all"})["ok"]


def test_report_endpoints(conn, shop, monkeypatch):
    from fastapi.testclient import TestClient

    from app import main as main_mod
    from app.agent import Agent

    monkeypatch.setattr(main_mod, "agent", Agent(conn, llm=object(), log_path=None))
    client = TestClient(main_mod.app)
    _sale(conn, shop["flour"], 1)
    db.add_transaction(conn, shop["ali"], "debt", 12345)
    r = client.get("/reports?period=today").json()
    assert r["profit"]["sales"]["IQD"] == 25000 and r["forecast"]["ok"] is False
    page = client.get(f"/statement/{shop['ali']}?period=all")
    assert page.status_code == 200 and "أبو علي" in page.text and "12,345" in page.text
    assert client.get("/statement/999").status_code == 404
    # the page shows one customer only
    other = db.add_customer(conn, "أم حسين")
    db.add_transaction(conn, other, "debt", 777)
    assert "777" not in client.get(f"/statement/{shop['ali']}?period=all").text


def test_money_taken_home_is_not_an_expense(conn, shop):
    _sale(conn, shop["flour"], 4)  # 12,000 profit from the sale
    db.add_withdrawal(conn, 2000, "IQD", "أكل")
    db.add_withdrawal(conn, 50000, "IQD", "البيت")
    db.add_withdrawal(conn, 30000, "IQD", "شخصي")
    db.add_withdrawal(conn, 10000, "IQD", "للبيت")
    p = reports.profit(conn, "today")
    assert p["expenses"]["IQD"] == 2000 and p["owner_draws"]["IQD"] == 90000
    assert p["net_profit"]["IQD"] == 10000
    assert [e["reason"] for e in p["expenses_by_reason"]] == ["أكل"]
    assert not reports.is_owner_draw(db.reason_key("مولدة")) and not reports.is_owner_draw(db.reason_key("بيتزا"))
