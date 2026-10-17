"""17-10-2026 merchant day: "خمسطعش الف" was asked again as "تقصد 15,000؟", and "الحجي كريم
عليه 100 دولار، ضيفه" asked "أضيفه؟" although the owner had just said to add him."""

from app import db
from app.agent import Agent, _owner_amounts
from app.llm import LLMResponse, ToolCall


class FakeLLM:
    def __init__(self, *responses):
        self.responses = list(responses)

    def complete(self, system, messages, tools):
        return self.responses.pop(0)


def call(tool, **args):
    return LLMResponse(text="", tool_calls=[ToolCall("c", tool, args)])


def test_iraqi_teens_and_hundreds():
    assert 15000 in _owner_amounts("سجل على ام حسين خمسطعش الف")
    assert {11000, 12000, 13000, 14000, 16000, 17000, 18000, 19000} <= (
        _owner_amounts("دعش") | _owner_amounts("اثنعش") | _owner_amounts("ثلطعش") | _owner_amounts("اربعطعش")
        | _owner_amounts("ستطعش") | _owner_amounts("سبعطعش") | _owner_amounts("ثمنطعش") | _owner_amounts("تسعطعش"))
    assert 500000 in _owner_amounts("خمسمية الف")


def test_iraqi_teens_are_recorded_without_asking_again(conn):
    c = db.add_customer(conn, "أم حسين")
    agent = Agent(conn, llm=FakeLLM(call("record_debt", customer_id=c, amount=15000, currency="IQD"),
                                    LLMResponse(text="سجلت")), log_path=None)
    out = agent.chat("s", "سجل على ام حسين خمسطعش الف")
    assert out["tool_calls"][0]["result"]["ok"] and db.get_balance(conn, c)["IQD"] == 15000


def test_add_in_the_same_message_when_the_owner_said_so(conn):
    agent = Agent(conn, llm=FakeLLM(call("add_customer", name="الحجي كريم"), LLMResponse(text="ضفته")),
                  log_path=None)
    out = agent.chat("s", "الحجي كريم عليه 100 دولار، ضيفه")
    assert out["tool_calls"][0]["result"]["ok"]
    assert [x["name"] for x in db.all_customers(conn)] == ["الحجي كريم"]


def test_a_new_name_without_add_still_asks_first(conn):
    agent = Agent(conn, llm=FakeLLM(call("add_customer", name="سعد"), LLMResponse(text="أضيفه؟")), log_path=None)
    assert agent.chat("s", "سجل على زبون جديد اسمه سعد 30 الف")["tool_calls"][0]["result"]["error"] == "ask_first"
    assert db.all_customers(conn) == []


def test_add_said_for_one_name_does_not_add_another(conn):
    # "ضيفه" was about كريم; the model trying to add a different name still has to ask
    agent = Agent(conn, llm=FakeLLM(call("add_customer", name="سعد"), LLMResponse(text="أضيفه؟")), log_path=None)
    assert agent.chat("s", "الحجي كريم عليه 100 دولار، ضيفه")["tool_calls"][0]["result"]["error"] == "ask_first"
