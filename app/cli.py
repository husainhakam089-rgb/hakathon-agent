"""Terminal chat with the agent: python -m app.cli"""

import json
import uuid

from dotenv import load_dotenv

HELP = """الأوامر:
  /tools   تشغيل/إطفاء عرض تفاصيل الأدوات (المدخلات والنتائج)
  /new     محادثة جديدة (ينمسح تاريخ المحادثة، والبيانات تبقى)
  /exit    خروج"""


def main() -> None:
    load_dotenv()
    from app import db
    from app.agent import Agent

    agent = Agent(db.connect())
    session_id = uuid.uuid4().hex
    verbose = True
    print("دفتر الديون - اكتب رسالتك.")
    print(HELP)

    while True:
        try:
            message = input("\nإنت> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not message:
            continue
        if message == "/exit":
            break
        if message == "/new":
            session_id = uuid.uuid4().hex
            print("بدينا محادثة جديدة.")
            continue
        if message == "/tools":
            verbose = not verbose
            print("عرض الأدوات:", "شغال" if verbose else "مطفي")
            continue

        result = agent.chat(session_id, message)
        for call in result["tool_calls"]:
            status = "✓" if call["result"].get("ok") else "✗"
            print(f"  [{status} {call['tool']}] {json.dumps(call['input'], ensure_ascii=False)}")
            if verbose:
                print(f"      → {json.dumps(call['result'], ensure_ascii=False)}")
        print(f"الوكيل> {result['reply']}")


if __name__ == "__main__":
    main()
