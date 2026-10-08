"""Web server: chat page + API. Run: python -m app.main  (or: uvicorn app.main:app)"""

import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

load_dotenv()

from app import db  # noqa: E402  (after load_dotenv: DB_PATH comes from .env)
from app.agent import Agent  # noqa: E402

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

app = FastAPI(title="دفتر الديون")
agent = Agent(db.connect())


class ChatRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=2000)


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/chat")
def chat(req: ChatRequest):
    return agent.chat(req.session_id, req.message)


@app.get("/debtors")
def debtors():
    """Everyone with a non-zero balance (negative = shop owes them), biggest debt first."""
    with agent.lock:
        rows = db.all_balances(agent.conn)
    rows = [r for r in rows if any(r["balances"].values())]
    rows.sort(key=lambda r: (r["balances"]["IQD"], r["balances"]["USD"]), reverse=True)
    totals = {cur: sum(max(r["balances"][cur], 0) for r in rows) for cur in db.CURRENCIES}
    return {"debtors": rows, "totals": totals}


def main() -> None:
    import threading
    import webbrowser

    import uvicorn

    port = int(os.getenv("PORT") or 8000)
    url = f"http://127.0.0.1:{port}"
    print(f"دفتر الديون شغال على: {url}  (للإيقاف: Ctrl+C)")
    if not os.getenv("NO_BROWSER"):
        threading.Timer(1.5, webbrowser.open, [url]).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
