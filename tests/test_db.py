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


# ---------- v1: batches and migration ----------

import sqlite3

import pytest


def test_old_database_is_migrated(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE customers (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
          normalized_name TEXT NOT NULL, phone TEXT,
          created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')));
        CREATE TABLE transactions (id INTEGER PRIMARY KEY AUTOINCREMENT,
          customer_id INTEGER NOT NULL REFERENCES customers(id),
          type TEXT NOT NULL CHECK (type IN ('debt','payment')),
          amount INTEGER NOT NULL CHECK (amount > 0),
          currency TEXT NOT NULL DEFAULT 'IQD' CHECK (currency IN ('IQD','USD')),
          note TEXT, source_message TEXT, undone INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')));
        INSERT INTO customers (name, normalized_name) VALUES ('أبو علي', 'ابو علي');
        INSERT INTO transactions (customer_id, type, amount) VALUES (1, 'debt', 25000);
    """)
    old.commit()
    old.close()

    conn = db.connect(path)
    row = conn.execute("SELECT batch_id, source FROM transactions").fetchone()
    assert (row["batch_id"], row["source"]) == (None, "chat")
    assert db.get_balance(conn, 1)["IQD"] == 25000
    db.connect(path).close()  # second connect: nothing to migrate, no error


def test_import_batch_and_undo(conn):
    cid = db.add_customer(conn, "أبو علي")
    db.add_transaction(conn, cid, "debt", 1000)  # before the batch, stays
    ids = db.import_batch(conn, [
        {"customer_id": cid, "type": "debt", "amount": 25000, "currency": "IQD"},
        {"customer_id": None, "new_customer": "سعد", "type": "debt", "amount": 5000, "currency": "IQD"},
        {"customer_id": None, "new_customer": "سعد", "type": "payment", "amount": 2000, "currency": "IQD"},
    ], batch_id="b1", source="image:f1", note="رصيد منقول من الدفتر")
    assert len(ids) == 3
    saad = [c for c in db.all_customers(conn) if c["name"] == "سعد"]
    assert len(saad) == 1  # created once
    assert db.get_balance(conn, saad[0]["id"])["IQD"] == 3000
    row = conn.execute("SELECT source, note FROM transactions WHERE id = ?", (ids[0],)).fetchone()
    assert tuple(row) == ("image:f1", "رصيد منقول من الدفتر")

    assert db.last_batch_id(conn) == "b1"
    assert len(db.undo_batch(conn, "b1")) == 3
    assert db.get_balance(conn, cid)["IQD"] == 1000
    assert db.last_batch_id(conn) is None
    assert db.undo_batch(conn, "b1") == []


def test_import_batch_is_all_or_nothing(conn):
    cid = db.add_customer(conn, "أبو علي")
    with pytest.raises(sqlite3.IntegrityError):
        db.import_batch(conn, [
            {"customer_id": None, "new_customer": "سعد", "type": "debt", "amount": 5000, "currency": "IQD"},
            {"customer_id": cid, "type": "debt", "amount": -5, "currency": "IQD"},  # CHECK fails
        ], batch_id="b2", source="image:x")
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
    assert [c["name"] for c in db.all_customers(conn)] == ["أبو علي"]  # سعد rolled back too
