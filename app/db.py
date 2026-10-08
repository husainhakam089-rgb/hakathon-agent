"""SQLite storage: schema and queries. All amounts are integers."""

import os
import sqlite3
from pathlib import Path

from app.arabic import find_matches, normalize

CURRENCIES = ("IQD", "USD")

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "debt.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  name            TEXT NOT NULL,
  normalized_name TEXT NOT NULL,
  phone           TEXT,
  created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS transactions (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  customer_id     INTEGER NOT NULL REFERENCES customers(id),
  type            TEXT NOT NULL CHECK (type IN ('debt','payment')),
  amount          INTEGER NOT NULL CHECK (amount > 0),
  currency        TEXT NOT NULL DEFAULT 'IQD' CHECK (currency IN ('IQD','USD')),
  note            TEXT,
  source_message  TEXT,
  undone          INTEGER NOT NULL DEFAULT 0,
  created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_tx_customer ON transactions(customer_id);
"""


def connect(path=None) -> sqlite3.Connection:
    """Open (and initialize) the database. Use ':memory:' for tests."""
    path = path or os.getenv("DB_PATH") or DEFAULT_DB_PATH
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


# ---------- customers ----------

def add_customer(conn, name: str, phone: str | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO customers (name, normalized_name, phone) VALUES (?, ?, ?)",
        (name.strip(), normalize(name), phone),
    )
    conn.commit()
    return cur.lastrowid


def get_customer(conn, customer_id: int):
    row = conn.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
    return dict(row) if row else None


def all_customers(conn) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM customers ORDER BY id")]


def find_customers(conn, query: str) -> list[dict]:
    return find_matches(query, all_customers(conn))


# ---------- transactions ----------

def add_transaction(conn, customer_id, tx_type, amount, currency="IQD",
                    note=None, source_message=None) -> int:
    cur = conn.execute(
        """INSERT INTO transactions (customer_id, type, amount, currency, note, source_message)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (customer_id, tx_type, amount, currency, note, source_message),
    )
    conn.commit()
    return cur.lastrowid


def get_transaction(conn, tx_id: int):
    row = conn.execute("SELECT * FROM transactions WHERE id = ?", (tx_id,)).fetchone()
    return dict(row) if row else None


def mark_undone(conn, tx_id: int) -> None:
    conn.execute("UPDATE transactions SET undone = 1 WHERE id = ?", (tx_id,))
    conn.commit()


def get_history(conn, customer_id: int, limit: int = 10) -> list[dict]:
    rows = conn.execute(
        """SELECT id, type, amount, currency, note, undone, created_at
           FROM transactions WHERE customer_id = ?
           ORDER BY id DESC LIMIT ?""",
        (customer_id, limit),
    )
    return [dict(r) for r in rows]


# ---------- balances ----------

_BALANCE_SQL = """
SELECT customer_id, currency,
       SUM(CASE WHEN type = 'debt' THEN amount ELSE -amount END) AS balance,
       MAX(created_at) AS last_activity
FROM transactions
WHERE undone = 0 {where}
GROUP BY customer_id, currency
"""


def get_balance(conn, customer_id: int) -> dict:
    """Balance per currency: debts minus payments, ignoring undone entries."""
    balances = {c: 0 for c in CURRENCIES}
    for r in conn.execute(_BALANCE_SQL.format(where="AND customer_id = ?"), (customer_id,)):
        balances[r["currency"]] = r["balance"]
    return balances


def all_balances(conn) -> list[dict]:
    """Every customer with their per-currency balance and last activity time."""
    customers = {
        c["id"]: {"id": c["id"], "name": c["name"], "balances": {k: 0 for k in CURRENCIES},
                  "last_activity": None}
        for c in all_customers(conn)
    }
    for r in conn.execute(_BALANCE_SQL.format(where="")):
        entry = customers[r["customer_id"]]
        entry["balances"][r["currency"]] = r["balance"]
        if entry["last_activity"] is None or r["last_activity"] > entry["last_activity"]:
            entry["last_activity"] = r["last_activity"]
    return list(customers.values())
