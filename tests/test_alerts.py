"""Stage 19: the agent reminds and warns by itself (with the owner's yes for anything it records)."""

from datetime import datetime

from app import db
from app.agent import Agent, unsupported_numbers, _owner_numbers
from app.llm import LLMResponse, ToolCall


class FakeLLM:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, system, messages, tools):
        self.calls.append(messages)
        return self.responses.pop(0)


# ---------- duplicate payment ----------

def test_same_payment_twice_within_minutes_is_asked(conn, tools):
    ali = db.add_customer(conn, "أبو علي")
    db.add_transaction(conn, ali, "debt", 50000)
    tools.turn = 1
    tools._turn_entries = set()
    assert tools.execute("record_payment", {"customer_id": ali, "amount": 10000, "currency": "IQD"})["ok"]
    tools.turn = 2
    tools._turn_entries = set()  # next message: the same again
    r = tools.execute("record_payment", {"customer_id": ali, "amount": 10000, "currency": "IQD"})
    assert r["error"] == "possible_duplicate" and r["customer_name"] == "أبو علي"
    assert tools.execute("record_payment", {"customer_id": ali, "amount": 10000, "currency": "IQD", "confirmed": True})["ok"]
    assert db.get_balance(conn, ali)["IQD"] == 30000


def test_two_equal_lines_in_one_message_are_not_a_duplicate(conn, tools):
    ali = db.add_customer(conn, "أبو علي")
    tools._turn_entries = set()
    assert tools.execute("record_debt", {"customer_id": ali, "amount": 5000, "currency": "IQD"})["ok"]
    assert tools.execute("record_debt", {"customer_id": ali, "amount": 5000, "currency": "IQD"})["ok"]


def test_old_or_different_entries_are_not_duplicates(conn, tools):
    ali = db.add_customer(conn, "أبو علي")
    old = db.add_transaction(conn, ali, "payment", 10000)
    conn.execute("UPDATE transactions SET created_at = datetime('now','localtime','-1 hour') WHERE id = ?", (old,))
    tools._turn_entries = set()
    assert tools.execute("record_payment", {"customer_id": ali, "amount": 10000, "currency": "IQD"})["ok"]
    assert tools.execute("record_payment", {"customer_id": ali, "amount": 20000, "currency": "IQD"})["ok"]


# ---------- recurring expenses ----------

def test_recurring_expense_due_then_recorded_once(conn, tools):
    tools.source_message = "الإيجار 250 ألف كل شهر يوم 1"
    r = tools.execute("add_recurring_expense", {"reason": "إيجار", "amount": 250000, "currency": "IQD", "day_of_month": 1})
    assert r["ok"] and r["due_now"]  # day 1 has always come
    assert [x["reason"] for x in db.recurring_due(conn)] == ["إيجار"]
    rec = tools.execute("record_recurring_expense", {"recurring_id": r["recurring_id"]})
    assert rec["ok"] and rec["amount"] == 250000 and rec["reason"] == "إيجار"
    assert db.withdrawals_summary(conn, "month")["totals"]["IQD"] == 250000
    assert db.recurring_due(conn) == []
    assert tools.execute("record_recurring_expense", {"recurring_id": r["recurring_id"]})["error"] == "already_done"
    # undo removes the withdrawal (the month stays marked: it was answered)
    assert tools.execute("undo_last", {})["undone"]["ledger"] == "withdrawal"


def test_recurring_due_dates(conn):
    rid = db.add_recurring(conn, "مولدة", 360000, "IQD", 31)
    assert db.recurring_due(conn, datetime(2026, 2, 27)) == []
    assert [r["id"] for r in db.recurring_due(conn, datetime(2026, 2, 28))] == [rid]  # 31 = last day of February
    db.mark_recurring_done(conn, rid, datetime(2026, 2, 28))
    assert db.recurring_due(conn, datetime(2026, 2, 28)) == []
    assert [r["id"] for r in db.recurring_due(conn, datetime(2026, 3, 31))] == [rid]  # next month again


def test_recurring_skip_and_remove(conn, tools):
    rid = db.add_recurring(conn, "راتب عامل", 400000, "IQD", 1)
    assert tools.execute("record_recurring_expense", {"recurring_id": rid, "skip": True})["skipped"]
    assert db.withdrawals_summary(conn, "month")["totals"]["IQD"] == 0 and db.recurring_due(conn) == []
    assert tools.execute("remove_recurring_expense", {"recurring_id": rid})["ok"]
    assert tools.execute("list_recurring_expenses", {})["count"] == 0


def test_recurring_only_when_asked(tools):
    tools.source_message = "سحبت 250 ألف إيجار"
    r = tools.execute("add_recurring_expense", {"reason": "إيجار", "amount": 250000, "currency": "IQD", "day_of_month": 1})
    assert r["error"] == "ask_first"


# ---------- overdue debts and the drawer reminder ----------

def test_overdue_debts(conn):
    ali, umh, kar = (db.add_customer(conn, n) for n in ("أبو علي", "أم حسين", "كرار"))
    db.add_transaction(conn, ali, "debt", 50000)
    db.add_transaction(conn, umh, "debt", 30000)
    paid = db.add_transaction(conn, umh, "payment", 5000)
    db.add_transaction(conn, kar, "debt", 7000)
    db.add_transaction(conn, kar, "payment", 7000)  # owes nothing
    conn.execute("UPDATE transactions SET created_at = datetime('now','localtime','-40 days') WHERE customer_id = ?", (ali,))
    conn.execute("UPDATE transactions SET created_at = datetime('now','localtime','-50 days') WHERE customer_id = ? AND id != ?",
                 (umh, paid))
    conn.execute("UPDATE transactions SET created_at = datetime('now','localtime','-10 days') WHERE id = ?", (paid,))
    conn.execute("UPDATE transactions SET created_at = datetime('now','localtime','-90 days') WHERE customer_id = ?", (kar,))
    conn.commit()
    late = db.overdue_debts(conn, 30)
    assert [(x["name"], x["days"]) for x in late] == [("أبو علي", 40)]  # أم حسين paid 10 days ago
    assert [(x["name"], x["days"]) for x in db.overdue_debts(conn, 7)] == [("أبو علي", 40), ("أم حسين", 10)]


def test_drawer_reminder_after_the_hour(conn, tools):
    db.set_setting(conn, "drawer_hour", 21)
    assert not db.alerts(conn, datetime.now().replace(hour=20))["drawer_not_counted"]
    assert db.alerts(conn, datetime.now().replace(hour=22))["drawer_not_counted"]
    tools.execute("set_drawer_opening", {"iqd": 0})
    tools.execute("count_drawer", {"iqd": 1000})
    assert not db.alerts(conn, datetime.now().replace(hour=22))["drawer_not_counted"]


def test_set_reminders(conn, tools):
    assert tools.execute("set_reminders", {"drawer_hour": 20, "overdue_days": 45})["ok"]
    assert db.get_setting(conn, "drawer_hour") == "20" and db.get_setting(conn, "overdue_days") == "45"
    assert tools.execute("set_reminders", {"drawer_hour": 25})["error"] == "invalid_hour"
    assert tools.execute("set_reminders", {})["error"] == "nothing_to_change"


# ---------- invented figures ----------

def test_number_check_rules():
    allowed = _owner_numbers("سجل على ابو علي 25 الف") | {75000}
    assert unsupported_numbers("سجلت 25,000، صار عليه 75,000.", allowed) == []
    assert unsupported_numbers("صار عليه 80,000", allowed) == [80000]
    assert unsupported_numbers("الساعة 21:30 يوم 2026-10-12، سنة 2026", allowed) == []  # times, dates, years
    assert unsupported_numbers("عندك 3 زباين و 500 دينار", allowed) == []  # small numbers are not checked
    assert unsupported_numbers("٨٠٬٠٠٠ دينار", allowed) == [80000]  # Arabic-Indic digits too
    assert 1_500_000 in _owner_numbers("مليون ونص") and 250_000 in _owner_numbers("ربع مليون")


def test_invented_figure_is_sent_back_once(conn):
    ali = db.add_customer(conn, "أبو علي")
    llm = FakeLLM(
        LLMResponse(text="", tool_calls=[ToolCall("t", "record_debt", {"customer_id": ali, "amount": 25000, "currency": "IQD"})]),
        LLMResponse(text="تمام، سجلت 25,000. صار عليه 125,000."),  # 125,000 comes from nowhere
        LLMResponse(text="تمام، سجلت على أبو علي 25,000 دينار. صار عليه 25,000."),
    )
    agent = Agent(conn, llm=llm, log_path=None)
    r = agent.chat("s", "سجل على ابو علي 25 الف")
    assert r["reply"] == "تمام، سجلت على أبو علي 25,000 دينار. صار عليه 25,000."
    assert "125,000" in llm.calls[-1][-1]["text"]  # the note named the figure
    # the rejected draft and the note do not stay in the history
    assert [m["text"] for m in agent.session("s").history() if m["role"] == "user"] == ["سجل على ابو علي 25 الف"]


def test_invented_figure_twice_warns_the_owner(conn):
    llm = FakeLLM(LLMResponse(text="ربحك هالشهر 900,000."), LLMResponse(text="ربحك هالشهر 900,000 تقريبًا."))
    r = Agent(conn, llm=llm, log_path=None).chat("s", "شكد ربحت؟")
    assert "⚠️" in r["reply"] and "900,000" in r["reply"]
    assert r["tool_calls"][-1]["tool"] == "number_check" and r["tool_calls"][-1]["result"]["unsupported"] == [900000]


# ---------- endpoints ----------

def test_alert_endpoints(conn, monkeypatch):
    from fastapi.testclient import TestClient

    from app import main as main_mod

    monkeypatch.setattr(main_mod, "agent", Agent(conn, llm=object(), log_path=None))
    client = TestClient(main_mod.app)
    r = client.post("/recurring", json={"reason": "إيجار", "amount": 250000, "day_of_month": 1}).json()
    assert r["ok"]
    a = client.get("/alerts").json()
    assert [x["reason"] for x in a["recurring_due"]] == ["إيجار"]
    done = client.post(f"/recurring/{r['recurring']['id']}/record", json={"session_id": "s1"}).json()
    assert done["ok"] and "إيجار" in done["reply"]
    assert client.get("/alerts").json()["recurring_due"] == []
    # undo from the chat works on it
    assert main_mod.agent.session("s1").tools.execute("undo_last", {})["undone"]["ledger"] == "withdrawal"
    assert client.post("/settings", json={"drawer_hour": 19, "overdue_days": 14}).json()["settings"] == \
        {"drawer_hour": 19, "overdue_days": 14}
    assert client.delete(f"/recurring/{r['recurring']['id']}").json()["ok"]
    assert client.get("/recurring").json()["recurring"] == []


def test_a_loss_said_as_a_positive_figure_is_fine():
    from app.agent import _walk_numbers
    allowed = set()
    _walk_numbers({"net_profit": {"IQD": -259000}}, allowed)
    assert unsupported_numbers("الربح الصافي سالب 259,000 دينار", allowed) == []
