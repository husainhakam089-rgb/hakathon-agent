"""Stage 7: Excel / CSV import (read by code; the model only suggests column roles)."""

import io

import openpyxl
import pytest
from fastapi.testclient import TestClient

from app import db, spreadsheet
from app import main as main_mod
from app.agent import Agent


def xlsx(rows) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------- reading ----------

def test_read_xlsx_skips_empty_rows():
    data = xlsx([[], ["الاسم", "المبلغ"], ["أبو علي", 25000], [None, None], ["سعد", 7500]])
    headers, rows = spreadsheet.read_table(data, "xlsx")
    assert headers == ["الاسم", "المبلغ"]
    assert rows == [["أبو علي", 25000], ["سعد", 7500]]


@pytest.mark.parametrize("encoding, sep", [("utf-8-sig", ","), ("cp1256", ";"), ("utf-8", "\t")])
def test_read_csv_encodings_and_separators(encoding, sep):
    text = f"الاسم{sep}المبلغ\nأبو علي{sep}25000\nسعد{sep}7500\n"
    headers, rows = spreadsheet.read_table(text.encode(encoding), "csv")
    assert headers == ["الاسم", "المبلغ"] and rows[1] == ["سعد", "7500"]


def test_read_errors():
    with pytest.raises(spreadsheet.SpreadsheetError):
        spreadsheet.read_table(xlsx([["الاسم", "المبلغ"]]), "xlsx")  # header only
    with pytest.raises(spreadsheet.SpreadsheetError):
        spreadsheet.read_table(b"PK\x03\x04 not really", "xlsx")
    big = [["الاسم", "المبلغ"]] + [[f"زبون {i}", 1000] for i in range(501)]
    with pytest.raises(spreadsheet.SpreadsheetError, match="500"):
        spreadsheet.read_table(xlsx(big), "xlsx")


# ---------- mapping ----------

def test_known_column_names_map_directly():
    m = spreadsheet.guess_mapping(["اسم الزبون", "المبلغ", "العملة", "ملاحظات"])
    assert m == {"name": "اسم الزبون", "amount": "المبلغ", "currency": "العملة", "type": None}
    assert spreadsheet.guess_mapping(["Customer", "Balance"])["amount"] == "Balance"


def test_odd_column_names_are_not_guessed():
    m = spreadsheet.guess_mapping(["صاحب الحساب", "الباقي"])
    assert not spreadsheet.is_complete(m)


# ---------- rows ----------

def test_amounts_currencies_and_types():
    headers = ["الاسم", "المبلغ", "العملة", "النوع"]
    rows = [
        ["أبو علي", "25,000", "دينار", "دين"],
        ["سعد", "٧٥٠٠", "", ""],
        ["كريم", "50 $", None, "واصل"],
        ["زينب", 12.5, "IQD", "دين"],      # not a whole number: low confidence
        ["حسن", 1000, "يورو", "دين"],       # unknown currency
        [None, None, None, None],          # empty line, skipped
    ]
    out = spreadsheet.rows_from_table(headers, rows, {"name": "الاسم", "amount": "المبلغ",
                                                      "currency": "العملة", "type": "النوع"})["rows"]
    got = [(r["name"], r["amount"], r["currency"], r["type"], r["confidence"]) for r in out]
    assert got == [
        ("أبو علي", 25000, "IQD", "debt", "high"),
        ("سعد", 7500, "IQD", "debt", "high"),       # empty type cell: debt (balances file)
        ("كريم", 50, "USD", "payment", "high"),
        ("زينب", None, "IQD", "debt", "low"),
        ("حسن", 1000, "unknown", "debt", "low"),
    ]


def test_negative_balance_is_a_payment_without_type_column():
    out = spreadsheet.rows_from_table(["n", "a"], [["سعد", -5000]], {"name": "n", "amount": "a"})["rows"]
    assert (out[0]["amount"], out[0]["type"]) == (5000, "payment")


# ---------- endpoints ----------

class FakeLLM:
    def __init__(self, suggestion=None):
        self.suggestion = suggestion
        self.texts = []

    def extract(self, system, text, file_bytes, mime, tool):
        assert file_bytes is None  # only column names and a few rows, never the file
        self.texts.append(text)
        return self.suggestion


@pytest.fixture
def client(conn, monkeypatch, tmp_path):
    monkeypatch.setattr(main_mod, "UPLOAD_DIR", tmp_path / "uploads")

    def make(llm=None):
        monkeypatch.setattr(main_mod, "agent", Agent(conn, llm=llm or FakeLLM(), log_path=None))
        return TestClient(main_mod.app)
    return make


def upload(c, data, name):
    from urllib.parse import quote
    # headers are ASCII only: the page sends encodeURIComponent(name), the server unquotes
    return c.post("/upload", content=data, headers={"X-File-Name": quote(name)})


def commit_all(c, review, migrated=True):
    rows = [{"name": r["name"], "customer_id": (r["match"].get("customer_id")), "amount": r["amount"],
             "currency": r["currency"], "type": r["type"]} for r in review["rows"] if r["selected"]]
    return c.post("/import/commit", json={"session_id": "s", "file_id": review["file_id"],
                                          "source_kind": "file", "migrated": migrated, "rows": rows})


def test_upload_kinds(client):
    c = client()
    assert upload(c, xlsx([["الاسم", "المبلغ"], ["a", 1]]), "x.xlsx").json()["kind"] == "xlsx"
    assert upload(c, "الاسم,المبلغ\nسعد,1\n".encode(), "ديون.csv").json()["kind"] == "csv"
    assert upload(c, b"plain text", "notes.txt").status_code == 415
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", "<w/>")
    assert upload(c, buf.getvalue(), "letter.docx").status_code == 415  # a zip, but not Excel


def test_twenty_customers_import_fully(client, conn):
    """PLAN_v1_media.md 7.3: an Excel file with 20 customers moves over complete and correct."""
    existing = db.add_customer(conn, "أبو علي")
    people = [("أبو علي", 25000, "دينار")] + [(f"زبون رقم {i}", 1000 * i, "دينار") for i in range(1, 18)] \
        + [("أبو زينب", 50, "دولار"), ("أم حسين", 80000, "دينار")]
    assert len(people) == 20
    c = client()
    file_id = upload(c, xlsx([["الاسم", "المبلغ", "العملة"], *people]), "ديون.xlsx").json()["file_id"]

    info = c.post("/import/file", json={"file_id": file_id}).json()
    assert info["mapping_source"] == "known" and not info["needs_confirmation"]
    review = c.post("/import/file/rows", json={"file_id": file_id, "mapping": info["mapping"]}).json()
    assert len(review["rows"]) == 20 and all(r["selected"] for r in review["rows"])
    assert review["rows"][0]["match"] == {"status": "existing", "customer_id": existing}
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0  # nothing before commit

    r = commit_all(c, review)
    assert r.status_code == 200 and r.json()["count"] == 20
    balances = {b["name"]: b["balances"] for b in db.all_balances(conn)}
    assert len(balances) == 20
    for name, amount, cur in people:
        assert balances[name]["USD" if cur == "دولار" else "IQD"] == amount
    src = conn.execute("SELECT DISTINCT source, note FROM transactions").fetchall()
    assert [tuple(x) for x in src] == [(f"file:{file_id}", main_mod.MIGRATED_NOTE)]


def test_odd_column_names_after_confirmation(client, conn):
    """PLAN_v1_media.md 7.3: "صاحب الحساب" / "الباقي" are matched after the owner agrees."""
    data = xlsx([["التسلسل", "صاحب الحساب", "الباقي", "ملاحظة"],
                 [1, "أبو علي", 25000, ""], [2, "سعد", 7500, "قديم"],
                 [3, "كريم", 3000, ""], [4, "سرّي جدًا", 999, ""]])
    llm = FakeLLM({"name_column": "صاحب الحساب", "amount_column": "الباقي",
                   "currency_column": "none", "type_column": "none"})
    c = client(llm)
    file_id = upload(c, data, "حسابات.xlsx").json()["file_id"]
    info = c.post("/import/file", json={"file_id": file_id}).json()
    assert info["needs_confirmation"] and info["mapping_source"] == "model"
    assert info["mapping"] == {"name": "صاحب الحساب", "amount": "الباقي", "currency": None, "type": None}
    assert "سرّي جدًا" not in llm.texts[0]  # only the first 3 rows go to the model
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0

    # the owner confirms (here: as suggested) -> review -> commit
    review = c.post("/import/file/rows", json={"file_id": file_id, "mapping": info["mapping"]}).json()
    assert [r["amount"] for r in review["rows"]] == [25000, 7500, 3000, 999]
    assert commit_all(c, review).json()["count"] == 4


def test_model_unavailable_owner_picks_by_hand(client):
    from app.llm import LLMError

    class Broken(FakeLLM):
        def extract(self, *a):
            raise LLMError("خلصت الحصة", "429")
    c = client(Broken())
    file_id = upload(c, xlsx([["أ", "ب"], ["سعد", 1]]), "x.xlsx").json()["file_id"]
    info = c.post("/import/file", json={"file_id": file_id}).json()
    assert info["mapping_source"] == "none" and info["needs_confirmation"]


def test_bad_mappings_rejected(client):
    c = client()
    file_id = upload(c, xlsx([["الاسم", "المبلغ"], ["سعد", 1]]), "x.xlsx").json()["file_id"]
    post = lambda m: c.post("/import/file/rows", json={"file_id": file_id, "mapping": m})  # noqa: E731
    assert post({"name": "الاسم", "amount": None}).status_code == 422
    assert post({"name": "الاسم", "amount": "الاسم"}).status_code == 422
    assert post({"name": "الاسم", "amount": "غير موجود"}).status_code == 422
    photo = upload(c, b"\xff\xd8\xff" + b"0" * 50, "p.jpg").json()["file_id"]
    assert c.post("/import/file", json={"file_id": photo}).status_code == 404
