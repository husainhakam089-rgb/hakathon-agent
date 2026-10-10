"""Web server: chat page + API. Run: python -m app.main  (or: uvicorn app.main:app)"""

import os
import re
import uuid
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

load_dotenv()

from app import db  # noqa: E402  (after load_dotenv: DB_PATH comes from .env)
from app import extraction  # noqa: E402
from app.agent import Agent  # noqa: E402
from app.llm import LLMError  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "static"
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR") or ROOT / "uploads")
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_PDF_PAGES = 5
MIGRATED_NOTE = "رصيد منقول من الدفتر"

# Accepted uploads, recognised by their first bytes (not by name or browser header).
FILE_KINDS = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp", "pdf": "application/pdf"}
FILE_ID_RE = re.compile(r"^[0-9a-f]{32}$")

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


# ---------- stage 6: photos of the notebook ----------

def _error(status: int, message: str, **extra):
    return JSONResponse({"ok": False, "message": message, **extra}, status_code=status)


def _sniff(head: bytes) -> str | None:
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head.startswith(b"%PDF"):
        return "pdf"
    return None


def _pdf_pages(data: bytes) -> int:
    # Good enough for a limit check without a PDF library: count page objects.
    return len(re.findall(rb"/Type\s*/Page(?![a-zA-Z])", data))


def _uploaded_file(file_id: str) -> tuple[Path, str] | None:
    if not FILE_ID_RE.match(file_id or ""):
        return None
    for ext, mime in FILE_KINDS.items():
        path = UPLOAD_DIR / f"{file_id}.{ext}"
        if path.is_file():
            return path, mime
    return None


@app.post("/upload")
async def upload(request: Request):
    """Raw file in the body (the browser already shrank photos). Saved under uploads/."""
    data = bytearray()
    async for chunk in request.stream():
        data += chunk
        if len(data) > MAX_UPLOAD_BYTES:
            return _error(413, "الملف أكبر من 10 ميگا.")
    kind = _sniff(bytes(data[:16]))
    if kind is None:
        return _error(415, "نوع الملف مو مدعوم. دز صورة (jpg أو png أو webp) أو pdf.")
    if kind == "pdf" and _pdf_pages(bytes(data)) > MAX_PDF_PAGES:
        return _error(413, f"الـ PDF بيه أكثر من {MAX_PDF_PAGES} صفحات.")
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    file_id = uuid.uuid4().hex
    (UPLOAD_DIR / f"{file_id}.{kind}").write_bytes(bytes(data))
    return {"ok": True, "file_id": file_id, "kind": kind, "size": len(data)}


class ExtractRequest(BaseModel):
    file_id: str = Field(min_length=1, max_length=64)
    note: str | None = Field(default=None, max_length=500)


@app.post("/extract")
def extract(req: ExtractRequest):
    """Read the page into a review table. Writes nothing to the database."""
    found = _uploaded_file(req.file_id)
    if not found:
        return _error(404, "ما لگيت الملف، ارفعه من جديد.")
    path, mime = found
    try:
        result = extraction.extract_file(agent.get_llm(), path.read_bytes(), mime, req.note)
        with agent.lock:
            review = extraction.build_review(agent.conn, result)
    except extraction.ExtractionNotReady as e:
        return _error(409, e.message)
    except NotImplementedError:
        return _error(409, "ميزة الصور بعدها مو جاهزة: لازم تنكتب مطابقة الأسماء "
                           "(match_extracted_rows بملف app/extraction.py).")
    except LLMError as e:
        return _error(502, e.user_message)
    return {"ok": True, "file_id": req.file_id, **review}


class CommitRow(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    customer_id: int | None = None
    amount: int | str | None = None
    currency: str | None = None
    type: str | None = None


class CommitRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=100)
    file_id: str | None = Field(default=None, max_length=64)
    rows: list[CommitRow] = Field(max_length=extraction.MAX_ROWS)
    migrated: bool = False  # "these are balances carried over from the old notebook"


def _money(amount: int, currency: str) -> str:
    return f"{amount:,} " + ("دولار" if currency == "USD" else "دينار")


@app.post("/import/commit")
def import_commit(req: CommitRequest):
    """Save the rows the owner reviewed and ticked, in one transaction."""
    if req.file_id and not FILE_ID_RE.match(req.file_id):
        return _error(400, "رقم الملف غلط.")
    with agent.lock:
        rows, errors = extraction.prepare_commit(agent.conn, [r.model_dump() for r in req.rows])
        if errors:
            return _error(422, "صلّح هالصفوف وعيد:", errors=errors)
        batch_id = uuid.uuid4().hex[:12]
        source = f"image:{req.file_id}" if req.file_id else "image"
        db.import_batch(agent.conn, rows, batch_id, source, MIGRATED_NOTE if req.migrated else None)

    totals: dict[tuple[str, str], int] = {}
    for r in rows:
        totals[(r["type"], r["currency"])] = totals.get((r["type"], r["currency"]), 0) + r["amount"]
    parts = []
    for (tx_type, cur), amount in sorted(totals.items()):
        parts.append(("ديون " if tx_type == "debt" else "تسديدات ") + _money(amount, cur))
    reply = (f"سجلت {len(rows)} قيد: {'، '.join(parts)}. "
             "إذا شي غلط گلي 'ألغِ آخر دفعة'.")
    agent.record_event(req.session_id, "(رفعت صورة من الدفتر وراجعت الجدول وسجلته)",
                       f"{reply} (batch_id: {batch_id})")
    return {"ok": True, "batch_id": batch_id, "count": len(rows), "reply": reply}


def port_in_use(port: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def main() -> None:
    import threading
    import webbrowser

    import uvicorn

    port = int(os.getenv("PORT") or 8000)
    url = f"http://127.0.0.1:{port}"
    if port_in_use(port):
        # Otherwise the browser would open an older copy that is still running.
        print(f"المنفذ {port} مشغول: أكو نسخة ثانية من البرنامج شغالة (ممكن نسخة قديمة).")
        print("سدها أول، أو اكتب بـ PowerShell:  taskkill /F /IM python.exe /IM py.exe")
        print("وبعدين شغّل start.bat من جديد.")
        raise SystemExit(1)
    print(f"دفتر الديون شغال على: {url}  (للإيقاف: Ctrl+C)")
    if not os.getenv("NO_BROWSER"):
        threading.Timer(1.5, webbrowser.open, [url]).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
