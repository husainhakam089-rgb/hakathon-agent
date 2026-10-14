"""Stage 23: scanning the notebook page after page (one review table, each line keeps its page)."""

import pytest
from fastapi.testclient import TestClient

from app import db
from app import main as main_mod
from app.agent import Agent

JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


@pytest.fixture
def client(conn, monkeypatch, tmp_path):
    monkeypatch.setattr(main_mod, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(main_mod, "agent", Agent(conn, llm=object(), log_path=None))
    return TestClient(main_mod.app)


def test_rows_from_many_pages_keep_their_page(conn, client):
    ali = db.add_customer(conn, "أبو علي")
    r = client.post("/import/commit", json={"session_id": "s", "source_kind": "scan", "rows": [
        {"customer_id": ali, "amount": 25000, "currency": "IQD", "type": "debt", "page": 1},
        {"name": "سعد", "amount": 10000, "currency": "IQD", "type": "debt", "page": 3},
        {"name": "سعد", "amount": 4000, "currency": "IQD", "type": "payment", "page": 4},
    ]})
    assert r.status_code == 200 and r.json()["count"] == 3
    notes = [t["note"] for t in db.get_history(conn, ali)]
    assert notes == ["سكان الدفتر ص 1"]
    saad = next(c for c in db.all_customers(conn) if c["name"] == "سعد")  # one customer, from two pages
    assert db.get_balance(conn, saad["id"])["IQD"] == 6000
    assert sorted(t["note"] for t in db.get_history(conn, saad["id"])) == ["سكان الدفتر ص 3", "سكان الدفتر ص 4"]
    src = conn.execute("SELECT DISTINCT source FROM transactions").fetchall()
    assert [x[0] for x in src] == ["scan"]


def test_migrated_balances_from_a_scan(conn, client):
    client.post("/import/commit", json={"session_id": "s", "source_kind": "scan", "migrated": True, "rows": [
        {"name": "كريم", "amount": 70000, "currency": "IQD", "type": "debt", "page": 2}]})
    assert conn.execute("SELECT note FROM transactions").fetchone()[0] == "رصيد منقول من الدفتر · سكان الدفتر ص 2"


def test_old_imports_unchanged(conn, client):
    client.post("/import/commit", json={"session_id": "s", "migrated": True, "rows": [
        {"name": "كريم", "amount": 70000, "currency": "IQD", "type": "debt"}]})
    assert tuple(conn.execute("SELECT note, source FROM transactions").fetchone()) == ("رصيد منقول من الدفتر", "image")


def test_one_bad_row_saves_nothing(conn, client):
    r = client.post("/import/commit", json={"session_id": "s", "source_kind": "scan", "rows": [
        {"name": "كريم", "amount": 70000, "currency": "IQD", "type": "debt", "page": 1},
        {"name": "سعد", "amount": 5000, "currency": "IQD", "type": "", "page": 2}]})
    assert r.status_code == 422 and conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0


def test_scanned_pages_can_be_shown_again(client):
    up = client.post("/upload", content=JPG, headers={"X-File-Name": "page.jpg"}).json()
    r = client.get(f"/uploads/{up['file_id']}")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and r.content == JPG
    assert client.get("/uploads/" + "0" * 32).status_code == 404
    assert client.get("/uploads/..%2Fdebt.db").status_code == 404
    csv = client.post("/upload", content=b"name,amount\n", headers={"X-File-Name": "x.csv"}).json()
    assert client.get(f"/uploads/{csv['file_id']}").status_code == 404  # images only


def test_scan_screen_is_in_the_page(client):
    html = client.get("/").text
    assert 'id="scanBtn"' in html and "سكان الدفتر" in html and 'capture="environment"' in html


def test_a_name_written_as_null_is_empty():
    """Found by the scan eval: the model wrote the word "null" for a date line's name."""
    from app import extraction

    class LLM:
        def extract(self, *a):
            return {"rows": [{"name": "null", "amount": None, "raw_text": "١٤/١٠"},
                             {"name": "سعد", "amount": 20000, "currency": "IQD", "type": "debt"}]}

    rows = extraction.extract_file(LLM(), b"x", "image/jpeg")["rows"]
    assert rows[0]["name"] is None and rows[1]["name"] == "سعد"


def test_payment_words_left_in_the_name_make_it_a_payment():
    """Found by the scan eval: "سدد ابو علي 15" came back as a debt of "سد ابو علي"."""
    from app import extraction

    class LLM:
        def extract(self, *a):
            return {"rows": [
                {"name": "سد ابو علي", "amount": 15000, "amount_as_written": "15", "type": "debt", "confidence": "high"},
                {"name": "واصل من سعد", "amount": 10000, "type": "debt"},
                {"name": "جاسم", "amount": 20000, "amount_as_written": "-20", "type": "debt"},
                {"name": "سعد", "amount": 20000, "amount_as_written": "20", "type": "debt"},
                {"name": "سدير", "amount": 5000, "type": "debt"}]}

    rows = extraction.extract_file(LLM(), b"x", "image/jpeg")["rows"]
    assert [(r["name"], r["type"]) for r in rows] == [
        ("ابو علي", "payment"), ("سعد", "payment"), ("جاسم", "payment"), ("سعد", "debt"), ("سدير", "debt")]
    assert rows[0]["confidence"] == "medium"
