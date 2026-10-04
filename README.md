# invoiceop-ai-worker

Autonomous AI worker for invoice processing (work in progress, Phase 2 of 5 complete).

- Phase 1: mock company environment (vendor invoice portal + internal AP system, SQLite).
- Phase 2: real browser tools (Playwright) and a scripted end-to-end baseline. No LLM yet.

## Setup (Windows, PowerShell)

```powershell
cd invoiceop-ai-worker
python --version                  # expect 3.14
python -m venv .venv
.venv\Scripts\Activate.ps1        # if blocked: Set-ExecutionPolicy -Scope Process Bypass
pip install -r requirements.txt
playwright install chromium       # one-time browser download (~150 MB)
```

macOS/Linux: `source .venv/bin/activate` instead of the Activate line.

## Run

Terminal 1 - the mock company app (keep it running):

```powershell
python run.py serve --reset       # --reset re-creates and re-seeds the database
```

Browse it: http://127.0.0.1:5000/inbox, /ap, /ap/create.

Terminal 2 - scripted browser demo (add `--headed` to watch the browser work):

```powershell
python run.py demo "Acme Corp" --headed
python run.py demo "Globex"
python run.py demo "Umbrella"     # unknown vendor -> controlled failure
```

Run `serve --reset` again between demos, otherwise AP rejects the duplicate (that is expected behaviour).

## Tests

```powershell
pytest -q
```

Each test starts its own server on a free port with a temp database and drives a real headless Chromium. Nothing needs to be running beforehand.

## Optional environment variables

| Variable | Purpose |
| --- | --- |
| `MOCK_APP_URL` | base URL the browser uses (default `http://127.0.0.1:5000`) |
| `INVOICEOP_HEADLESS` | `0` to show the browser window |
| `INVOICEOP_CHROMIUM_PATH` | use an existing Chrome/Chromium binary |
| `INVOICEOP_DB` | SQLite file path used by the mock app |

## Layout

```
tools/browser.py     navigate, inspect_page, click, fill, submit (Playwright)
tools/invoice.py     InvoiceData validation + generic vendor filtering / latest selection
tools/mechanical.py  scripted baseline workflow (replaced by the LLM agent in Phase 3)
mock_app/            Flask app: server.py, database.py, templates/
agent/               (Phase 3+) state, nodes, prompts, graph
tests/               end-to-end tests
run.py               entry point
```
