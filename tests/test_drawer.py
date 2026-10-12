"""Stage 15: counting the cash drawer at closing and working out the day's cash sales."""

from app import db


def _parties(conn):
    ali = db.add_customer(conn, "أبو علي")
    zain = db.add_customer(conn, "شركة الزين", kind="supplier")
    return ali, zain


def test_plan_example(conn, tools):
    """PLAN_v3_drawer.md: start 50k; Ali paid 20k; paid Zain 200k; 30k for the generator;
    75k new debt; counted 850k -> cash sales 1,010,000 and total 1,085,000."""
    ali, zain = _parties(conn)
    assert tools.execute("set_drawer_opening", {"iqd": 50000})["ok"]
    db.add_transaction(conn, ali, "payment", 20000)
    db.add_transaction(conn, zain, "payment", 200000)
    db.add_withdrawal(conn, 30000, "IQD", "مولدة")
    db.add_transaction(conn, ali, "debt", 75000)
    db.add_transaction(conn, zain, "debt", 500000)  # buying on credit: no cash moved
    r = tools.execute("count_drawer", {"iqd": 850000})
    assert r["ok"] and r["counted"]["iqd"] == 850000 and r["start"]["iqd"] == 50000
    assert r["customer_payments"]["IQD"] == 20000 and r["supplier_payments"]["IQD"] == 200000
    assert r["withdrawals"]["IQD"] == 30000 and r["credit_sales"]["IQD"] == 75000
    assert r["cash_sales"] == {"IQD": 1010000} and r["total_sales"] == {"IQD": 1085000}
    assert not r["shortage"] and "1,010,000" in r["meaning"]


def test_first_count_asks_for_the_opening(conn, tools):
    r = tools.execute("count_drawer", {"iqd": 300000})
    assert not r["ok"] and r["error"] == "opening_required"
    assert conn.execute("SELECT COUNT(*) FROM drawer_counts").fetchone()[0] == 0


def test_next_day_starts_from_the_last_count(conn, tools):
    ali, _ = _parties(conn)
    tools.execute("set_drawer_opening", {"iqd": 0})
    tools.execute("count_drawer", {"iqd": 100000})
    db.add_transaction(conn, ali, "payment", 10000)
    db.add_withdrawal(conn, 40000, "IQD", "البيت")  # took money home after counting
    r = tools.execute("count_drawer", {"iqd": 200000})
    # 200 - 100 - 10 + 40 = 130
    assert r["start"]["iqd"] == 100000 and r["cash_sales"]["IQD"] == 130000


def test_not_from_the_drawer(conn, tools):
    ali, zain = _parties(conn)
    tools.execute("set_drawer_opening", {"iqd": 0})
    assert tools.execute("record_supplier_payment", {"supplier_id": zain, "amount": 100000, "currency": "IQD",
                                                     "from_drawer": False})["ok"]
    tools.execute("record_withdrawal", {"amount": 5000, "currency": "IQD", "reason": "شخصي", "from_drawer": False})
    tools.execute("record_payment", {"customer_id": ali, "amount": 7000, "currency": "IQD", "from_drawer": False})
    r = tools.execute("count_drawer", {"iqd": 50000})
    assert r["supplier_payments"]["IQD"] == 0 and r["withdrawals"]["IQD"] == 0
    assert r["customer_payments"]["IQD"] == 0 and r["not_from_drawer"]["IQD"] == 112000
    assert r["cash_sales"]["IQD"] == 50000
    # the supplier's balance still went down
    assert db.get_balance(conn, zain)["IQD"] == -100000


def test_shortage_is_flagged(conn, tools):
    ali, _ = _parties(conn)
    tools.execute("set_drawer_opening", {"iqd": 100000})
    db.add_transaction(conn, ali, "payment", 50000)
    r = tools.execute("count_drawer", {"iqd": 120000})  # 120 - 100 - 50 = -30
    assert r["cash_sales"]["IQD"] == -30000 and r["shortage"] and "ناقص" in r["meaning"]


def test_currencies_apart_and_uncounted_dollars(conn, tools):
    ali, _ = _parties(conn)
    tools.execute("set_drawer_opening", {"iqd": 0, "usd": 100})
    db.add_transaction(conn, ali, "payment", 20, "USD")
    r = tools.execute("count_drawer", {"iqd": 30000, "usd": 150})
    assert r["cash_sales"] == {"IQD": 30000, "USD": 30}
    r2 = tools.execute("count_drawer", {"iqd": 40000})  # dollars not counted this time
    assert r2["cash_sales"] == {"IQD": 10000}


def test_imported_and_undone_rows_are_not_todays_money(conn, tools):
    ali, _ = _parties(conn)
    tools.execute("set_drawer_opening", {"iqd": 0})
    db.import_batch(conn, [{"customer_id": ali, "type": "payment", "amount": 90000, "currency": "IQD"}],
                    "b1", "file:x")
    gone = db.add_transaction(conn, ali, "payment", 5000)
    db.mark_undone(conn, gone)
    r = tools.execute("count_drawer", {"iqd": 10000})
    assert r["customer_payments"]["IQD"] == 0 and r["cash_sales"]["IQD"] == 10000


def test_undo_a_count(conn, tools):
    tools.execute("set_drawer_opening", {"iqd": 0})
    tools.execute("count_drawer", {"iqd": 999000})
    r = tools.execute("undo_last", {})
    assert r["ok"] and r["undone"]["ledger"] == "drawer"
    assert db.last_drawer_count(conn)["kind"] == "open"
    assert tools.execute("count_drawer", {"iqd": 10000})["cash_sales"]["IQD"] == 10000


def test_count_needs_an_amount(tools):
    assert tools.execute("count_drawer", {})["error"] == "amount_required"
    assert tools.execute("count_drawer", {"iqd": -5})["error"] == "invalid_amount"
    assert tools.execute("set_drawer_opening", {"iqd": "كثير"})["error"] == "invalid_amount"


def test_status_and_history(conn, tools):
    ali, _ = _parties(conn)
    assert "note" in tools.execute("drawer_status", {})
    tools.execute("set_drawer_opening", {"iqd": 0})
    tools.execute("count_drawer", {"iqd": 50000})
    db.add_transaction(conn, ali, "payment", 15000)
    s = tools.execute("drawer_status", {})
    assert s["since_last_count"]["customer_payments"]["IQD"] == 15000 and "cash_sales" not in s["since_last_count"]
    assert s["last_close"]["cash_sales"]["IQD"] == 50000
    tools.execute("count_drawer", {"iqd": 80000})
    h = tools.execute("drawer_history", {"limit": 5})["closes"]
    assert [c["cash_sales"]["IQD"] for c in h] == [15000, 50000]


def test_drawer_counts_come_back_with_the_conversation(conn, tools):
    tools.session_id = "s1"
    tools.execute("set_drawer_opening", {"iqd": 0})
    tools.execute("count_drawer", {"iqd": 1000})
    assert db.session_entries(conn, "s1")[-1][0] == "drawer"


def test_drawer_endpoint_and_excel(conn, tools, monkeypatch):
    import io

    from fastapi.testclient import TestClient
    from openpyxl import load_workbook

    from app import main as main_mod
    from app.agent import Agent

    monkeypatch.setattr(main_mod, "agent", Agent(conn, llm=object(), log_path=None))
    client = TestClient(main_mod.app)
    assert client.get("/drawer").json()["closes"] == []
    tools.execute("set_drawer_opening", {"iqd": 50000})
    tools.execute("count_drawer", {"iqd": 80000})
    closes = client.get("/drawer").json()["closes"]
    assert len(closes) == 1 and closes[0]["cash_sales"]["IQD"] == 30000
    wb = load_workbook(io.BytesIO(client.get("/export.xlsx").content))
    assert list(wb["الجرار"].values)[1][1:3] == (80000, 50000)


def test_details_in_out_and_left(conn, tools):
    """The owner's own sheet: in (start, payments, cash sales), out (suppliers, withdrawals),
    and in - out = what was counted."""
    ali, zain = _parties(conn)
    tools.execute("set_drawer_opening", {"iqd": 50000})
    db.add_transaction(conn, ali, "payment", 20000)
    db.add_transaction(conn, zain, "payment", 200000)
    db.add_withdrawal(conn, 30000, "IQD", "مولدة")
    db.add_withdrawal(conn, 9000, "IQD", "شخصي", from_drawer=False)  # not from the drawer: not listed
    so_far = tools.execute("drawer_details", {"which": "since_last_count"})
    assert not so_far["complete"] and so_far["totals"]["IQD"] == {"in": 70000, "out": 230000, "left": -160000}
    tools.execute("count_drawer", {"iqd": 850000})
    d = tools.execute("drawer_details", {})
    assert d["complete"]
    assert [(x["what"], x["amount"]) for x in d["in"]] == [
        ("چان بالجرار (الفكة)", 50000), ("تسديد من أبو علي", 20000), ("البيع النقدي (محسوب من العدّة)", 1010000)]
    assert [(x["what"], x["amount"]) for x in d["out"]] == [("دفع لـ شركة الزين", 200000), ("سحب: مولدة", 30000)]
    assert d["totals"]["IQD"] == {"in": 1080000, "out": 230000, "left": 850000}  # = what was counted


def test_details_endpoint(conn, tools, monkeypatch):
    from fastapi.testclient import TestClient

    from app import main as main_mod
    from app.agent import Agent

    monkeypatch.setattr(main_mod, "agent", Agent(conn, llm=object(), log_path=None))
    client = TestClient(main_mod.app)
    tools.execute("set_drawer_opening", {"iqd": 1000})
    r = tools.execute("count_drawer", {"iqd": 5000})
    d = client.get(f"/drawer/details?count_id={r['count_id']}").json()
    assert d["totals"]["IQD"]["left"] == 5000
    assert client.get("/drawer/details").json()["complete"] is False
    assert client.get("/drawer/details?count_id=999").status_code == 404
