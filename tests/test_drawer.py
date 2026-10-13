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
        ("چان بالجرار (الفكة)", 50000), ("تسديد من أبو علي", 20000), ("زيادة بالجرار: بيع ما انكتب", 1010000)]
    assert [(x["what"], x["amount"]) for x in d["out"]] == [("دفع لـ شركة الزين", 200000), ("مولدة", 30000)]
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


def test_every_dinar_in_and_out(conn, tools):
    """Hussein's example: وارد محمد 25، صادر 35 راتب عامل. Each line by who / what; the count
    then shows what was not written down (or what is missing)."""
    ali, zain = _parties(conn)
    tools.execute("set_drawer_opening", {"iqd": 50000})
    r = tools.execute("record_cash_in", {"amount": 25000, "currency": "IQD", "label": "محمد"})
    assert r["ok"] and r["cash_ins_since_last_count"]["IQD"] == 25000
    tools.execute("record_withdrawal", {"amount": 35000, "currency": "IQD", "reason": "راتب عامل"})
    tools.execute("record_payment", {"customer_id": ali, "amount": 10000, "currency": "IQD"})
    tools.execute("record_cash_in", {"amount": 40000, "currency": "IQD", "label": "كارت شحن"})
    now = tools.execute("drawer_details", {"which": "since_last_count"})
    # in time order (same second here, so only the set is checked; the start comes first)
    assert now["in"][0]["what"] == "چان بالجرار (الفكة)"
    assert sorted(x["what"] for x in now["in"][1:]) == sorted(["محمد", "تسديد من أبو علي", "كارت شحن"])
    assert [x["what"] for x in now["out"]] == ["راتب عامل"]
    assert now["totals"]["IQD"] == {"in": 125000, "out": 35000, "left": 90000}  # what should be in it
    # counted 100,000: 10,000 more than written down
    c = tools.execute("count_drawer", {"iqd": 100000})
    assert c["expected"]["IQD"] == 90000 and c["difference"]["IQD"] == 10000
    assert c["cash_sales"]["IQD"] == 75000 and not c["shortage"]  # 25 + 40 written + 10 not
    d = tools.execute("drawer_details", {})
    assert d["in"][-1]["what"].startswith("زيادة") and d["totals"]["IQD"]["left"] == 100000


def test_short_drawer_with_cash_ins(conn, tools):
    tools.execute("set_drawer_opening", {"iqd": 0})
    tools.execute("record_cash_in", {"amount": 30000, "currency": "IQD", "label": "محمد"})
    c = tools.execute("count_drawer", {"iqd": 25000})
    assert c["difference"]["IQD"] == -5000 and c["shortage"] and "ناقص 5,000" in c["meaning"]
    d = tools.execute("drawer_details", {})
    assert d["out"][-1]["what"].startswith("نقص") and d["totals"]["IQD"]["left"] == 25000


def test_cash_in_rules_and_undo(conn, tools):
    assert tools.execute("record_cash_in", {"amount": 5000, "currency": "IQD", "label": " "})["error"] == "label_required"
    assert tools.execute("record_cash_in", {"amount": 0, "currency": "IQD", "label": "x"})["error"] == "invalid_amount"
    big = tools.execute("record_cash_in", {"amount": 5_000_000, "currency": "IQD", "label": "x"})
    assert big["error"] == "confirmation_required"
    tools.execute("record_cash_in", {"amount": 5000, "currency": "IQD", "label": "محمد"})
    u = tools.execute("undo_last", {})
    assert u["ok"] and u["undone"]["type"] == "cash_in" and u["undone"]["label"] == "محمد"
    assert db.drawer_report(conn)["cash_ins"]["IQD"] == 0
    # no customer was created or touched
    assert conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 0


def test_cash_ins_survive_an_old_count(conn, tools):
    """A count made before this stage has ci_upto 0: later cash ins still fall after it."""
    tools.execute("set_drawer_opening", {"iqd": 0})
    conn.execute("UPDATE drawer_counts SET ci_upto = 0"); conn.commit()
    tools.execute("record_cash_in", {"amount": 7000, "currency": "IQD", "label": "بيع"})
    assert tools.execute("count_drawer", {"iqd": 7000})["difference"]["IQD"] == 0


def test_drawer_shows_todays_money_before_any_count(conn, tools):
    """Hussein: "ابو علي سدد 45" must show in the drawer even if it was never counted."""
    ali, zain = _parties(conn)
    old = db.add_transaction(conn, ali, "payment", 99000)
    conn.execute("UPDATE transactions SET created_at = datetime('now','localtime','-1 day') WHERE id = ?", (old,))
    conn.commit()
    r = tools.execute("record_payment", {"customer_id": ali, "amount": 45000, "currency": "IQD"})
    assert r["drawer"] == "دخلت للجرار"
    tools.execute("record_withdrawal", {"amount": 5000, "currency": "IQD", "reason": "أكل"})
    d = db.drawer_details(conn)
    assert d["since"] == "today" and d["start"] is None
    assert [(x["what"], x["amount"]) for x in d["in"]] == [("تسديد من أبو علي", 45000)]  # not yesterday's 99,000
    assert [(x["what"], x["amount"]) for x in d["out"]] == [("أكل", 5000)]
    assert db.drawer_report(conn)["customer_payments"]["IQD"] == 45000
