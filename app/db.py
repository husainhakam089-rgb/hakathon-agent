"""SQLite storage: schema and queries. All amounts are integers."""

import os
import sqlite3
from pathlib import Path

from app.arabic import find_matches, normalize

CURRENCIES = ("IQD", "USD")
# Who a party is. customer: owes the shop (receivable). supplier: a company, supplier or
# sales rep the shop owes (payable). Same tables, separate ledgers.
KINDS = ("customer", "supplier")

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "debt.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  name            TEXT NOT NULL,
  normalized_name TEXT NOT NULL,
  phone           TEXT,
  created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  kind            TEXT NOT NULL DEFAULT 'customer'  -- customer | supplier
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

-- Stage 11: money taken out of the shop's income (generator, rent, personal...).
-- No other party: nobody pays it back. Never deleted, only undone.
CREATE TABLE IF NOT EXISTS withdrawals (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  amount            INTEGER NOT NULL CHECK (amount > 0),
  currency          TEXT NOT NULL DEFAULT 'IQD' CHECK (currency IN ('IQD','USD')),
  reason            TEXT NOT NULL,          -- as the owner said it: مولدة، إيجار، شخصي...
  reason_key        TEXT NOT NULL,          -- normalized, to add up the same reason
  note              TEXT,
  source_message    TEXT,
  source            TEXT NOT NULL DEFAULT 'chat',
  undone            INTEGER NOT NULL DEFAULT 0,
  created_at        TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  session_id        TEXT
);

-- Stage 12: things the owner taught the agent ("الورقة = 10,000"، "أبو علي نفسه علي النجار").
-- Added to the agent's instructions every turn. Forgetting switches active off.
CREATE TABLE IF NOT EXISTS memories (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  text              TEXT NOT NULL,
  text_key          TEXT NOT NULL,
  source_message    TEXT,
  active            INTEGER NOT NULL DEFAULT 1,
  created_at        TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- Stage 13: saved conversations, so the owner can go back to an old one.
CREATE TABLE IF NOT EXISTS chat_sessions (
  id                TEXT PRIMARY KEY,
  title             TEXT NOT NULL,
  created_at        TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  updated_at        TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS chat_messages (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id        TEXT NOT NULL REFERENCES chat_sessions(id),
  role              TEXT NOT NULL CHECK (role IN ('user','assistant')),
  text              TEXT NOT NULL,
  tool_calls        TEXT,                    -- JSON, for "شنو سوّى الوكيل؟"
  source            TEXT NOT NULL DEFAULT 'chat',
  created_at        TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_msg_session ON chat_messages(session_id);

-- Stage 15: counting the cash drawer. 'close' = counted at closing; 'open' = the owner said
-- how much was in it at the start ("الفكة الصبح 50 ألف"). NULL = that currency not counted.
-- tx_upto / wd_upto: the last transaction / withdrawal id at that moment, so a day is
-- exactly the entries between two counts (not guessed from clock times).
CREATE TABLE IF NOT EXISTS drawer_counts (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  kind              TEXT NOT NULL CHECK (kind IN ('open','close')),
  iqd               INTEGER,
  usd               INTEGER,
  tx_upto           INTEGER NOT NULL,
  wd_upto           INTEGER NOT NULL,
  source_message    TEXT,
  session_id        TEXT,
  undone            INTEGER NOT NULL DEFAULT 0,
  created_at        TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
"""

# Columns added after v0: (name, definition) added to older databases on connect.
_MIGRATIONS = [
    ("batch_id", "TEXT"),
    ("source", "TEXT NOT NULL DEFAULT 'chat'"),
    ("session_id", "TEXT"),  # stage 13: the conversation it was recorded in (undo_last later)
    ("from_drawer", "INTEGER NOT NULL DEFAULT 1"),  # stage 15: 0 = paid by transfer / from the owner's pocket
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
    if "kind" not in {r["name"] for r in conn.execute("PRAGMA table_info(customers)")}:
        conn.execute("ALTER TABLE customers ADD COLUMN kind TEXT NOT NULL DEFAULT 'customer'")
    wd_columns = {r["name"] for r in conn.execute("PRAGMA table_info(withdrawals)")}
    if "session_id" not in wd_columns:
        conn.execute("ALTER TABLE withdrawals ADD COLUMN session_id TEXT")
    if "from_drawer" not in wd_columns:
        conn.execute("ALTER TABLE withdrawals ADD COLUMN from_drawer INTEGER NOT NULL DEFAULT 1")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tx_batch ON transactions(batch_id)")
    conn.commit()


# ---------- customers ----------

def add_customer(conn, name: str, phone: str | None = None, commit: bool = True,
                 kind: str = "customer") -> int:
    cur = conn.execute(
        "INSERT INTO customers (name, normalized_name, phone, kind) VALUES (?, ?, ?, ?)",
        (name.strip(), normalize(name), phone, kind),
    )
    if commit:
        conn.commit()
    return cur.lastrowid


def get_customer(conn, customer_id: int):
    row = conn.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
    return dict(row) if row else None


def all_customers(conn, kind: str = "customer") -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM customers WHERE kind = ? ORDER BY id", (kind,))]


def find_customers(conn, query: str, kind: str = "customer") -> list[dict]:
    return find_matches(query, all_customers(conn, kind))


# ---------- transactions ----------

def add_transaction(conn, customer_id, tx_type, amount, currency="IQD",
                    note=None, source_message=None, batch_id=None, source="chat",
                    commit: bool = True, session_id: str | None = None, from_drawer: bool = True) -> int:
    cur = conn.execute(
        """INSERT INTO transactions (customer_id, type, amount, currency, note, source_message,
                                     batch_id, source, session_id, from_drawer)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (customer_id, tx_type, amount, currency, note, source_message, batch_id, source, session_id,
         int(bool(from_drawer))),
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


# ---------- withdrawals (stage 11) ----------

PERIODS = ("today", "month", "all")
_PERIOD_SQL = {
    "today": "date(created_at) = date('now','localtime')",
    "month": "strftime('%Y-%m', created_at) = strftime('%Y-%m','now','localtime')",
    "all": "1 = 1",
}


def reason_key(reason: str) -> str:
    """Same reason, different spelling: "المولدة" = "مولده" = "مولدة"."""
    key = normalize(reason or "")
    if key.startswith("ال") and len(key) > 4:
        key = key[2:]
    return key


def add_withdrawal(conn, amount: int, currency: str, reason: str, note=None,
                   source_message=None, source="chat", session_id: str | None = None,
                   from_drawer: bool = True) -> int:
    cur = conn.execute(
        """INSERT INTO withdrawals (amount, currency, reason, reason_key, note, source_message, source,
                                    session_id, from_drawer)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (amount, currency, reason.strip(), reason_key(reason), note, source_message, source, session_id,
         int(bool(from_drawer))),
    )
    conn.commit()
    return cur.lastrowid


def get_withdrawal(conn, wd_id: int):
    row = conn.execute("SELECT * FROM withdrawals WHERE id = ?", (wd_id,)).fetchone()
    return dict(row) if row else None


def mark_withdrawal_undone(conn, wd_id: int) -> None:
    conn.execute("UPDATE withdrawals SET undone = 1 WHERE id = ?", (wd_id,))
    conn.commit()


def withdrawals_summary(conn, period: str = "month", reason: str | None = None, recent: int = 10) -> dict:
    """Totals per currency, per reason, and the latest entries, for a period."""
    where = _PERIOD_SQL[period if period in PERIODS else "month"] + " AND undone = 0"
    args: list = []
    if reason:
        where += " AND reason_key = ?"
        args.append(reason_key(reason))
    totals = {c: 0 for c in CURRENCIES}
    for r in conn.execute(f"SELECT currency, SUM(amount) s FROM withdrawals WHERE {where} GROUP BY currency", args):
        totals[r["currency"]] = r["s"]
    by_reason: dict[str, dict] = {}
    for r in conn.execute(
            f"""SELECT reason_key, currency, SUM(amount) s, MAX(created_at) last, MAX(reason) reason
                FROM withdrawals WHERE {where} GROUP BY reason_key, currency""", args):
        entry = by_reason.setdefault(r["reason_key"], {"reason": r["reason"], "totals": {c: 0 for c in CURRENCIES},
                                                       "last": r["last"]})
        entry["totals"][r["currency"]] = r["s"]
        entry["last"] = max(entry["last"], r["last"])
    reasons = sorted(by_reason.values(), key=lambda e: (e["totals"]["IQD"], e["totals"]["USD"]), reverse=True)
    latest = [dict(r) for r in conn.execute(
        f"""SELECT id, amount, currency, reason, created_at FROM withdrawals WHERE {where}
            ORDER BY id DESC LIMIT ?""", (*args, recent))]
    return {"period": period, "totals": totals, "by_reason": reasons, "latest": latest}


# ---------- memories (stage 12) ----------

def add_memory(conn, text: str, source_message: str | None = None) -> int:
    cur = conn.execute("INSERT INTO memories (text, text_key, source_message) VALUES (?, ?, ?)",
                       (text.strip(), normalize(text), source_message))
    conn.commit()
    return cur.lastrowid


def list_memories(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT id, text, created_at FROM memories WHERE active = 1 ORDER BY id")]


def find_memory(conn, text: str):
    row = conn.execute("SELECT id, text FROM memories WHERE active = 1 AND text_key = ?",
                       (normalize(text),)).fetchone()
    return dict(row) if row else None


def forget_memory(conn, memory_id: int) -> dict | None:
    row = conn.execute("SELECT id, text FROM memories WHERE id = ? AND active = 1", (memory_id,)).fetchone()
    if row is None:
        return None
    conn.execute("UPDATE memories SET active = 0 WHERE id = ?", (memory_id,))
    conn.commit()
    return dict(row)


# ---------- saved conversations (stage 13) ----------

TITLE_CHARS = 60


def save_turn(conn, session_id: str, user_text: str, reply: str, tool_calls: list | None = None,
              source: str = "chat") -> None:
    """One exchange: the owner's message and the agent's reply (+ what it did)."""
    import json

    title = " ".join(user_text.split())[:TITLE_CHARS] or "محادثة"
    conn.execute("INSERT OR IGNORE INTO chat_sessions (id, title) VALUES (?, ?)", (session_id, title))
    conn.execute("UPDATE chat_sessions SET updated_at = datetime('now','localtime') WHERE id = ?", (session_id,))
    conn.execute("INSERT INTO chat_messages (session_id, role, text, source) VALUES (?, 'user', ?, ?)",
                 (session_id, user_text, source))
    conn.execute("INSERT INTO chat_messages (session_id, role, text, tool_calls) VALUES (?, 'assistant', ?, ?)",
                 (session_id, reply, json.dumps(tool_calls or [], ensure_ascii=False)))
    conn.commit()


def list_sessions(conn, limit: int = 100) -> list[dict]:
    return [dict(r) for r in conn.execute(
        """SELECT s.id, s.title, s.created_at, s.updated_at, COUNT(m.id) AS messages
           FROM chat_sessions s LEFT JOIN chat_messages m ON m.session_id = s.id
           GROUP BY s.id ORDER BY s.updated_at DESC, s.rowid DESC LIMIT ?""", (limit,))]


def get_messages(conn, session_id: str, last: int | None = None) -> list[dict]:
    import json

    sql = "SELECT role, text, tool_calls, created_at FROM chat_messages WHERE session_id = ? ORDER BY id"
    rows = [dict(r) for r in conn.execute(sql, (session_id,))]
    if last is not None:
        rows = rows[-last:]
    for r in rows:
        r["tool_calls"] = json.loads(r["tool_calls"]) if r["tool_calls"] else []
    return rows


def session_entries(conn, session_id: str) -> list:
    """What this conversation recorded and is not undone yet, oldest first, in the form
    undo_last expects: a transaction id, ("withdrawal", id) or ("drawer", id)."""
    rows = conn.execute(
        """SELECT 'tx' AS kind, id, created_at FROM transactions WHERE session_id = ? AND undone = 0
           UNION ALL
           SELECT 'wd', id, created_at FROM withdrawals WHERE session_id = ? AND undone = 0
           UNION ALL
           SELECT 'dr', id, created_at FROM drawer_counts WHERE session_id = ? AND undone = 0
           ORDER BY created_at, kind, id""", (session_id, session_id, session_id))
    tags = {"wd": "withdrawal", "dr": "drawer"}
    return [r["id"] if r["kind"] == "tx" else (tags[r["kind"]], r["id"]) for r in rows]


# ---------- the cash drawer (stage 15) ----------

def _max_ids(conn) -> tuple[int, int]:
    tx = conn.execute("SELECT COALESCE(MAX(id), 0) FROM transactions").fetchone()[0]
    wd = conn.execute("SELECT COALESCE(MAX(id), 0) FROM withdrawals").fetchone()[0]
    return tx, wd


def _ids_before_today(conn) -> tuple[int, int]:
    tx = conn.execute("SELECT COALESCE(MAX(id), 0) FROM transactions "
                      "WHERE date(created_at) < date('now','localtime')").fetchone()[0]
    wd = conn.execute("SELECT COALESCE(MAX(id), 0) FROM withdrawals "
                      "WHERE date(created_at) < date('now','localtime')").fetchone()[0]
    return tx, wd


def last_drawer_count(conn, before_id: int | None = None):
    """The latest count (open or close) still in force, optionally before a given count."""
    sql = "SELECT * FROM drawer_counts WHERE undone = 0"
    args: tuple = ()
    if before_id is not None:
        sql += " AND id < ?"
        args = (before_id,)
    row = conn.execute(sql + " ORDER BY id DESC LIMIT 1", args).fetchone()
    return dict(row) if row else None


def add_drawer_count(conn, kind: str, iqd: int | None, usd: int | None, source_message=None,
                     session_id: str | None = None) -> int:
    """A close counts everything recorded up to now. An open ("الفكة الصبح") replaces what
    the last count said was in the drawer, from the same point; with no count before it,
    it is the start of today."""
    if kind == "close":
        tx_upto, wd_upto = _max_ids(conn)
    else:
        prev = last_drawer_count(conn)
        tx_upto, wd_upto = (prev["tx_upto"], prev["wd_upto"]) if prev else _ids_before_today(conn)
    cur = conn.execute(
        """INSERT INTO drawer_counts (kind, iqd, usd, tx_upto, wd_upto, source_message, session_id)
           VALUES (?, ?, ?, ?, ?, ?, ?)""", (kind, iqd, usd, tx_upto, wd_upto, source_message, session_id))
    conn.commit()
    return cur.lastrowid


def get_drawer_count(conn, count_id: int):
    row = conn.execute("SELECT * FROM drawer_counts WHERE id = ?", (count_id,)).fetchone()
    return dict(row) if row else None


def mark_drawer_count_undone(conn, count_id: int) -> None:
    conn.execute("UPDATE drawer_counts SET undone = 1 WHERE id = ?", (count_id,))
    conn.commit()


def drawer_movements(conn, tx_from: int, tx_to: int | None, wd_from: int, wd_to: int | None) -> dict:
    """Money in and out of the drawer between two counts, per currency. Imported rows (old
    notebook pages) and undone ones are not today's money."""
    zero = lambda: {c: 0 for c in CURRENCIES}  # noqa: E731
    out = {"customer_payments": zero(), "supplier_payments": zero(), "withdrawals": zero(),
           "credit_sales": zero(), "not_from_drawer": zero()}
    tx_to = tx_to if tx_to is not None else 1 << 62
    wd_to = wd_to if wd_to is not None else 1 << 62
    for r in conn.execute(
            """SELECT c.kind, t.type, t.currency, t.from_drawer, SUM(t.amount) s
               FROM transactions t JOIN customers c ON c.id = t.customer_id
               WHERE t.id > ? AND t.id <= ? AND t.undone = 0 AND t.batch_id IS NULL
               GROUP BY c.kind, t.type, t.currency, t.from_drawer""", (tx_from, tx_to)):
        cur, s = r["currency"], r["s"]
        if r["kind"] == "customer" and r["type"] == "debt":
            out["credit_sales"][cur] += s
        elif r["type"] == "payment" and not r["from_drawer"]:
            out["not_from_drawer"][cur] += s
        elif r["kind"] == "customer":
            out["customer_payments"][cur] += s
        elif r["kind"] == "supplier" and r["type"] == "payment":
            out["supplier_payments"][cur] += s
    for r in conn.execute(
            """SELECT currency, from_drawer, SUM(amount) s FROM withdrawals
               WHERE id > ? AND id <= ? AND undone = 0 GROUP BY currency, from_drawer""", (wd_from, wd_to)):
        key = "withdrawals" if r["from_drawer"] else "not_from_drawer"
        out[key][r["currency"]] += r["s"]
    return out


def drawer_report(conn, count: dict | None = None) -> dict:
    """Cash sales for the day that ends with `count` (a close), or so far since the last
    count (count=None: movements only, nothing to compare with yet).

    cash sales = in the drawer now - in it at the start - customer payments
                 + paid to suppliers + withdrawals            (each currency on its own)
    """
    prev = last_drawer_count(conn, count["id"] if count else None)
    tx_from, wd_from = (prev["tx_upto"], prev["wd_upto"]) if prev else (0, 0)
    tx_to, wd_to = (count["tx_upto"], count["wd_upto"]) if count else (None, None)
    moves = drawer_movements(conn, tx_from, tx_to, wd_from, wd_to)
    report = {"start": ({"iqd": prev["iqd"], "usd": prev["usd"], "kind": prev["kind"], "at": prev["created_at"]}
                        if prev else None),
              **moves}
    if count:
        report["count_id"] = count["id"]
        report["counted"] = {"iqd": count["iqd"], "usd": count["usd"], "at": count["created_at"]}
        cash_sales, total_sales = {}, {}
        for cur, key in (("IQD", "iqd"), ("USD", "usd")):
            now, start = count[key], prev[key] if prev else None
            if now is None or start is None:
                continue  # not counted at both ends: no sales figure for this currency
            sales = (now - start - moves["customer_payments"][cur]
                     + moves["supplier_payments"][cur] + moves["withdrawals"][cur])
            cash_sales[cur] = sales
            total_sales[cur] = sales + moves["credit_sales"][cur]
        report["cash_sales"] = cash_sales
        report["total_sales"] = total_sales
        report["shortage"] = any(v < 0 for v in cash_sales.values())
    return report


def drawer_details(conn, count: dict | None = None) -> dict:
    """The day line by line, like the owner's own sheet: what came INTO the drawer (start,
    customer payments, cash sales) and what went OUT (paid to suppliers, withdrawals), and
    what is left. For count=None: since the last count, without sales (not counted yet)."""
    report = drawer_report(conn, count)
    prev = last_drawer_count(conn, count["id"] if count else None)
    tx_from, wd_from = (prev["tx_upto"], prev["wd_upto"]) if prev else (0, 0)
    tx_to = count["tx_upto"] if count else 1 << 62
    wd_to = count["wd_upto"] if count else 1 << 62
    lines_in, lines_out = [], []
    for cur, key in (("IQD", "iqd"), ("USD", "usd")):
        if prev and prev[key] is not None:
            lines_in.append({"what": "چان بالجرار" + (" (الفكة)" if prev["kind"] == "open" else " (العدّة اللي قبل)"),
                             "amount": prev[key], "currency": cur, "at": prev["created_at"]})
    for r in conn.execute(
            """SELECT t.created_at, c.kind, c.name, t.amount, t.currency FROM transactions t
               JOIN customers c ON c.id = t.customer_id
               WHERE t.id > ? AND t.id <= ? AND t.undone = 0 AND t.batch_id IS NULL
                 AND t.type = 'payment' AND t.from_drawer = 1 ORDER BY t.id""", (tx_from, tx_to)):
        line = {"amount": r["amount"], "currency": r["currency"], "at": r["created_at"]}
        if r["kind"] == "customer":
            lines_in.append({**line, "what": f"تسديد من {r['name']}"})
        else:
            lines_out.append({**line, "what": f"دفع لـ {r['name']}"})
    for r in conn.execute(
            """SELECT created_at, amount, currency, reason FROM withdrawals
               WHERE id > ? AND id <= ? AND undone = 0 AND from_drawer = 1 ORDER BY id""", (wd_from, wd_to)):
        lines_out.append({"what": f"سحب: {r['reason']}", "amount": r["amount"], "currency": r["currency"],
                          "at": r["created_at"]})
    for cur, sales in report.get("cash_sales", {}).items():
        lines_in.append({"what": "البيع النقدي (محسوب من العدّة)", "amount": sales, "currency": cur,
                         "at": count["created_at"], "computed": True})
    totals = {}
    for cur in CURRENCIES:
        t_in = sum(x["amount"] for x in lines_in if x["currency"] == cur)
        t_out = sum(x["amount"] for x in lines_out if x["currency"] == cur)
        if t_in or t_out:
            totals[cur] = {"in": t_in, "out": t_out, "left": t_in - t_out}
    return {"count_id": count["id"] if count else None, "counted": report.get("counted"), "start": report["start"],
            "in": lines_in, "out": lines_out, "totals": totals, "credit_sales": report["credit_sales"],
            "not_from_drawer": report["not_from_drawer"], "complete": bool(count and report.get("cash_sales"))}


def drawer_closes(conn, limit: int = 30) -> list[dict]:
    """Recent closing counts with their day's report, newest first."""
    rows = conn.execute("SELECT * FROM drawer_counts WHERE undone = 0 AND kind = 'close' "
                        "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [drawer_report(conn, dict(r)) for r in rows]


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


def all_balances(conn, kind: str = "customer") -> list[dict]:
    """Every party of this kind with their per-currency balance and last activity time.
    For a supplier the balance is what the shop owes them."""
    customers = {
        c["id"]: {"id": c["id"], "name": c["name"], "balances": {k: 0 for k in CURRENCIES},
                  "last_activity": None}
        for c in all_customers(conn, kind)
    }
    for r in conn.execute(_BALANCE_SQL.format(where="")):
        entry = customers.get(r["customer_id"])
        if entry is None:  # the other ledger
            continue
        entry["balances"][r["currency"]] = r["balance"]
        if entry["last_activity"] is None or r["last_activity"] > entry["last_activity"]:
            entry["last_activity"] = r["last_activity"]
    return list(customers.values())
