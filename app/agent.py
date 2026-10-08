"""Agent loop: model decides which tools to call, we execute them, repeat."""

import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path

from app.llm import LLMError, make_llm
from app.prompts import SYSTEM_PROMPT
from app.tools import TOOL_SCHEMAS, DebtTools

log = logging.getLogger("debt_agent")

MAX_ITERATIONS = 6
MAX_HISTORY_MESSAGES = 20  # user + assistant text messages kept per session

CONFUSED_REPLY = "صار عندي لبس، عيد الطلب بطريقة أبسط"
REFUSED_REPLY = "ما أگدر أساعد بهذا. هسه أسوي الديون بس."
EMPTY_REPLY = "ما فهمت عليك، عيد الطلب بطريقة ثانية."

DEFAULT_LOG_PATH = Path(__file__).resolve().parent.parent / "logs" / "tool_calls.jsonl"


class Session:
    def __init__(self, conn):
        self.tools = DebtTools(conn)
        # Completed turns; each turn is a list of neutral messages (see llm.py).
        self.turns: list[list[dict]] = []

    def history(self) -> list[dict]:
        return [m for turn in self.turns for m in turn]

    def commit(self, turn: list[dict]) -> None:
        # Drop provider-native data (e.g. thinking blocks): they are only valid
        # inside the turn that produced them, and history gets trimmed.
        self.turns.append([{k: v for k, v in m.items() if k != "raw"} for m in turn])
        # Each turn holds one user text and one final assistant text.
        max_turns = max(1, MAX_HISTORY_MESSAGES // 2)
        del self.turns[:-max_turns]


class Agent:
    def __init__(self, conn, llm=None, log_path: Path | None = DEFAULT_LOG_PATH):
        self.conn = conn
        self.llm = llm
        self.log_path = log_path
        self.sessions: dict[str, Session] = {}
        # v0 serves one shop: one turn at a time keeps the shared SQLite connection safe.
        self._lock = threading.Lock()

    def _get_llm(self):
        if self.llm is None:
            self.llm = make_llm()
        return self.llm

    def session(self, session_id: str) -> Session:
        if session_id not in self.sessions:
            self.sessions[session_id] = Session(self.conn)
        return self.sessions[session_id]

    def reset(self, session_id: str) -> None:
        self.sessions.pop(session_id, None)

    def chat(self, session_id: str, message: str) -> dict:
        """Run one user turn. Returns {"reply": str, "tool_calls": [...]}."""
        with self._lock:
            return self._chat(session_id, message.strip())

    def _chat(self, session_id: str, message: str) -> dict:
        session = self.session(session_id)
        session.tools.source_message = message
        turn: list[dict] = [{"role": "user", "text": message}]
        tool_log: list[dict] = []

        reply = None
        try:
            llm = self._get_llm()
            for _ in range(MAX_ITERATIONS):
                response = llm.complete(SYSTEM_PROMPT, session.history() + turn, TOOL_SCHEMAS)
                if response.refused:
                    reply = REFUSED_REPLY
                    break
                if not response.tool_calls:
                    reply = response.text or EMPTY_REPLY
                    break

                turn.append({"role": "assistant", "text": response.text,
                             "tool_calls": response.tool_calls, "raw": response.raw})
                results = []
                for call in response.tool_calls:
                    entry = self._run_tool(session, session_id, message, call)
                    tool_log.append(entry)
                    results.append({"id": call.id, "name": call.name,
                                    "content": json.dumps(entry["result"], ensure_ascii=False),
                                    "is_error": not entry["result"].get("ok", False)})
                turn.append({"role": "tool", "results": results})
            else:
                reply = CONFUSED_REPLY
        except LLMError as e:
            log.warning("LLM error: %s", e.detail)
            reply = e.user_message
        except Exception:  # never crash the chat on an unexpected failure
            log.exception("Agent turn failed")
            reply = "صار خطأ غير متوقع، عيد المحاولة."

        turn.append({"role": "assistant", "text": reply, "tool_calls": []})
        session.commit(turn)
        return {"reply": reply, "tool_calls": tool_log}

    def _run_tool(self, session: Session, session_id: str, message: str, call) -> dict:
        started = time.perf_counter()
        try:
            result = session.tools.execute(call.name, call.input)
        except Exception as e:
            log.exception("Tool %s crashed", call.name)
            result = {"ok": False, "error": "tool_crashed", "message": str(e)}
        entry = {
            "tool": call.name,
            "input": call.input,
            "result": result,
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            "at": datetime.now().isoformat(timespec="seconds"),
        }
        self._write_log(session_id, message, entry)
        return entry

    def _write_log(self, session_id: str, message: str, entry: dict) -> None:
        log.info("tool %s %s -> %s", entry["tool"], entry["input"], entry["result"])
        if not self.log_path:
            return
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as f:
                record = {"session_id": session_id, "message": message, **entry}
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            log.exception("Could not write tool log")
