"""Security review 15-10-2026: each test replays an attack that worked before the fix."""

import pytest
from fastapi.testclient import TestClient

from app import db, security
from app import main as main_mod
from app.agent import Agent


@pytest.fixture
def client(conn, monkeypatch):
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    monkeypatch.setattr(main_mod, "agent", Agent(conn, llm=object(), log_path=None))
    a = db.add_customer(conn, "أبو علي")
    db.add_transaction(conn, a, "debt", 100000)
    return TestClient(main_mod.app)


def test_dns_rebinding_cannot_read_the_notebook(client):
    """A web page pointed its own name at 127.0.0.1 and downloaded the whole notebook."""
    for host in ("evil.example:8000", "evil.example", "127.0.0.1.evil.example"):
        r = client.get("/debtors", headers={"Host": host})
        assert r.status_code == 400 and "أبو علي" not in r.text
        assert client.get("/backups", headers={"Host": host}).status_code == 400
    for host in ("127.0.0.1:8000", "localhost:8000", "[::1]:8000", "localhost"):
        assert client.get("/debtors", headers={"Host": host}).status_code == 200


def test_with_a_password_any_host_is_served(client, monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "pw")
    assert client.get("/version", headers={"Host": "shop.onrender.com"}).status_code == 200
    assert client.get("/debtors", headers={"Host": "shop.onrender.com"}).status_code == 401  # the cookie protects


def test_another_site_cannot_post(client):
    """A plain form on another site could make a backup, send the daily report, confirm a payment."""
    for url in ("/backups", "/daily-reports/now", "/payment-links/1/confirm", "/payment-links/1/reject"):
        assert client.post(url, headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post("/backups", headers={"Origin": "null"}).status_code == 403
    assert client.post("/backups", headers={"Origin": "http://testserver"}).json()["ok"]  # the program itself
    assert client.post("/backups").status_code == 200  # no Origin: not a browser page
    assert client.get("/debtors", headers={"Origin": "https://evil.example"}).status_code == 200  # reading is CORS's job


def test_pages_cannot_be_framed_or_sniffed(client):
    for url in ("/", "/debtors", "/login"):
        h = client.get(url).headers
        assert h["x-frame-options"] == "DENY" and "frame-ancestors 'none'" in h["content-security-policy"]
        assert h["x-content-type-options"] == "nosniff" and h["referrer-policy"] == "same-origin"


def test_host_parsing():
    assert security._host_only("[::1]:8000") == "::1" and security._host_only("LocalHost:80") == "localhost"
    assert security.same_origin(None, "a") and not security.same_origin("null", "a")
    assert security.same_origin("http://127.0.0.1:8000", "127.0.0.1:8000")
    assert not security.same_origin("http://127.0.0.1:9999", "127.0.0.1:8000")
