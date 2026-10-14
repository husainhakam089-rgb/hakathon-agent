"""Stage 22: Zain Cash payment links (simulated, and the real adapter), reminders, daily report."""

from datetime import date, datetime
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from app import daily, db, payments
from app.agent import Agent


@pytest.fixture
def ali(conn):
    cid = db.add_customer(conn, "أبو علي")
    db.add_transaction(conn, cid, "debt", 45000, note="طحين")
    return cid


@pytest.fixture
def client(conn, monkeypatch):
    from app import main as main_mod

    monkeypatch.delenv("ZAINCASH_MERCHANT_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setattr(main_mod, "agent", Agent(conn, llm=object(), log_path=None))
    return TestClient(main_mod.app)


# ---------- phones and the reminder text ----------

@pytest.mark.parametrize("raw,want", [("07701234567", "07701234567"), ("+964 770 123 4567", "07701234567"),
                                      ("009647701234567", "07701234567"), ("٠٧٧٠١٢٣٤٥٦٧", "07701234567"),
                                      ("7701234567", "07701234567"), ("0770123", None), ("06601234567", None), ("", None)])
def test_normalize_phone(raw, want):
    assert payments.normalize_phone(raw) == want


def test_whatsapp_link_carries_the_message():
    url = payments.whatsapp_url("07701234567", "هلا 45,000")
    assert url.startswith("https://wa.me/9647701234567?text=") and unquote(url.split("text=")[1]) == "هلا 45,000"
    assert payments.whatsapp_url(None, "x").startswith("https://wa.me/?text=")


def test_set_customer_phone(conn, tools, ali):
    assert tools.execute("set_customer_phone", {"customer_id": ali, "phone": "0770 123 4567"})["phone"] == "07701234567"
    assert db.get_customer(conn, ali)["phone"] == "07701234567"
    assert tools.execute("set_customer_phone", {"customer_id": ali, "phone": "123"})["error"] == "invalid_phone"


# ---------- links ----------

def test_link_is_for_all_he_owes_and_only_one_stays_open(conn, tools, ali):
    tools.base_url = "http://shop.example/"
    r = tools.execute("create_payment_link", {"customer_id": ali})
    assert r["ok"] and r["amount"] == 45000 and r["url"].startswith("http://shop.example/pay/")
    assert "تجربة" in r["provider"]
    again = tools.execute("create_payment_link", {"customer_id": ali, "amount": 20000})
    assert again["amount"] == 20000
    assert db.get_payment_link(conn, r["link_id"])["status"] == "cancelled"  # cannot pay the same debt twice


def test_link_refusals(conn, tools, ali):
    clean = db.add_customer(conn, "زبون صافي")
    assert tools.execute("create_payment_link", {"customer_id": clean})["error"] == "nothing_owed"
    assert tools.execute("create_payment_link", {"customer_id": ali, "amount": 100})["error"] == "invalid_amount"
    sup = db.add_customer(conn, "شركة", kind="supplier")
    assert not tools.execute("create_payment_link", {"customer_id": sup}).get("ok")


def test_base_url_env_wins(monkeypatch, tools, ali):
    monkeypatch.setenv("BASE_URL", "https://dafter.example.com/")
    tools.base_url = "http://127.0.0.1:8000/"
    assert tools.execute("create_payment_link", {"customer_id": ali})["url"].startswith("https://dafter.example.com/pay/")


def test_paying_records_the_payment_not_in_the_drawer(conn, tools, ali):
    tools.execute("set_drawer_opening", {"iqd": 0})
    link = db.get_payment_link(conn, tools.execute("create_payment_link", {"customer_id": ali})["link_id"])
    tx = db.pay_link(conn, link)
    assert db.get_balance(conn, ali)["IQD"] == 0
    assert db.pay_link(conn, db.get_payment_link(conn, link["id"])) == tx  # twice = once
    assert db.get_balance(conn, ali)["IQD"] == 0
    hist = db.get_history(conn, ali)
    assert hist[0]["type"] == "payment" and "زين كاش" in hist[0]["note"]
    c = tools.execute("count_drawer", {"iqd": 0})
    assert c["difference"]["IQD"] == 0  # electronic: the drawer did not get it
    a = db.alerts(conn)
    assert [(p["name"], p["amount"]) for p in a["paid_links"]] == [("أبو علي", 45000)]
    db.mark_announced(conn, [p["id"] for p in a["paid_links"]])
    assert db.alerts(conn)["paid_links"] == []


# ---------- reminders ----------

def test_draft_reminder_is_written_by_code_and_not_sent(conn, tools, ali):
    tools.base_url = "http://shop.example"
    db.set_setting(conn, "shop_name", "أسواق حسين")
    r = tools.execute("draft_reminder", {"customer_id": ali, "note": "وإذا تحب نص المبلغ هسه يكفي"})
    assert r["ok"] and "45,000" in r["text"] and r["url"] in r["text"] and "أسواق حسين" in r["text"]
    assert "نص المبلغ" in r["text"] and r["no_phone"] and r["last_reminded"] is None
    assert conn.execute("SELECT COUNT(*) FROM reminders").fetchone()[0] == 0  # the owner sends it, not the agent
    db.add_reminder(conn, ali, r["text"], r["link_id"])
    db.set_phone(conn, ali, "07701234567")
    again = tools.execute("draft_reminder", {"customer_id": ali})
    assert again["last_reminded"] and again["whatsapp"].startswith("https://wa.me/9647701234567") and "no_phone" not in again


def test_overdue_alerts_carry_phone_and_last_reminder(conn, ali):
    conn.execute("UPDATE transactions SET created_at = datetime('now','localtime','-40 days')")
    conn.commit()
    db.add_reminder(conn, ali, "x", None)
    o = db.alerts(conn)["overdue"][0]
    assert o["name"] == "أبو علي" and o["reminded_at"] and o["phone"] is None


# ---------- the real Zain Cash adapter (no network: a fake client) ----------

def test_jwt_round_trip_and_forgery():
    t = payments.jwt_encode({"status": "success", "orderid": "abc"}, "s3cret")
    assert payments.jwt_decode(t, "s3cret") == {"status": "success", "orderid": "abc"}
    assert payments.jwt_decode(t, "other") is None
    assert payments.jwt_decode("not.a.token", "s3cret") is None


def test_zaincash_checkout_request(monkeypatch):
    monkeypatch.setenv("ZAINCASH_MERCHANT_ID", "m1")
    monkeypatch.setenv("ZAINCASH_SECRET", "s3cret")
    monkeypatch.setenv("ZAINCASH_MSISDN", "9647835077893")
    assert payments.provider_name() == "zaincash"
    seen = {}

    class Fake:
        def post(self, url, data):
            seen.update(url=url, data=data)

            class R:
                text = ""

                def json(self):
                    return {"id": "tx77"}
            return R()

    url = payments.zaincash_checkout({"id": 1, "token": "tok", "amount": 45000}, "https://x/pay/tok/callback", Fake())
    assert url == "https://test.zaincash.iq/transaction/pay?id=tx77"
    assert seen["url"] == "https://test.zaincash.iq/transaction/init" and seen["data"]["merchantId"] == "m1"
    claims = payments.jwt_decode(seen["data"]["token"], "s3cret")
    assert claims["amount"] == 45000 and claims["orderId"] == "tok" and claims["redirectUrl"].endswith("/callback")


# ---------- endpoints ----------

def test_simulated_checkout_end_to_end(conn, client, ali):
    r = client.post("/payment-links", json={"customer_id": ali}).json()
    token = r["url"].rsplit("/", 1)[1]
    page = client.get(f"/pay/{token}").text
    assert "وضع تجربة" in page and "45,000" in page and "أبو علي" in page and "طحين" not in page  # no owner notes
    done = client.post(f"/pay/{token}/confirm").text
    assert "انسجل التسديد" in done and db.get_balance(conn, ali)["IQD"] == 0
    assert client.post(f"/pay/{token}/confirm").status_code == 200 and db.get_balance(conn, ali)["IQD"] == 0
    assert client.get("/alerts").json()["paid_links"][0]["amount"] == 45000
    assert client.get("/pay/nope").status_code == 404


def test_callback_refuses_a_forged_result(conn, client, ali, monkeypatch):
    for k, v in {"ZAINCASH_MERCHANT_ID": "m1", "ZAINCASH_SECRET": "s3cret", "ZAINCASH_MSISDN": "9647835077893"}.items():
        monkeypatch.setenv(k, v)
    token = client.post("/payment-links", json={"customer_id": ali}).json()["url"].rsplit("/", 1)[1]
    assert client.post(f"/pay/{token}/confirm").status_code == 400  # no test button on a real link
    forged = payments.jwt_encode({"status": "success", "orderid": token}, "wrong")
    assert client.get(f"/pay/{token}/callback", params={"token": forged}).status_code == 400
    assert db.get_balance(conn, ali)["IQD"] == 45000
    good = payments.jwt_encode({"status": "success", "orderid": token}, "s3cret")
    assert "انسجل التسديد" in client.get(f"/pay/{token}/callback", params={"token": good}).text
    assert db.get_balance(conn, ali)["IQD"] == 0


def test_reminder_endpoints(conn, client, ali):
    r = client.post("/reminders/draft", json={"customer_id": ali}).json()
    assert r["ok"] and "45,000" in r["text"]
    assert client.post(f"/customers/{ali}/phone", json={"phone": "07701234567"}).json()["phone"] == "07701234567"
    assert client.post(f"/customers/{ali}/phone", json={"phone": "12"}).status_code == 422
    assert client.post("/reminders/sent", json={"customer_id": ali, "text": r["text"], "link_id": r["link_id"]}).json()["ok"]
    assert db.last_reminder(conn, ali)["link_id"] == r["link_id"]
    assert client.post("/reminders/draft", json={"customer_id": 999}).status_code == 422


# ---------- the daily report ----------

def test_daily_report_once_a_day_after_its_hour(conn, ali):
    sent = []
    early = datetime.combine(date.today(), datetime.min.time()).replace(hour=10)
    assert daily.ensure_today(conn, early, send=lambda t: sent.append(t) or True) is None
    late = early.replace(hour=23)
    text = daily.ensure_today(conn, late, send=lambda t: sent.append(t) or True)
    assert text and sent == [text] and "ديون جديدة: 45,000 دينار" in text and "الجرار: ما انعد اليوم" in text
    assert daily.ensure_today(conn, late, send=lambda t: sent.append(t) or True) is None  # not twice
    assert daily.recent(conn)[0]["sent_via"] == "telegram"


def test_daily_report_endpoints_without_telegram(conn, client, ali):
    r = client.post("/daily-reports/now").json()
    assert r["ok"] and not r["sent"] and not r["telegram"] and "تقرير" in r["text"]
    assert client.get("/daily-reports").json()["reports"][0]["sent_via"] is None


def test_telegram_send(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "T")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    seen = {}

    class Fake:
        def post(self, url, json):
            seen.update(url=url, json=json)

            class R:
                status_code = 200
            return R()

    assert daily.send_telegram("هلا", Fake())
    assert seen["url"] == "https://api.telegram.org/botT/sendMessage" and seen["json"] == {"chat_id": "42", "text": "هلا"}


# ---------- the password once online ----------

def test_no_password_changes_nothing(client):
    assert client.get("/version").status_code == 200
    assert client.get("/alerts").status_code == 200


def test_password_guards_everything_but_pay_pages(conn, client, ali, monkeypatch):
    from app import auth

    monkeypatch.setenv("APP_PASSWORD", "sirr-123")
    auth._tries.clear()
    token = client.post("/payment-links", json={"customer_id": ali})
    assert token.status_code == 401
    assert client.get("/", headers={"accept": "text/html"}, follow_redirects=False).headers["location"] == "/login"
    assert client.get("/version").status_code == 200
    assert "كلمة السر غلط" in client.post("/login", data={"password": "no"}).text
    r = client.post("/login", data={"password": "sirr-123"}, follow_redirects=False)
    assert r.status_code == 303 and auth.COOKIE in r.cookies
    link = client.post("/payment-links", json={"customer_id": ali}).json()
    token = link["url"].rsplit("/", 1)[1]
    client.cookies.clear()
    assert client.get(f"/pay/{token}").status_code == 200  # the customer has no password
    assert client.get("/debtors").status_code == 401
    assert client.get(f"/statement/{ali}").status_code == 401  # the full statement stays the owner's


def test_login_is_slowed_after_five_wrong_tries(client, monkeypatch):
    from app import auth

    monkeypatch.setenv("APP_PASSWORD", "sirr-123")
    auth._tries.clear()
    for _ in range(5):
        client.post("/login", data={"password": "no"})
    assert "محاولات هواية" in client.post("/login", data={"password": "sirr-123"}).text
    auth._tries.clear()


def test_cookie_signature_and_expiry(monkeypatch):
    from app import auth

    monkeypatch.setenv("APP_PASSWORD", "a")
    c = auth.make_cookie()
    assert auth.valid_cookie(c) and not auth.valid_cookie(c + "0") and not auth.valid_cookie("x")
    assert not auth.valid_cookie(auth.make_cookie(now=0))  # expired
    monkeypatch.setenv("APP_PASSWORD", "b")
    assert not auth.valid_cookie(c)  # a new password logs everyone out
