"""Checks for Hussein's match_extracted_rows (PLAN_v1_media.md 6.7).

Run only these:   py -m pytest tests/test_match_extracted_rows.py -v
Skipped until the function is written.
"""

import copy

import pytest

from app import db
from app.extraction import match_extracted_rows


@pytest.fixture
def shop(conn):
    ids = {n: db.add_customer(conn, n) for n in ["أبو علي", "أحمد علي", "أحمد حسن", "سعد"]}
    return conn, ids


def run(conn, names):
    rows = [{"name": n, "amount": 1000, "raw_text": f"{n} 1"} for n in names]
    try:
        return match_extracted_rows(conn, rows)
    except NotImplementedError:
        pytest.skip("match_extracted_rows بعدها مو مكتوبة")


def test_one_customer_is_existing(shop):
    conn, ids = shop
    [row] = run(conn, ["ابو علي"])  # different spelling, same person
    assert row["match"] == {"status": "existing", "customer_id": ids["أبو علي"]}


def test_two_customers_is_ambiguous(shop):
    conn, ids = shop
    [row] = run(conn, ["أحمد"])
    assert row["match"]["status"] == "ambiguous"
    got = sorted((c["id"], c["name"]) for c in row["match"]["candidates"])
    assert got == sorted([(ids["أحمد علي"], "أحمد علي"), (ids["أحمد حسن"], "أحمد حسن")])


def test_unknown_name_is_new(shop):
    conn, _ = shop
    [row] = run(conn, ["كريم"])
    assert row["match"] == {"status": "new"}


def test_missing_name_is_new(shop):
    conn, _ = shop
    rows = [{"name": None, "amount": None, "raw_text": "سطر ممسوح"}]
    try:
        [row] = match_extracted_rows(conn, rows)
    except NotImplementedError:
        pytest.skip("match_extracted_rows بعدها مو مكتوبة")
    assert row["match"] == {"status": "new"}


def test_keeps_order_and_fields_and_writes_nothing(shop):
    conn, _ = shop
    names = ["سعد", "كريم", "ابو علي"]
    rows = run(conn, names)
    assert [r["name"] for r in rows] == names
    assert all(r["amount"] == 1000 and r["raw_text"] for r in rows)
    assert len(db.all_customers(conn)) == 4
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
