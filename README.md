# invoiceop-ai-worker

Autonomous AI worker for invoice processing (work in progress, Phase 1 of 5 complete).
Phase 1 provides the mock company environment: a vendor invoice portal and an internal AP system backed by SQLite. The agent comes in later phases.

## Quick start (Windows, PowerShell)

```powershell
cd invoiceop-ai-worker
python --version                  # expect 3.14 (3.12+ should also work)
python -m venv .venv
.venv\Scripts\Activate.ps1        # if blocked: Set-ExecutionPolicy -Scope Process Bypass
pip install -r requirements.txt
python run.py serve --reset       # --reset re-creates and re-seeds the database
```

macOS/Linux: `source .venv/bin/activate` instead of the Activate line.

Then open in a browser:

- http://127.0.0.1:5000/inbox - vendor invoice portal (6 seeded invoices)
- http://127.0.0.1:5000/invoice/AC-2026-104 - invoice detail
- http://127.0.0.1:5000/ap - internal AP system (empty at first)
- http://127.0.0.1:5000/ap/create - create an AP invoice (submit with a blank invoice date to see the validation error)

Stop the server with Ctrl+C. Data is stored in `mock_app/company.db` and persists across restarts; use `--reset` to wipe it.

## Layout

```
agent/        (Phase 3+) LLM agent: state, nodes, prompts, graph
tools/        (Phase 2+) browser, policy, verification, invoice tools
mock_app/     Flask app: server.py, database.py, templates/
tests/        (Phase 2+)
run.py        entry point
```
