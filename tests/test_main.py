import pytest
from fastapi.testclient import TestClient

from app import db
from app import main as main_mod
from app.agent import Agent
from app.llm import LLMResponse, ToolCall


class FakeLLM:
    def __init__(self, *responses):
        self.responses = list(responses)

    def complete(self, system, messages, tools):
        return self.responses.pop(0)


@pytest.fixture
def client(conn, monkeypatch):
    def use(*responses):
        monkeypatch.setattr(main_mod, "agent", Agent(conn, llm=FakeLLM(*responses), log_path=None))
        return TestClient(main_mod.app)
    return use


def test_index_page(client):
    r = client().get("/")
    assert r.status_code == 200 and "دفتر الديون" in r.text and 'dir="rtl"' in r.text
    assert r.headers["cache-control"] == "no-cache"


def test_chat_then_debtors(client, conn):
    cid = db.add_customer(conn, "أبو علي")
    db.add_customer(conn, "سعد")  # no balance: not listed
    c = client(
        LLMResponse(text="", tool_calls=[ToolCall("t1", "record_debt",
                                                  {"customer_id": cid, "amount": 25000, "currency": "IQD"})]),
        LLMResponse(text="تمام، سجلت على أبو علي 25,000 دينار."),
    )
    r = c.post("/chat", json={"session_id": "s1", "message": "سجل على ابو علي 25 الف"})
    assert r.status_code == 200
    body = r.json()
    assert body["reply"].startswith("تمام")
    assert body["tool_calls"][0]["tool"] == "record_debt"

    d = c.get("/debtors").json()
    assert [x["name"] for x in d["debtors"]] == ["أبو علي"]
    assert d["debtors"][0]["balances"] == {"IQD": 25000, "USD": 0}
    assert d["totals"] == {"IQD": 25000, "USD": 0}


def test_debtors_sorted_and_credit_not_in_total(client, conn):
    a, b, c_ = (db.add_customer(conn, n) for n in ("أ", "ب", "ج"))
    db.add_transaction(conn, a, "debt", 1000)
    db.add_transaction(conn, b, "debt", 5000)
    db.add_transaction(conn, c_, "payment", 2000)  # shop owes them
    d = client().get("/debtors").json()
    assert [x["name"] for x in d["debtors"]] == ["ب", "أ", "ج"]
    assert d["totals"]["IQD"] == 6000


def test_empty_message_rejected(client):
    assert client().post("/chat", json={"session_id": "s", "message": ""}).status_code == 422


def test_port_in_use():
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        port = s.getsockname()[1]
        assert main_mod.port_in_use(port)
    assert not main_mod.port_in_use(port)


def test_version_endpoint(client):
    assert client().get("/version").json() == {"app": "debt-agent", "version": main_mod.APP_VERSION}


def test_running_version_of_something_else():
    assert main_mod.running_version("http://127.0.0.1:9") is None  # nothing listening


def test_voice_message_is_saved_with_source_voice(client, conn):
    cid = db.add_customer(conn, "أبو علي")
    debt = lambda n: LLMResponse(text="", tool_calls=[ToolCall("t", "record_debt", {"customer_id": cid, "amount": n, "currency": "IQD"})])  # noqa: E731
    # different amounts: the same one twice within minutes is now asked about (stage 19)
    c = client(debt(25000), LLMResponse(text="تمام"), debt(30000), LLMResponse(text="تمام"))
    c.post("/chat", json={"session_id": "s", "message": "سجل على ابو علي 25 الف", "source": "voice"})
    c.post("/chat", json={"session_id": "s", "message": "سجل على ابو علي 30 الف"})
    assert [r[0] for r in conn.execute("SELECT source FROM transactions ORDER BY id")] == ["voice", "chat"]
    assert c.post("/chat", json={"session_id": "s", "message": "x", "source": "email"}).status_code == 422


def test_suppliers_endpoint_is_its_own_ledger(client, conn):
    ali = db.add_customer(conn, "أبو علي")
    zain = db.add_customer(conn, "شركة الزين", kind="supplier")
    db.add_transaction(conn, ali, "debt", 25000)
    db.add_transaction(conn, zain, "debt", 500000)
    db.add_transaction(conn, zain, "debt", 40, "USD")
    c = client()
    d, s = c.get("/debtors").json(), c.get("/suppliers").json()
    assert [x["name"] for x in d["debtors"]] == ["أبو علي"] and d["totals"] == {"IQD": 25000, "USD": 0}
    assert [x["name"] for x in s["debtors"]] == ["شركة الزين"] and s["totals"] == {"IQD": 500000, "USD": 40}


def test_withdrawals_endpoint(client, conn):
    db.add_withdrawal(conn, 50000, "IQD", "مولدة")
    db.add_withdrawal(conn, 25000, "IQD", "المولدة")
    db.add_withdrawal(conn, 200000, "IQD", "إيجار")
    db.add_withdrawal(conn, 20, "USD", "شخصي")
    w = client().get("/withdrawals").json()
    assert w["totals"] == {"IQD": 275000, "USD": 20} and w["today"] == {"IQD": 275000, "USD": 20}
    assert [(r["balances"]["IQD"], r["balances"]["USD"]) for r in w["debtors"]] == [(200000, 0), (75000, 0), (0, 20)]


def test_memories_endpoints(client, conn):
    mid = db.add_memory(conn, "الورقة = 10,000 دينار")
    c = client()
    assert [m["text"] for m in c.get("/memories").json()["memories"]] == ["الورقة = 10,000 دينار"]
    assert c.delete(f"/memories/{mid}").json()["ok"]
    assert c.get("/memories").json()["memories"] == []
    assert c.delete(f"/memories/{mid}").status_code == 404


def test_sessions_endpoints(client, conn):
    db.save_turn(conn, "old", "سجل على ابو علي 25 الف", "تمام", [{"tool": "record_debt", "input": {}, "result": {"ok": True}}])
    db.save_turn(conn, "new", "شكد مجموع الديون؟", "65,000")
    c = client()
    s = c.get("/sessions").json()["sessions"]
    assert [x["id"] for x in s] == ["new", "old"] and s[1]["title"] == "سجل على ابو علي 25 الف"
    m = c.get("/sessions/old").json()["messages"]
    assert [x["role"] for x in m] == ["user", "assistant"] and m[1]["tool_calls"][0]["tool"] == "record_debt"
    assert c.get("/sessions/nothing").json()["messages"] == []
