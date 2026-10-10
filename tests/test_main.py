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
