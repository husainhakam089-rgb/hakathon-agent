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
  ci_upto           INTEGER NOT NULL DEFAULT 0,  -- last cash_ins id
  sa_upto           INTEGER NOT NULL DEFAULT 0,  -- last sales id (stage 17)
  source_message    TEXT,
  session_id        TEXT,
  undone            INTEGER NOT NULL DEFAULT 0,
  created_at        TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- Stage 16: money that came into the drawer and is not a customer's debt payment:
-- "وارد محمد 25 ألف" (a cash sale or any cash in), with who or what it was from.
-- Money going out is a withdrawal ("صادر 35 ألف راتب عامل") or a supplier payment.
CREATE TABLE IF NOT EXISTS cash_ins (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  amount            INTEGER NOT NULL CHECK (amount > 0),
  currency          TEXT NOT NULL DEFAULT 'IQD' CHECK (currency IN ('IQD','USD')),
  label             TEXT NOT NULL,          -- from whom or for what, as the owner said it
  note              TEXT,
  source_message    TEXT,
  source            TEXT NOT NULL DEFAULT 'chat',
  session_id        TEXT,
  undone            INTEGER NOT NULL DEFAULT 0,
  created_at        TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- Stage 17: what the shop sells, and each sale from the sale screen or the chat.
-- Prices are whole dinars (or dollars). cost may be unknown (NULL): then no profit for it.
CREATE TABLE IF NOT EXISTS items (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  name              TEXT NOT NULL,
  normalized_name   TEXT NOT NULL,
  unit              TEXT,                    -- كيس، كارتون، قطعة...
  price             INTEGER NOT NULL CHECK (price > 0),
  cost              INTEGER CHECK (cost IS NULL OR cost >= 0),
  currency          TEXT NOT NULL DEFAULT 'IQD' CHECK (currency IN ('IQD','USD')),
  active            INTEGER NOT NULL DEFAULT 1,
  created_at        TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
-- payment: cash (into the drawer) | debt (added to the customer: debt_tx_id) |
-- electronic (Zain Cash, card: not in the drawer). Never deleted, only undone.
CREATE TABLE IF NOT EXISTS sales (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  payment           TEXT NOT NULL CHECK (payment IN ('cash','debt','electronic')),
  customer_id       INTEGER REFERENCES customers(id),
  debt_tx_id        INTEGER REFERENCES transactions(id),
  total             INTEGER NOT NULL CHECK (total > 0),
  cost_total        INTEGER,                 -- NULL when a line's cost is unknown
  currency          TEXT NOT NULL DEFAULT 'IQD' CHECK (currency IN ('IQD','USD')),
  source_message    TEXT,
  source            TEXT NOT NULL DEFAULT 'chat',  -- chat | voice | screen
  session_id        TEXT,
  undone            INTEGER NOT NULL DEFAULT 0,
  created_at        TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS sale_lines (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  sale_id           INTEGER NOT NULL REFERENCES sales(id),
  item_id           INTEGER NOT NULL REFERENCES items(id),
  qty               REAL NOT NULL CHECK (qty > 0),
  unit_price        INTEGER NOT NULL,
  unit_cost         INTEGER,
  line_total        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sale_lines_sale ON sale_lines(sale_id);

-- Stage 19: the agent reminds by itself. Settings are small key/values.
CREATE TABLE IF NOT EXISTS settings (
  key               TEXT PRIMARY KEY,
  value             TEXT NOT NULL
);
-- Stage 22: payment links ("ادفع بزين كاش") for a customer, and reminders the owner sent.
-- provider: 'simulated' (a clearly marked test checkout) or a real one when its keys are set.
CREATE TABLE IF NOT EXISTS payment_links (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  token             TEXT NOT NULL UNIQUE,
  customer_id       INTEGER NOT NULL REFERENCES customers(id),
  amount            INTEGER NOT NULL CHECK (amount > 0),
  currency          TEXT NOT NULL DEFAULT 'IQD',
  provider          TEXT NOT NULL DEFAULT 'simulated',
  status            TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','paid','cancelled')),
  tx_id             INTEGER REFERENCES transactions(id),
  announced         INTEGER NOT NULL DEFAULT 0,   -- the owner was told it was paid
  created_at        TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  paid_at           TEXT
);
CREATE TABLE IF NOT EXISTS reminders (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  customer_id       INTEGER NOT NULL REFERENCES customers(id),
  text              TEXT NOT NULL,
  link_id           INTEGER REFERENCES payment_links(id),
  channel           TEXT NOT NULL DEFAULT 'whatsapp',
  created_at        TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
-- Stage 22: the daily report, made and sent by itself.
CREATE TABLE IF NOT EXISTS daily_reports (
  day               TEXT PRIMARY KEY,
  text              TEXT NOT NULL,
  sent_via          TEXT,
  created_at        TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
-- Fixed monthly expenses (rent, generator, salaries): on their day the owner is asked
-- "record it?"; nothing is recorded without a yes. done_month: 'YYYY-MM' recorded or skipped.
CREATE TABLE IF NOT EXISTS recurring_expenses (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  reason            TEXT NOT NULL,
  amount            INTEGER NOT NULL CHECK (amount > 0),
  currency          TEXT NOT NULL DEFAULT 'IQD' CHECK (currency IN ('IQD','USD')),
  day_of_month      INTEGER NOT NULL CHECK (day_of_month BETWEEN 1 AND 31),
  from_drawer       INTEGER NOT NULL DEFAULT 1,
  done_month        TEXT,
  active            INTEGER NOT NULL DEFAULT 1,
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
    dc_columns = {r["name"] for r in conn.execute("PRAGMA table_info(drawer_counts)")}
    for col in ("ci_upto", "sa_upto"):
        if col not in dc_columns:
            conn.execute(f"ALTER TABLE drawer_counts ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0")
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

PERIODS = ("today", "yesterday", "week", "month", "last_month", "all")
_PERIOD_SQL = {
    "today": "date(created_at) = date('now','localtime')",
    "yesterday": "date(created_at) = date('now','localtime','-1 day')",
    "last_month": "strftime('%Y-%m', created_at) = strftime('%Y-%m','now','localtime','start of month','-1 month')",
    "week": "date(created_at) > date('now','localtime','-7 days')",  # the last 7 days, today included
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
    undo_last expects: a transaction id, ("withdrawal", id), ("drawer", id) or ("cash_in", id)."""
    rows = conn.execute(
        """SELECT 'tx' AS kind, id, created_at FROM transactions WHERE session_id = ? AND undone = 0
           UNION ALL
           SELECT 'wd', id, created_at FROM withdrawals WHERE session_id = ? AND undone = 0
           UNION ALL
           SELECT 'dr', id, created_at FROM drawer_counts WHERE session_id = ? AND undone = 0
           UNION ALL
           SELECT 'ci', id, created_at FROM cash_ins WHERE session_id = ? AND undone = 0
           UNION ALL
           SELECT 'sa', id, created_at FROM sales WHERE session_id = ? AND undone = 0
           ORDER BY created_at, kind, id""", (session_id,) * 5)
    tags = {"wd": "withdrawal", "dr": "drawer", "ci": "cash_in", "sa": "sale"}
    return [r["id"] if r["kind"] == "tx" else (tags[r["kind"]], r["id"]) for r in rows]


# ---------- the cash drawer (stage 15) ----------

def _max_ids(conn) -> dict:
    """Where each list ends right now: a count covers every entry up to these ids."""
    one = lambda t: conn.execute(f"SELECT COALESCE(MAX(id), 0) FROM {t}").fetchone()[0]  # noqa: E731
    return {"tx_upto": one("transactions"), "wd_upto": one("withdrawals"), "ci_upto": one("cash_ins"),
            "sa_upto": one("sales")}


def _ids_before_today(conn) -> dict:
    one = lambda t: conn.execute(  # noqa: E731
        f"SELECT COALESCE(MAX(id), 0) FROM {t} WHERE date(created_at) < date('now','localtime')").fetchone()[0]
    return {"tx_upto": one("transactions"), "wd_upto": one("withdrawals"), "ci_upto": one("cash_ins"),
            "sa_upto": one("sales")}


UPTO_KEYS = ("tx_upto", "wd_upto", "ci_upto", "sa_upto")


def _range(prev: dict | None, count: dict | None) -> dict:
    """Entry ids after `prev` and up to `count` (no count: up to now)."""
    big = 1 << 62
    return {k: ((prev[k] if prev else 0), (count[k] if count else big)) for k in UPTO_KEYS}


def _live_range(conn, prev: dict | None, count: dict | None) -> dict:
    """Like _range, but with no count at all yet the drawer still shows today's money: every
    payment, sale, cash in and withdrawal since this morning, not the whole history."""
    if prev is None and count is None:
        start = _ids_before_today(conn)
        return {k: (start[k], 1 << 62) for k in UPTO_KEYS}
    return _range(prev, count)


def add_cash_in(conn, amount: int, currency: str, label: str, note=None, source_message=None,
                source="chat", session_id: str | None = None) -> int:
    cur = conn.execute(
        """INSERT INTO cash_ins (amount, currency, label, note, source_message, source, session_id)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (amount, currency, label.strip(), note, source_message, source, session_id))
    conn.commit()
    return cur.lastrowid


def get_cash_in(conn, ci_id: int):
    row = conn.execute("SELECT * FROM cash_ins WHERE id = ?", (ci_id,)).fetchone()
    return dict(row) if row else None


def mark_cash_in_undone(conn, ci_id: int) -> None:
    conn.execute("UPDATE cash_ins SET undone = 1 WHERE id = ?", (ci_id,))
    conn.commit()


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
        upto = _max_ids(conn)
    else:
        prev = last_drawer_count(conn)
        upto = {k: prev[k] for k in UPTO_KEYS} if prev else _ids_before_today(conn)
    cur = conn.execute(
        """INSERT INTO drawer_counts (kind, iqd, usd, tx_upto, wd_upto, ci_upto, sa_upto, source_message, session_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (kind, iqd, usd, upto["tx_upto"], upto["wd_upto"], upto["ci_upto"], upto["sa_upto"],
         source_message, session_id))
    conn.commit()
    return cur.lastrowid


def get_drawer_count(conn, count_id: int):
    row = conn.execute("SELECT * FROM drawer_counts WHERE id = ?", (count_id,)).fetchone()
    return dict(row) if row else None


def mark_drawer_count_undone(conn, count_id: int) -> None:
    conn.execute("UPDATE drawer_counts SET undone = 1 WHERE id = ?", (count_id,))
    conn.commit()


def drawer_movements(conn, rng: dict) -> dict:
    """Money in and out of the drawer between two counts, per currency. Imported rows (old
    notebook pages) and undone ones are not today's money."""
    zero = lambda: {c: 0 for c in CURRENCIES}  # noqa: E731
    out = {"customer_payments": zero(), "cash_ins": zero(), "sales_cash": zero(), "sales_electronic": zero(),
           "supplier_payments": zero(), "withdrawals": zero(), "credit_sales": zero(), "not_from_drawer": zero()}
    (tx_from, tx_to), (wd_from, wd_to), (ci_from, ci_to) = rng["tx_upto"], rng["wd_upto"], rng["ci_upto"]
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
    for r in conn.execute("""SELECT currency, SUM(amount) s FROM cash_ins
                             WHERE id > ? AND id <= ? AND undone = 0 GROUP BY currency""", (ci_from, ci_to)):
        out["cash_ins"][r["currency"]] += r["s"]
    sa_from, sa_to = rng["sa_upto"]
    for r in conn.execute("""SELECT payment, currency, SUM(total) s FROM sales WHERE id > ? AND id <= ? AND undone = 0
                             AND payment IN ('cash','electronic') GROUP BY payment, currency""", (sa_from, sa_to)):
        out["sales_cash" if r["payment"] == "cash" else "sales_electronic"][r["currency"]] += r["s"]
    return out


def drawer_report(conn, count: dict | None = None) -> dict:
    """The day that ends with `count` (a close), or so far since the last count (count=None:
    movements only, nothing counted to compare with yet). Each currency on its own:

    expected   = start + customer payments + cash ins ("وارد محمد") + cash sales from the sale screen
                 - paid to suppliers - withdrawals
    difference = counted - expected    (+ = sold and not written down, - = the drawer is short)
    cash sales = cash ins + screen cash sales + difference
               = counted - start - customer payments + suppliers + withdrawals
    """
    prev = last_drawer_count(conn, count["id"] if count else None)
    moves = drawer_movements(conn, _live_range(conn, prev, count))
    report = {"start": ({"iqd": prev["iqd"], "usd": prev["usd"], "kind": prev["kind"], "at": prev["created_at"]}
                        if prev else None),
              "since": "last_count" if prev else "today", **moves}
    if count:
        report["count_id"] = count["id"]
        report["counted"] = {"iqd": count["iqd"], "usd": count["usd"], "at": count["created_at"]}
        expected, difference, cash_sales, total_sales = {}, {}, {}, {}
        for cur, key in (("IQD", "iqd"), ("USD", "usd")):
            now, start = count[key], prev[key] if prev else None
            if now is None or start is None:
                continue  # not counted at both ends: nothing to compare for this currency
            expected[cur] = (start + moves["customer_payments"][cur] + moves["cash_ins"][cur]
                             + moves["sales_cash"][cur] - moves["supplier_payments"][cur] - moves["withdrawals"][cur])
            difference[cur] = now - expected[cur]
            cash_sales[cur] = moves["cash_ins"][cur] + moves["sales_cash"][cur] + difference[cur]
            total_sales[cur] = cash_sales[cur] + moves["credit_sales"][cur]
        report.update(expected=expected, difference=difference, cash_sales=cash_sales, total_sales=total_sales,
                      shortage=any(v < 0 for v in difference.values()))
    return report


def drawer_details(conn, count: dict | None = None) -> dict:
    """Every dinar that came INTO the drawer and went OUT, line by line with who or what and
    when, like the owner's own sheet. In: the start, customer payments, cash ins ("وارد محمد").
    Out: paid to suppliers, withdrawals ("صادر راتب عامل"). After a count, the difference
    closes it: in - out = what was counted. count=None: since the last count."""
    report = drawer_report(conn, count)
    prev = last_drawer_count(conn, count["id"] if count else None)
    rng = _live_range(conn, prev, count)
    lines_in, lines_out = [], []
    for cur, key in (("IQD", "iqd"), ("USD", "usd")):
        if prev and prev[key] is not None:
            lines_in.append({"what": "چان بالجرار" + (" (الفكة)" if prev["kind"] == "open" else " (العدّة اللي قبل)"),
                             "amount": prev[key], "currency": cur, "at": prev["created_at"], "start": True})
    entries = []  # (time, id order, side, line)
    for r in conn.execute(
            """SELECT t.id, t.created_at, c.kind, c.name, t.amount, t.currency FROM transactions t
               JOIN customers c ON c.id = t.customer_id
               WHERE t.id > ? AND t.id <= ? AND t.undone = 0 AND t.batch_id IS NULL
                 AND t.type = 'payment' AND t.from_drawer = 1""", rng["tx_upto"]):
        line = {"amount": r["amount"], "currency": r["currency"], "at": r["created_at"]}
        if r["kind"] == "customer":
            entries.append(("in", {**line, "what": f"تسديد من {r['name']}", "kind": "payment"}))
        else:
            entries.append(("out", {**line, "what": f"دفع لـ {r['name']}", "kind": "supplier"}))
    for r in conn.execute(
            """SELECT created_at, amount, currency, reason FROM withdrawals
               WHERE id > ? AND id <= ? AND undone = 0 AND from_drawer = 1""", rng["wd_upto"]):
        entries.append(("out", {"what": r["reason"], "amount": r["amount"], "currency": r["currency"],
                                "at": r["created_at"], "kind": "withdrawal"}))
    for r in conn.execute(
            """SELECT created_at, amount, currency, label FROM cash_ins
               WHERE id > ? AND id <= ? AND undone = 0""", rng["ci_upto"]):
        entries.append(("in", {"what": r["label"], "amount": r["amount"], "currency": r["currency"],
                               "at": r["created_at"], "kind": "cash_in"}))
    for r in conn.execute(
            """SELECT s.id, s.created_at, s.total, s.currency, s.payment,
                      GROUP_CONCAT(i.name || CASE WHEN l.qty != 1 THEN ' ×' || CAST(l.qty AS TEXT) ELSE '' END, '، ') AS what
               FROM sales s JOIN sale_lines l ON l.sale_id = s.id JOIN items i ON i.id = l.item_id
               WHERE s.id > ? AND s.id <= ? AND s.undone = 0 AND s.payment = 'cash'
               GROUP BY s.id""", rng["sa_upto"]):
        what = (r["what"] or "").replace(".0", "")
        entries.append(("in", {"what": f"بيع: {what}", "amount": r["total"], "currency": r["currency"],
                               "at": r["created_at"], "kind": "sale"}))
    entries.sort(key=lambda e: e[1]["at"])  # as it happened through the day
    for side, line in entries:
        (lines_in if side == "in" else lines_out).append(line)
    for cur, diff in report.get("difference", {}).items():
        if diff > 0:
            lines_in.append({"what": "زيادة بالجرار: بيع ما انكتب", "amount": diff, "currency": cur,
                             "at": count["created_at"], "computed": True})
        elif diff < 0:
            lines_out.append({"what": "نقص بالجرار: ما ينعرف وين راح", "amount": -diff, "currency": cur,
                              "at": count["created_at"], "computed": True})
    totals = {}
    for cur in CURRENCIES:
        t_in = sum(x["amount"] for x in lines_in if x["currency"] == cur)
        t_out = sum(x["amount"] for x in lines_out if x["currency"] == cur)
        if t_in or t_out:
            totals[cur] = {"in": t_in, "out": t_out, "left": t_in - t_out}
    return {"count_id": count["id"] if count else None, "counted": report.get("counted"), "start": report["start"],
            "in": lines_in, "out": lines_out, "totals": totals, "credit_sales": report["credit_sales"],
            "not_from_drawer": report["not_from_drawer"], "sales_electronic": report["sales_electronic"],
            "difference": report.get("difference", {}),
            "cash_sales": report.get("cash_sales", {}), "complete": bool(count and report.get("difference")),
            "since": report["since"]}


def drawer_closes(conn, limit: int = 30) -> list[dict]:
    """Recent closing counts with their day's report, newest first."""
    rows = conn.execute("SELECT * FROM drawer_counts WHERE undone = 0 AND kind = 'close' "
                        "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [drawer_report(conn, dict(r)) for r in rows]


# ---------- items and sales (stage 17) ----------

def add_item(conn, name: str, price: int, cost: int | None = None, unit: str | None = None,
             currency: str = "IQD") -> int:
    cur = conn.execute(
        "INSERT INTO items (name, normalized_name, unit, price, cost, currency) VALUES (?, ?, ?, ?, ?, ?)",
        (name.strip(), normalize(name), (unit or "").strip() or None, price, cost, currency))
    conn.commit()
    return cur.lastrowid


def get_item(conn, item_id: int):
    row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    return dict(row) if row else None


def all_items(conn, active_only: bool = True) -> list[dict]:
    sql = "SELECT * FROM items" + (" WHERE active = 1" if active_only else "") + " ORDER BY name"
    return [dict(r) for r in conn.execute(sql)]


def find_items(conn, query: str) -> list[dict]:
    items = all_items(conn)
    return find_matches(query, items)


def update_item(conn, item_id: int, **fields) -> None:
    allowed = {k: v for k, v in fields.items() if k in ("name", "unit", "price", "cost", "active") and v is not None}
    if "name" in allowed:
        allowed["normalized_name"] = normalize(allowed["name"])
    if allowed:
        sets = ", ".join(f"{k} = ?" for k in allowed)
        conn.execute(f"UPDATE items SET {sets} WHERE id = ?", (*allowed.values(), item_id))
        conn.commit()


def add_sale(conn, payment: str, lines: list[dict], currency: str = "IQD", customer_id: int | None = None,
             source_message=None, source="chat", session_id: str | None = None) -> int:
    """One sale, all or nothing. lines: [{item_id, qty, unit_price, unit_cost}] already checked.
    A debt sale also writes the customer's debt (no session id on it: undoing the sale undoes it)."""
    total = sum(round(l["qty"] * l["unit_price"]) for l in lines)
    costs = [l["unit_cost"] for l in lines]
    cost_total = None if any(c is None for c in costs) else sum(round(l["qty"] * l["unit_cost"]) for l in lines)
    names = "، ".join(f"{get_item(conn, l['item_id'])['name']}" + (f" ×{_qty(l['qty'])}" if l["qty"] != 1 else "")
                      for l in lines)
    try:
        debt_tx = None
        if payment == "debt":
            debt_tx = add_transaction(conn, customer_id, "debt", total, currency, note=f"بيع: {names}",
                                      source_message=source_message, source=source, commit=False)
        cur = conn.execute(
            """INSERT INTO sales (payment, customer_id, debt_tx_id, total, cost_total, currency, source_message,
                                  source, session_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (payment, customer_id, debt_tx, total, cost_total, currency, source_message, source, session_id))
        sale_id = cur.lastrowid
        for l in lines:
            conn.execute(
                "INSERT INTO sale_lines (sale_id, item_id, qty, unit_price, unit_cost, line_total) VALUES (?, ?, ?, ?, ?, ?)",
                (sale_id, l["item_id"], l["qty"], l["unit_price"], l["unit_cost"], round(l["qty"] * l["unit_price"])))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return sale_id


def _qty(q) -> str:
    return str(int(q)) if float(q).is_integer() else f"{q:g}"


def get_sale(conn, sale_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM sales WHERE id = ?", (sale_id,)).fetchone()
    if not row:
        return None
    sale = dict(row)
    sale["lines"] = [dict(r) for r in conn.execute(
        """SELECT l.*, i.name, i.unit FROM sale_lines l JOIN items i ON i.id = l.item_id
           WHERE l.sale_id = ? ORDER BY l.id""", (sale_id,))]
    sale["what"] = "، ".join(l["name"] + (f" ×{_qty(l['qty'])}" if l["qty"] != 1 else "") for l in sale["lines"])
    return sale


def mark_sale_undone(conn, sale_id: int) -> None:
    sale = get_sale(conn, sale_id)
    conn.execute("UPDATE sales SET undone = 1 WHERE id = ?", (sale_id,))
    if sale and sale["debt_tx_id"]:
        conn.execute("UPDATE transactions SET undone = 1 WHERE id = ?", (sale["debt_tx_id"],))
    conn.commit()


def recent_sales(conn, limit: int = 10) -> list[dict]:
    ids = [r["id"] for r in conn.execute("SELECT id FROM sales WHERE undone = 0 ORDER BY id DESC LIMIT ?", (limit,))]
    out = []
    for i in ids:
        sale = get_sale(conn, i)
        if sale["customer_id"]:
            sale["customer_name"] = get_customer(conn, sale["customer_id"])["name"]
        out.append(sale)
    return out


def sales_summary(conn, period: str = "month", item_id: int | None = None) -> dict:
    """Per item (or one item): how much was sold, for how much, its cost and profit; and the
    totals by payment. Profit counts only lines whose cost is known."""
    where = _PERIOD_SQL[period if period in PERIODS else "month"].replace("created_at", "s.created_at")
    args: list = []
    if item_id is not None:
        where += " AND l.item_id = ?"
        args.append(item_id)
    items = []
    for r in conn.execute(
            f"""SELECT i.id, i.name, i.unit, s.currency, SUM(l.qty) qty, SUM(l.line_total) revenue,
                       SUM(CASE WHEN l.unit_cost IS NULL THEN NULL ELSE ROUND(l.qty * l.unit_cost) END) cost,
                       SUM(CASE WHEN l.unit_cost IS NULL THEN 1 ELSE 0 END) unknown_cost, COUNT(DISTINCT s.id) sales
                FROM sale_lines l JOIN sales s ON s.id = l.sale_id JOIN items i ON i.id = l.item_id
                WHERE s.undone = 0 AND {where}
                GROUP BY i.id, s.currency ORDER BY revenue DESC""", args):
        cost = int(r["cost"]) if r["cost"] is not None and not r["unknown_cost"] else None
        items.append({"item_id": r["id"], "name": r["name"], "unit": r["unit"], "currency": r["currency"],
                      "qty": r["qty"], "revenue": r["revenue"], "cost": cost,
                      "profit": r["revenue"] - cost if cost is not None else None, "sales": r["sales"]})
    by_payment = {p: {c: 0 for c in CURRENCIES} for p in ("cash", "debt", "electronic")}
    # by payment: the lines that were asked about (one item: only that item's lines, never the
    # whole sales it was part of, which would count the other items too)
    for r in conn.execute(f"""SELECT s.payment, s.currency, SUM(l.line_total) s FROM sale_lines l
                              JOIN sales s ON s.id = l.sale_id WHERE s.undone = 0 AND {where}
                              GROUP BY s.payment, s.currency""", args):
        by_payment[r["payment"]][r["currency"]] = r["s"]
    totals = {c: sum(by_payment[p][c] for p in by_payment) for c in CURRENCIES}
    return {"period": period, "items": items, "by_payment": by_payment, "totals": totals}


# ---------- reminders and alerts (stage 19) ----------

DEFAULT_SETTINGS = {"drawer_hour": "21", "overdue_days": "30"}
# stage 22 settings: kept apart so the alert settings above stay what the old screens expect
EXTRA_SETTINGS = {"report_hour": "23", "shop_name": "المحل"}


def get_setting(conn, key: str) -> str:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else {**DEFAULT_SETTINGS, **EXTRA_SETTINGS}[key]


def set_setting(conn, key: str, value) -> None:
    conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (key, str(value)))
    conn.commit()


def _now(now=None):
    from datetime import datetime
    return now or datetime.now()


def add_recurring(conn, reason: str, amount: int, currency: str, day: int, from_drawer: bool = True) -> int:
    cur = conn.execute(
        "INSERT INTO recurring_expenses (reason, amount, currency, day_of_month, from_drawer) VALUES (?, ?, ?, ?, ?)",
        (reason.strip(), amount, currency, day, int(bool(from_drawer))))
    conn.commit()
    return cur.lastrowid


def get_recurring(conn, rec_id: int):
    row = conn.execute("SELECT * FROM recurring_expenses WHERE id = ?", (rec_id,)).fetchone()
    return dict(row) if row else None


def list_recurring(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM recurring_expenses WHERE active = 1 ORDER BY day_of_month, id")]


def remove_recurring(conn, rec_id: int) -> None:
    conn.execute("UPDATE recurring_expenses SET active = 0 WHERE id = ?", (rec_id,))
    conn.commit()


def mark_recurring_done(conn, rec_id: int, now=None) -> None:
    conn.execute("UPDATE recurring_expenses SET done_month = ? WHERE id = ?", (_now(now).strftime("%Y-%m"), rec_id))
    conn.commit()


def recurring_due(conn, now=None) -> list[dict]:
    """Fixed expenses whose day has come this month and that were not recorded or skipped
    yet. Day 31 in a 30-day month is the last day."""
    import calendar
    now = _now(now)
    last_day = calendar.monthrange(now.year, now.month)[1]
    month = now.strftime("%Y-%m")
    return [r for r in list_recurring(conn)
            if min(r["day_of_month"], last_day) <= now.day and r["done_month"] != month]


def overdue_debts(conn, days: int | None = None, now=None) -> list[dict]:
    """Customers who owe money and paid nothing for `days` days (counted from their last
    payment, or from their first unpaid debt if they never paid). Oldest first."""
    days = int(days if days is not None else get_setting(conn, "overdue_days"))
    now = _now(now)
    out = []
    for c in all_balances(conn, "customer"):
        if not any(v > 0 for v in c["balances"].values()):
            continue
        row = conn.execute(
            """SELECT MAX(CASE WHEN type = 'payment' THEN created_at END) last_payment,
                      MIN(CASE WHEN type = 'debt' THEN created_at END) first_debt
               FROM transactions WHERE customer_id = ? AND undone = 0""", (c["id"],)).fetchone()
        since = row["last_payment"] or row["first_debt"]
        if not since:
            continue
        from datetime import datetime
        waited = (now - datetime.fromisoformat(since)).days
        if waited >= days:
            out.append({"id": c["id"], "name": c["name"], "balance": c["balances"], "days": waited,
                        "last_payment": row["last_payment"]})
    out.sort(key=lambda r: r["days"], reverse=True)
    return out


def drawer_counted_today(conn, now=None) -> bool:
    day = _now(now).strftime("%Y-%m-%d")
    return conn.execute("SELECT 1 FROM drawer_counts WHERE undone = 0 AND kind = 'close' AND date(created_at) = ?",
                        (day,)).fetchone() is not None


def recent_duplicate(conn, customer_id: int, tx_type: str, amount: int, currency: str, minutes: int = 10):
    """The same entry for the same party within the last few minutes (a message sent twice)."""
    row = conn.execute(
        f"""SELECT id, created_at FROM transactions WHERE customer_id = ? AND type = ? AND amount = ? AND currency = ?
            AND undone = 0 AND created_at >= datetime('now','localtime','-{int(minutes)} minutes')
            ORDER BY id DESC LIMIT 1""", (customer_id, tx_type, amount, currency)).fetchone()
    return dict(row) if row else None


def set_phone(conn, customer_id: int, phone: str | None) -> None:
    conn.execute("UPDATE customers SET phone = ? WHERE id = ?", (phone, customer_id))
    conn.commit()


# ---------- payment links and reminders (stage 22) ----------

def add_payment_link(conn, customer_id: int, amount: int, currency: str = "IQD", provider: str = "simulated") -> dict:
    import secrets
    token = secrets.token_urlsafe(12)
    cur = conn.execute("INSERT INTO payment_links (token, customer_id, amount, currency, provider) VALUES (?, ?, ?, ?, ?)",
                       (token, customer_id, amount, currency, provider))
    conn.commit()
    return get_payment_link(conn, cur.lastrowid)


def get_payment_link(conn, link_id: int | None = None, token: str | None = None):
    if token is not None:
        row = conn.execute("SELECT * FROM payment_links WHERE token = ?", (token,)).fetchone()
    else:
        row = conn.execute("SELECT * FROM payment_links WHERE id = ?", (link_id,)).fetchone()
    return dict(row) if row else None


def pay_link(conn, link: dict, source: str = "zaincash") -> int:
    """The customer paid through the link: their payment goes into the notebook by itself
    (electronic: not into the drawer). Returns the transaction id. Paying twice does nothing."""
    if link["status"] != "pending":
        return link["tx_id"]
    tx = add_transaction(conn, link["customer_id"], "payment", link["amount"], link["currency"],
                         note=f"زين كاش ({'تجربة' if link['provider'] == 'simulated' else link['provider']}) · رابط #{link['id']}",
                         source=source, commit=False, from_drawer=False)
    conn.execute("UPDATE payment_links SET status = 'paid', tx_id = ?, paid_at = datetime('now','localtime') WHERE id = ?",
                 (tx, link["id"]))
    conn.commit()
    return tx


def unannounced_payments(conn) -> list[dict]:
    rows = conn.execute(
        """SELECT p.id, p.amount, p.currency, p.paid_at, c.name FROM payment_links p JOIN customers c ON c.id = p.customer_id
           WHERE p.status = 'paid' AND p.announced = 0 ORDER BY p.id""").fetchall()
    return [dict(r) for r in rows]


def mark_announced(conn, ids: list[int]) -> None:
    conn.executemany("UPDATE payment_links SET announced = 1 WHERE id = ?", [(i,) for i in ids])
    conn.commit()


def add_reminder(conn, customer_id: int, text: str, link_id: int | None, channel: str = "whatsapp") -> int:
    cur = conn.execute("INSERT INTO reminders (customer_id, text, link_id, channel) VALUES (?, ?, ?, ?)",
                       (customer_id, text, link_id, channel))
    conn.commit()
    return cur.lastrowid


def last_reminder(conn, customer_id: int):
    row = conn.execute("SELECT * FROM reminders WHERE customer_id = ? ORDER BY id DESC LIMIT 1", (customer_id,)).fetchone()
    return dict(row) if row else None


def alerts(conn, now=None) -> dict:
    """Everything the agent should bring up by itself, worked out by code."""
    now = _now(now)
    hour = int(get_setting(conn, "drawer_hour"))
    return {
        "drawer_not_counted": now.hour >= hour and not drawer_counted_today(conn, now),
        "drawer_hour": hour,
        "recurring_due": recurring_due(conn, now),
        "overdue": _with_reminders(conn, overdue_debts(conn, None, now)),
        "overdue_days": int(get_setting(conn, "overdue_days")),
        "paid_links": unannounced_payments(conn),
    }


def _with_reminders(conn, rows: list[dict]) -> list[dict]:
    for r in rows:
        last = last_reminder(conn, r["id"])
        r["reminded_at"] = last["created_at"] if last else None
        r["phone"] = get_customer(conn, r["id"]).get("phone")
    return rows


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
