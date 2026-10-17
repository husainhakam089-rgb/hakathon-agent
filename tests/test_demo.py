from app import db, demo


def test_demo_fills_only_an_empty_notebook(conn):
    assert demo.seed_if_empty(conn)
    assert not demo.seed_if_empty(conn)  # a second start adds nothing
    balances = db.all_balances(conn)
    assert len(balances) == len(demo.CUSTOMERS)
    assert all(b["balances"]["IQD"] >= 0 for b in balances)  # nobody paid more than they owed
    assert conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0] > 100
    assert db.overdue_debts(conn)  # something for the alerts to show


def test_demo_never_touches_a_notebook_in_use(conn):
    cid = db.add_customer(conn, "زبون حقيقي")
    db.add_transaction(conn, cid, "debt", 5000)
    assert not demo.seed_if_empty(conn)
    assert [c["name"] for c in db.all_customers(conn)] == ["زبون حقيقي"]
    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
