"""Entry point.

  python run.py serve [--reset]              start the mock company app (keep this running)
  python run.py demo "<vendor>" [--headed]   Phase 2 scripted baseline through a real browser
  python run.py agent "<task>" [--headed]    Phase 3 LLM agent (needs GROQ_API_KEY in .env)
"""
import json
import sys

try:  # keep non-ASCII output (e.g. currency symbols) safe on Windows consoles
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if cmd == "serve":
        from mock_app.server import app, create_app
        create_app(reset_db="--reset" in sys.argv)
        print("Mock company app: http://127.0.0.1:5000/inbox  |  http://127.0.0.1:5000/ap")
        app.run(host="127.0.0.1", port=5000, debug=False)
    elif cmd == "demo":
        from tools.browser import Browser
        from tools.invoice import InvoiceError
        from tools.mechanical import process_latest_invoice
        args = [a for a in sys.argv[2:] if not a.startswith("--")]
        if not args:
            print('Usage: python run.py demo "<vendor>" [--headed]')
            sys.exit(1)
        with Browser(headless="--headed" not in sys.argv) as b:
            try:
                print(json.dumps(process_latest_invoice(b, args[0]), indent=2))
            except InvoiceError as e:
                print(f"Controlled failure: {e}")
                sys.exit(2)
    elif cmd == "agent":
        from dotenv import load_dotenv
        load_dotenv()
        from agent.graph import run_agent
        from agent.llm import GroqLLM, LLMError
        from agent.trace import Trace
        from tools.browser import Browser
        args = [a for a in sys.argv[2:] if not a.startswith("--")]
        if not args:
            print('Usage: python run.py agent "<task>" [--headed]')
            sys.exit(1)
        try:
            llm = GroqLLM()
        except LLMError as e:
            print(f"Setup problem: {e}")
            sys.exit(1)
        with Browser(headless="--headed" not in sys.argv) as b:
            result = run_agent(args[0], b, llm, trace=Trace(verbose=True))
        result.pop("actions_taken")
        print(json.dumps(result, indent=2))
        sys.exit(0 if result["status"] == "submitted_unverified" else 2)
    else:
        print(f"Unknown command: {cmd}. Available: serve [--reset], demo \"<vendor>\" [--headed], agent \"<task>\" [--headed]")
        sys.exit(1)


if __name__ == "__main__":
    main()
