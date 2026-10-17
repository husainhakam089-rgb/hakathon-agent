"""The merchant day (17-10-2026): "حيدر غلط، شطبها" undid the supplier's payment, the last
entry, instead of Haider's debt; a fix in one message re-recorded a payment and undid it
in the same breath; and empty replies said "ما فهمت" after something was saved."""

from app import db
from app.agent import EMPTY_NOTE, EMPTY_REPLY, Agent
from app.llm import LLMResponse, ToolCall


class FakeLLM:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, system, messages, tools):
        self.calls.append(messages)
        return self.responses.pop(0)


def call(name, **args):
    return LLMResponse(text="", tool_calls=[ToolCall("c", name, args)])


def test_named_undo_cancels_that_persons_entry_not_the_last_one(conn):
    haider = db.add_customer(conn, "حيدر")
    rafidain = db.add_customer(conn, "شركة الرافدين", kind="supplier")
    agent = Agent(conn, llm=FakeLLM(
        call("record_debt", customer_id=haider, amount=15000, currency="IQD"), LLMResponse(text="سجلت"),
        call("record_supplier_payment", supplier_id=rafidain, amount=100000, currency="IQD"), LLMResponse(text="دفعت"),
        call("undo_last"), LLMResponse(text="لغيت دين حيدر")), log_path=None)
    agent.chat("s", "سجل على حيدر 15 الف")
    agent.chat("s", "دفعت للرافدين 100 الف")
    out = agent.chat("s", "حيدر غلط، شطبها")
    undone = out["tool_calls"][0]["result"]["undone"]
    assert undone["customer_name"] == "حيدر" and undone["amount"] == 15000
    assert db.get_balance(conn, haider)["IQD"] == 0
    assert db.get_balance(conn, rafidain)["IQD"] == -100000  # the payment to the supplier stays


def test_named_undo_never_falls_back_to_someone_else(conn):
    haider = db.add_customer(conn, "حيدر")
    ali = db.add_customer(conn, "أبو علي")
    db.add_transaction(conn, haider, "debt", 15000)  # recorded in another conversation
    agent = Agent(conn, llm=FakeLLM(
        call("record_debt", customer_id=ali, amount=5000, currency="IQD"), LLMResponse(text="سجلت"),
        call("undo_last"), LLMResponse(text="ما كو")), log_path=None)
    agent.chat("s", "سجل على ابو علي 5")
    out = agent.chat("s", "حيدر غلط شطبها")
    assert out["tool_calls"][0]["result"]["error"] == "nothing_to_undo"
    assert db.get_balance(conn, ali)["IQD"] == 5000 and db.get_balance(conn, haider)["IQD"] == 15000


def test_the_longest_name_wins(conn):
    ali = db.add_customer(conn, "علي")
    abu_ali = db.add_customer(conn, "أبو علي")
    agent = Agent(conn, llm=FakeLLM(
        call("record_debt", customer_id=abu_ali, amount=7000, currency="IQD"), LLMResponse(text="سجلت"),
        call("record_debt", customer_id=ali, amount=3000, currency="IQD"), LLMResponse(text="سجلت"),
        call("undo_last"), LLMResponse(text="لغيت")), log_path=None)
    agent.chat("s", "سجل على ابو علي 7")
    agent.chat("s", "سجل على علي 3")
    agent.chat("s", "ابو علي غلط، شطبها")
    assert db.get_balance(conn, abu_ali)["IQD"] == 0 and db.get_balance(conn, ali)["IQD"] == 3000


def test_one_message_never_undoes_what_it_just_recorded(conn):
    rafidain = db.add_customer(conn, "شركة الرافدين", kind="supplier")
    both = LLMResponse(text="", tool_calls=[
        ToolCall("a", "record_supplier_payment", {"supplier_id": rafidain, "amount": 100000, "currency": "IQD"}),
        ToolCall("b", "undo_last", {})])
    agent = Agent(conn, llm=FakeLLM(both, LLMResponse(text="سجلت الدفعة، وشنو تريد ألغي؟")), log_path=None)
    out = agent.chat("s", "رجع دفعة الرافدين 100 الف، والغي الغلط")
    assert out["tool_calls"][1]["result"]["error"] == "undo_same_message"
    assert db.get_balance(conn, rafidain)["IQD"] == -100000  # the payment is really there


def test_plain_undo_still_cancels_the_last_entry(conn):
    a = db.add_customer(conn, "سعد")
    agent = Agent(conn, llm=FakeLLM(call("record_debt", customer_id=a, amount=1000, currency="IQD"),
                                    LLMResponse(text="تمام"), call("undo_last"), LLMResponse(text="لغيته")),
                  log_path=None)
    agent.chat("s", "سجل على سعد الف")
    assert agent.chat("s", "شطبها")["tool_calls"][0]["result"]["ok"]
    assert db.get_balance(conn, a)["IQD"] == 0


def test_an_empty_reply_is_asked_again_once(conn):
    agent = Agent(conn, llm=FakeLLM(LLMResponse(text=""), LLMResponse(text="ما أگدر أبيع دين لحيدر، حافظها بالذاكرة.")),
                  log_path=None)
    out = agent.chat("s", "بيع حيدر 2 زيت دين")
    assert out["reply"] == "ما أگدر أبيع دين لحيدر، حافظها بالذاكرة."
    assert agent.llm.calls[1][-1]["text"] == EMPTY_NOTE
    assert [m["role"] for m in agent.session("s").history()] == ["user", "assistant"]  # the note is not kept


def test_still_empty_after_a_save_says_what_was_saved(conn):
    a = db.add_customer(conn, "أبو علي")
    agent = Agent(conn, llm=FakeLLM(call("set_customer_phone", customer_id=a, phone="07701234567"),
                                    LLMResponse(text=""), LLMResponse(text="")), log_path=None)
    out = agent.chat("s", "رقم ابو علي 07701234567")
    assert out["reply"] == "تمام، حفظت رقم أبو علي."


def test_still_empty_with_nothing_saved_keeps_the_old_reply(conn):
    agent = Agent(conn, llm=FakeLLM(LLMResponse(text=""), LLMResponse(text="")), log_path=None)
    assert agent.chat("s", "؟")["reply"] == EMPTY_REPLY
