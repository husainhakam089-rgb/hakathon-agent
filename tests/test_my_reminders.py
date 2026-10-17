""""ذكرني": anything the owner asks to be reminded of, saved and brought up at its time."""

from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from app import daily, db
from app.tools import _when_words


def _ask(tools, msg, **args):
    tools.source_message = msg
    return tools.execute("remind_me", args)


def test_tomorrow_morning_and_in_two_hours(conn, tools):
    r = _ask(tools, "ذكرني باچر الصبح اتصل بالمندوب", text="اتصل بالمندوب", day="tomorrow", time="09:00")
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    assert r["ok"] and r["due_at"] == f"{tomorrow} 09:00" and r["when_words"].startswith("باچر")
    r = _ask(tools, "ذكرني بعد ساعتين اطلب غاز", text="اطلب غاز", in_hours=2)
    due = datetime.strptime(r["due_at"], "%Y-%m-%d %H:%M")
    assert timedelta(minutes=119) <= due - datetime.now() <= timedelta(minutes=121)


def test_a_weekday_and_the_code_does_the_dates(conn, tools):
    r = _ask(tools, "نبهني الخميس 4 العصر اروح للبنك", text="روح للبنك", day="thursday", time="16:00")
    due = datetime.strptime(r["due_at"], "%Y-%m-%d %H:%M")
    assert due.weekday() == 3 and due.hour == 16 and timedelta(0) < due - datetime.now() <= timedelta(days=7)


def test_refusals(conn, tools):
    assert _ask(tools, "شكد على ابو علي؟", text="x", day="tomorrow")["error"] == "ask_first"  # he never asked
    assert _ask(tools, "ذكرني اليوم", text="x", day="today")["error"] == "time_required"
    assert _ask(tools, "ذكرني", text="x", day="date", date="2020-01-01", time="09:00")["error"] == "time_passed"
    assert _ask(tools, "ذكرني", text="x", day="tomorrow", time="25:00")["error"] == "invalid_time"
    assert _ask(tools, "ذكرني", text="", day="tomorrow")["error"] == "text_required"


def test_list_and_cancel(conn, tools):
    rid = _ask(tools, "ذكرني باچر", text="اتصل بالمندوب", day="tomorrow")["reminder_id"]
    assert [r["text"] for r in tools.execute("list_reminders", {})["reminders"]] == ["اتصل بالمندوب"]
    assert tools.execute("cancel_reminder", {"reminder_id": rid})["ok"]
    assert tools.execute("list_reminders", {})["count"] == 0


def test_due_one_comes_up_in_alerts_and_repeating_ones_come_back(conn):
    now = datetime(2026, 10, 17, 9, 30)
    once = db.add_owner_reminder(conn, "اتصل بالمندوب", datetime(2026, 10, 17, 9, 0))
    weekly = db.add_owner_reminder(conn, "رتب الرفوف", datetime(2026, 10, 17, 8, 0), repeat="weekly")
    later = db.add_owner_reminder(conn, "بعدين", datetime(2026, 10, 18, 9, 0))
    assert [r["id"] for r in db.alerts(conn, now)["my_reminders"]] == [weekly, once]
    db.finish_owner_reminder(conn, once, now)
    r = db.finish_owner_reminder(conn, weekly, now)
    assert r["due_at"] == "2026-10-24 08:00:00" and not r["done"]  # next week, same time
    assert db.due_owner_reminders(conn, now) == [] and later


def test_monthly_on_the_31st_lands_on_the_last_day():
    assert db._next_due(datetime(2026, 1, 31, 9), "monthly", datetime(2026, 2, 1)) == datetime(2026, 2, 28, 9)


def test_telegram_gets_each_due_reminder_once(conn, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    db.add_owner_reminder(conn, "اتصل بالمندوب", datetime(2026, 10, 17, 9, 0))
    sent = []
    now = datetime(2026, 10, 17, 9, 5)
    assert daily.notify_due_reminders(conn, now, send=lambda t: sent.append(t) or True) == 1
    assert daily.notify_due_reminders(conn, now, send=lambda t: sent.append(t) or True) == 0
    assert sent == ["⏰ تذكير: اتصل بالمندوب"]


def test_reminder_buttons(conn, monkeypatch):
    from app import main as main_mod
    from app.agent import Agent

    monkeypatch.setattr(main_mod, "agent", Agent(conn, llm=object(), log_path=None))
    c = TestClient(main_mod.app)
    rid = db.add_owner_reminder(conn, "اتصل بالمندوب", datetime.now() - timedelta(minutes=5))
    assert [r["text"] for r in c.get("/alerts").json()["my_reminders"]] == ["اتصل بالمندوب"]
    assert c.post(f"/owner-reminders/{rid}/snooze", json={"minutes": 60}).json()["ok"]
    assert c.get("/alerts").json()["my_reminders"] == []
    db.snooze_owner_reminder(conn, rid, datetime.now() - timedelta(minutes=1))
    assert c.post(f"/owner-reminders/{rid}/done").json()["reminder"]["done"] == 1
    assert c.get("/owner-reminders").json()["reminders"] == []
    assert c.post("/owner-reminders/999/done").status_code == 404


def test_when_words():
    now = datetime(2026, 10, 17, 10, 0)  # Saturday
    assert _when_words(datetime(2026, 10, 18, 9, 0), now) == "باچر الأحد الساعة 9:00 الصبح"
    assert _when_words(datetime(2026, 10, 22, 16, 30), now) == "الخميس الجاي الساعة 4:30 العصر"
    assert _when_words(datetime(2026, 10, 17, 21, 0), now) == "اليوم السبت الساعة 9:00 بالليل"
