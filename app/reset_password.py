"""Forgot the password set from ☰ ← 🔒? Run this on the shop's computer itself (whoever can
run it already has the notebook file, so it gives nothing away):

    python -m app.reset_password

It takes the password off; open the program and set a new one from ☰ ← 🔒.
(A password in .env as APP_PASSWORD is changed in that file instead.)
"""

from dotenv import load_dotenv

load_dotenv()

from app import db  # noqa: E402


def main() -> None:
    conn = db.connect()
    db.set_setting(conn, "password_hash", "")
    print("انشالت كلمة السر. شغّل البرنامج وحط وحدة جديدة من ☰ ← 🔒 كلمة السر.")


if __name__ == "__main__":
    main()
