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
  created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  batch_id        TEXT,                    -- rows imported together (image / file)
  source          TEXT NOT NULL DEFAULT 'chat'  -- chat | image:<file_id> | file:<file_id> | voice
);

CREATE INDEX IF NOT EXISTS idx_tx_customer ON transactions(customer_id);
"""

# Columns added after v0: (name, definition) added to older databases on connect.
_MIGRATIONS = [
    ("batch_id", "TEXT"),
    ("source", "TEXT NOT NULL DEFAULT 'chat'"),
]


def connect(path=None) -> sqlite3.Connection:
    """Open (and initialize) the database. Use ':memory:' for tests."""
    path = path or os.getenv("DB_PATH") or DEFAULT_DB_PATH
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn) -> None:
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(transactions)")}
    for name, definition in _MIGRATIONS:
        if name not in columns:
            conn.execute(f"ALTER TABLE transactions ADD COLUMN {name} {definition}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tx_batch ON transactions(batch_id)")
    conn.commit()


# ---------- customers ----------

def add_customer(conn, name: str, phone: str | None = None, commit: bool = True) -> int:
    cur = conn.execute(
        "INSERT INTO customers (name, normalized_name, phone) VALUES (?, ?, ?)",
        (name.strip(), normalize(name), phone),
    )
    if commit:
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
                    note=None, source_message=None, batch_id=None, source="chat",
                    commit: bool = True) -> int:
    cur = conn.execute(
        """INSERT INTO transactions (customer_id, type, amount, currency, note, source_message,
                                     batch_id, source)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (customer_id, tx_type, amount, currency, note, source_message, batch_id, source),
    )
    if commit:
        conn.commit()
    return cur.lastrowid


def get_transaction(conn, tx_id: int):
    row = conn.execute("SELECT * FROM transactions WHERE id = ?", (tx_id,)).fetchone()
    return dict(row) if row else None


def mark_undone(conn, tx_id: int) -> None:
    conn.execute("UPDATE transactions SET undone = 1 WHERE id = ?", (tx_id,))
    conn.commit()


# ---------- batches (imports) ----------

def import_batch(conn, rows: list[dict], batch_id: str, source: str, note: str | None = None) -> list[int]:
    """Insert all rows in one database transaction: all of them or none.

    Each row: {"customer_id": int | None, "new_customer": str | None,
               "type": "debt"|"payment", "amount": int, "currency": "IQD"|"USD"}.
    A row with `new_customer` creates that customer (once per name in the batch).
    """
    created: dict[str, int] = {}
    tx_ids = []
    try:
        for row in rows:
            cid = row.get("customer_id")
            if cid is None:
                key = normalize(row["new_customer"])
                if key not in created:
                    created[key] = add_customer(conn, row["new_customer"], commit=False)
                cid = created[key]
            tx_ids.append(add_transaction(conn, cid, row["type"], row["amount"], row["currency"],
                                          note=note, batch_id=batch_id, source=source, commit=False))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return tx_ids


def last_batch_id(conn) -> str | None:
    """Most recent batch that still has rows not undone."""
    row = conn.execute(
        "SELECT batch_id FROM transactions WHERE batch_id IS NOT NULL AND undone = 0 "
        "ORDER BY id DESC LIMIT 1").fetchone()
    return row["batch_id"] if row else None


def undo_batch(conn, batch_id: str) -> list[dict]:
    """Mark every row of the batch undone. Returns the rows that were undone now."""
    rows = [dict(r) for r in conn.execute(
        "SELECT id, customer_id, type, amount, currency FROM transactions "
        "WHERE batch_id = ? AND undone = 0 ORDER BY id", (batch_id,))]
    conn.execute("UPDATE transactions SET undone = 1 WHERE batch_id = ?", (batch_id,))
    conn.commit()
    return rows


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
