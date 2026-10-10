"""Stage 6: read debts from a photo (or PDF) of the paper notebook.

Nothing here writes to the database. The flow is:

    file --extract_file()--> extraction (rows as the model read them)
         --build_review()--> rows + match (existing / ambiguous / new) + selected
         --> the shop owner reviews and edits the table in the browser
         --prepare_commit()--> validated rows --db.import_batch()--> saved, all or nothing

Three parts are Hussein's to write (PLAN_v1_media.md, section 1), marked "HUSSEIN" below:
    1. EXTRACTION_PROMPT           -> app/prompts.py
    2. SUBMIT_EXTRACTION_SCHEMA    -> this file
    3. match_extracted_rows()      -> this file
Until they are written, the image feature answers with a clear "not ready" message.

Contract the rest of the code relies on (the review table in static/index.html reads
these field names; if you rename one, rename it there too):

    extraction = {
        "page_type": str,
        "rows": [{
            "raw_text": str, "name": str | None, "amount": int | None,
            "amount_as_written": str | None, "currency": "IQD" | "USD" | "unknown",
            "type": "debt" | "payment" | "unknown", "date": str | None,
            "crossed_out": bool, "confidence": "high" | "medium" | "low",
        }, ...],
        "unreadable_lines": [str, ...],
        "notes": str,
    }

    match_extracted_rows() adds to every row:
        "match": {"status": "existing", "customer_id": int}
               | {"status": "ambiguous", "candidates": [{"id": int, "name": str}, ...]}
               | {"status": "new"}
"""

import copy

from app import db
from app.arabic import normalize

TOOL_NAME = "submit_extraction"
MAX_ROWS = 500
MAX_AMOUNT = 10_000_000_000  # sanity limit for one row, in the row's currency

# Arabic-Indic and Persian digits -> ASCII, for amounts the model returns as text.
_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


class ExtractionNotReady(Exception):
    """One of Hussein's parts is still empty. `message` is shown to the shop owner."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


# =====================================================================
# HUSSEIN (2/3): the schema of the `submit_extraction` tool
# =====================================================================
# The model is forced to call this tool once, with the whole page as its input.
# Write a JSON Schema ("type": "object", "properties": {...}, "required": [...]).
# Start from the proposal in PLAN_v1_media.md section 6.5, and keep the field names
# of the contract at the top of this file (or update static/index.html with them).
# Tips:
#   - "enum" for page_type, currency, type and confidence keeps the model honest.
#   - "description" on each field is read by the model: it is part of the prompt.
#   - amount: integer or null. amount_as_written: string, exactly as on the paper.
SUBMIT_EXTRACTION_SCHEMA: dict | None = None


# =====================================================================
# HUSSEIN (3/3): match every extracted name to the customers we already have
# =====================================================================
def match_extracted_rows(conn, rows: list[dict]) -> list[dict]:
    """Add a "match" field to each row (see the contract at the top of this file).

    Use db.find_customers(conn, name) (it uses arabic.find_matches from v0):
      - exactly one customer   -> {"status": "existing", "customer_id": id}
      - more than one          -> {"status": "ambiguous", "candidates": [{"id", "name"}, ...]}
      - none, or name is None  -> {"status": "new"}
    Think about: find_matches also returns "contains" matches (searching "علي" finds
    "أبو علي"). Should one exact match win over several partial ones?
    Return the same rows (same order) with "match" added. Don't write to the database.
    """
    raise NotImplementedError


# =====================================================================
# Pipeline (not Hussein's part)
# =====================================================================

def _check_ready() -> None:
    from app.prompts import EXTRACTION_PROMPT

    if not (EXTRACTION_PROMPT or "").strip():
        raise ExtractionNotReady("ميزة الصور بعدها مو جاهزة: لازم تنكتب تعليمات الاستخراج "
                                 "(EXTRACTION_PROMPT بملف app/prompts.py).")
    if not SUBMIT_EXTRACTION_SCHEMA:
        raise ExtractionNotReady("ميزة الصور بعدها مو جاهزة: لازم ينكتب مخطط الاستخراج "
                                 "(SUBMIT_EXTRACTION_SCHEMA بملف app/extraction.py).")


def _to_int(value):
    """Amount from the model -> int, or None when it is not a clean positive number."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value) if float(value).is_integer() and value > 0 else None
    text = str(value).translate(_DIGITS).replace(",", "").replace("٬", "").strip()
    return int(text) if text.isdigit() and int(text) > 0 else None


def extract_file(llm, file_bytes: bytes, mime: str, note: str | None = None) -> dict:
    """Ask the model to read the page. Returns the extraction, cleaned up a little."""
    from app.prompts import EXTRACTION_PROMPT

    _check_ready()
    tool = {
        "name": TOOL_NAME,
        "description": "سلّم كل اللي انقرأ من الصفحة. استدعيها مرة وحدة بس.",
        "input_schema": SUBMIT_EXTRACTION_SCHEMA,
    }
    text = "هاي صورة الصفحة. استخرج اللي بيها واستدعي submit_extraction."
    if note:
        # The owner's note is context, the same as everything else: data, not orders.
        text += f"\n\nملاحظة صاحب المحل ويا الصورة: «{note.strip()}»"
    raw = llm.extract(EXTRACTION_PROMPT, text, file_bytes, mime, tool)

    rows = []
    for r in (raw.get("rows") or [])[:MAX_ROWS]:
        if not isinstance(r, dict):
            continue
        r = dict(r)
        r["amount"] = _to_int(r.get("amount"))
        r["crossed_out"] = bool(r.get("crossed_out"))
        rows.append(r)
    return {
        "page_type": raw.get("page_type") or "other",
        "rows": rows,
        "unreadable_lines": [str(x) for x in (raw.get("unreadable_lines") or [])],
        "notes": str(raw.get("notes") or ""),
    }


def build_review(conn, extraction: dict) -> dict:
    """Extraction + customer matching + which rows start ticked. Read-only."""
    rows = match_extracted_rows(conn, copy.deepcopy(extraction["rows"]))
    for i, r in enumerate(rows):
        r["index"] = i
        # Crossed-out and low-confidence rows start unticked (PLAN 6.8); so do rows
        # the owner would have to complete anyway.
        r["selected"] = not (
            r.get("crossed_out") or r.get("confidence") == "low" or not r.get("name")
            or not r.get("amount") or r.get("type") not in ("debt", "payment")
            or r.get("currency") not in ("IQD", "USD")
        )
    return {**extraction, "rows": rows}


def prepare_commit(conn, rows: list[dict]) -> tuple[list[dict], list[str]]:
    """Validate the rows the owner ticked. Returns (rows for db.import_batch, errors).

    Each incoming row: {"name": str, "customer_id": int | None, "amount": int,
                        "currency": "IQD"|"USD", "type": "debt"|"payment"}
    customer_id None = new customer called `name`, unless exactly one customer with
    that normalized name already exists (then that one is used, no duplicate).
    """
    if not rows:
        return [], ["ما كو ولا صف محدد."]
    if len(rows) > MAX_ROWS:
        return [], [f"الحد الأقصى {MAX_ROWS} صف بالدفعة الوحدة."]

    out, errors = [], []
    for n, r in enumerate(rows, start=1):
        name = str(r.get("name") or "").strip()
        label = f"الصف {n} ({name or 'بدون اسم'})"
        amount = _to_int(r.get("amount"))
        if amount is None or amount > MAX_AMOUNT:
            errors.append(f"{label}: المبلغ لازم يكون رقم صحيح أكبر من صفر.")
            continue
        if r.get("currency") not in db.CURRENCIES:
            errors.append(f"{label}: اختار العملة (دينار أو دولار).")
            continue
        if r.get("type") not in ("debt", "payment"):
            errors.append(f"{label}: اختار النوع (دين أو تسديد).")
            continue

        row = {"type": r["type"], "amount": amount, "currency": r["currency"],
               "customer_id": None, "new_customer": None}
        cid = r.get("customer_id")
        if cid not in (None, ""):
            try:
                cid = int(cid)
            except (TypeError, ValueError):
                cid = None
            if cid is None or not db.get_customer(conn, cid):
                errors.append(f"{label}: الزبون مو موجود.")
                continue
            row["customer_id"] = cid
        else:
            if not name:
                errors.append(f"{label}: اكتب اسم الزبون.")
                continue
            same = [c for c in db.all_customers(conn) if c["normalized_name"] == normalize(name)]
            if len(same) == 1:
                row["customer_id"] = same[0]["id"]
            elif len(same) > 1:
                errors.append(f"{label}: أكو أكثر من زبون بهذا الاسم، اختار واحد منهم.")
                continue
            else:
                row["new_customer"] = name
        out.append(row)
    return out, errors
