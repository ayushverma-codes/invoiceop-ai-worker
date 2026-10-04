"""Entry point. Phase 1: `python run.py serve [--reset]` starts the mock company app."""
import sys


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if cmd == "serve":
        from mock_app.server import app, create_app
        create_app(reset_db="--reset" in sys.argv)
        print("Mock company app: http://127.0.0.1:5000/inbox  |  http://127.0.0.1:5000/ap")
        app.run(host="127.0.0.1", port=5000, debug=False)
    else:
        print(f"Unknown command: {cmd}. Available: serve [--reset]")
        sys.exit(1)


if __name__ == "__main__":
    main()
