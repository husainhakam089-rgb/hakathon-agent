import sqlite3

import pytest

from app import db


def test_add_and_get_customer(conn):
    cid = db.add_customer(conn, "أبو علي", "0770")
    c = db.get_customer(conn, cid)
    assert c["name"] == "أبو علي"
    assert c["normalized_name"] == "ابوعلي"
    assert c["phone"] == "0770"
    assert db.get_customer(conn, 999) is None


def test_balance_per_currency_ignores_undone(conn):
    cid = db.add_customer(conn, "أبو علي")
    db.add_transaction(conn, cid, "debt", 50000)
    db.add_transaction(conn, cid, "payment", 10000)
    db.add_transaction(conn, cid, "debt", 20, "USD")
    t = db.add_transaction(conn, cid, "debt", 99000)
    db.mark_undone(conn, t)
    assert db.get_balance(conn, cid) == {"IQD": 40000, "USD": 20}


def test_balance_empty(conn):
    cid = db.add_customer(conn, "سعد")
    assert db.get_balance(conn, cid) == {"IQD": 0, "USD": 0}


def test_undo_keeps_row(conn):
    cid = db.add_customer(conn, "سعد")
    t = db.add_transaction(conn, cid, "debt", 1000)
    db.mark_undone(conn, t)
    tx = db.get_transaction(conn, t)
    assert tx is not None and tx["undone"] == 1


@pytest.mark.parametrize("kwargs", [
    {"tx_type": "debt", "amount": 0},
    {"tx_type": "debt", "amount": -5},
    {"tx_type": "loan", "amount": 5},
    {"tx_type": "debt", "amount": 5, "currency": "EUR"},
])
def test_schema_constraints(conn, kwargs):
    cid = db.add_customer(conn, "سعد")
    with pytest.raises(sqlite3.IntegrityError):
        db.add_transaction(conn, cid, **kwargs)


def test_foreign_key(conn):
    with pytest.raises(sqlite3.IntegrityError):
        db.add_transaction(conn, 999, "debt", 1000)


def test_history_newest_first(conn):
    cid = db.add_customer(conn, "سعد")
    ids = [db.add_transaction(conn, cid, "debt", a) for a in (1000, 2000, 3000)]
    hist = db.get_history(conn, cid, limit=2)
    assert [h["id"] for h in hist] == [ids[2], ids[1]]


def test_find_customers(conn):
    db.add_customer(conn, "الحجي كريم")
    db.add_customer(conn, "أبو علي")
    assert [c["name"] for c in db.find_customers(conn, "حجي كريم")] == ["الحجي كريم"]
    assert [c["name"] for c in db.find_customers(conn, "ابوعلي")] == ["أبو علي"]


def test_file_db_persists(tmp_path):
    path = tmp_path / "t.db"
    c1 = db.connect(path)
    cid = db.add_customer(c1, "سعد")
    db.add_transaction(c1, cid, "debt", 5000)
    c1.close()
    c2 = db.connect(path)
    assert db.get_balance(c2, cid)["IQD"] == 5000
    c2.close()
