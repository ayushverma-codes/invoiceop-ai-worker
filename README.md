# invoiceop-ai-worker

Autonomous AI worker for invoice processing (work in progress, Phase 4 of 5 complete).

- Phase 1: mock company environment (vendor invoice portal + internal AP system, SQLite).
- Phase 2: real browser tools (Playwright) and a scripted end-to-end baseline. No LLM yet.
- Phase 3: LLM-driven agent loop (observe -> reason -> act) over the same browser tools.
- Phase 4: observation-based recovery with bounded retries, deterministic approval policy, independent database verifier.

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

## Run the LLM agent (Phase 3)

```powershell
copy .env.example .env            # then put your Groq key in GROQ_API_KEY
python run.py serve --reset       # terminal 1
python run.py agent "Process the latest invoice from Acme Corp." --headed    # terminal 2
python run.py agent "Process the latest invoice from Globex Inc."
```

The run prints a tagged trace ([PLAN] [OBSERVE] [DECISION] [EVIDENCE] [ACTION] [EXTRACT] [ERROR] [RESULT])
and a JSON result. Final statuses: `completed` (only after the independent DB verifier passes every check), `failed`,
`waiting_for_approval`, `needs_clarification`. Reset the DB between runs (duplicates are rejected).

### Phase 4 demo scenarios

```powershell
# B. recovery: AP rejects the first submission with "Invoice date is required."
python run.py serve --reset --fault invoice_date_once
python run.py agent "Process the latest invoice from Acme Corp."

# C. human approval (Initech's invoice is 500000 > 200000): prompts Approve? [y/n] before anything is written
python run.py serve --reset
python run.py agent "Process the latest invoice from Initech LLC."
#   add --no-approver to stop with status waiting_for_approval instead of prompting

# D. the UI says success but AP stored a wrong amount: verifier catches it -> failed, never completed
python run.py serve --reset --fault store_wrong_amount
python run.py agent "Process the latest invoice from Acme Corp."
```

Other faults: `invoice_date_always` (AP never accepts -> retries are bounded, then `failed`).

**Safety design.** The LLM proposes one action per turn; deterministic code decides what is allowed:
- policy gate inside `submit` (amount > `APPROVAL_THRESHOLD` -> human approval first); form buttons cannot be clicked directly, so it cannot be bypassed
- after an AP rejection the agent must change something (`fill_form`) before resubmitting; at most `MAX_RETRIES` retries
- `finish(done)` only makes the *claim*; `tools/verification.py` reads SQLite read-only and checks existence, vendor, amount, dates, status, agreement with the source portal record, and that approval was respected

How it works: the LLM returns ONE structured action per turn (decision + short evidence + action). Deterministic
guards validate it before execution: invoice must be on the observed list and belong to the requested vendor,
extracted values must be visible on the page, form values must match the extraction, navigation is limited to the
intranet, `finish(done)` is refused unless the page confirmed the submission, and the loop is bounded (step budget,
repeat detector, consecutive-failure limit, schema-retry limit).

## Tests

```powershell
pytest -q
```

`tests/test_agent.py` uses a deterministic LLM test double: it verifies the loop, guards and bounds through the real
browser, not the quality of the real model. Use `python run.py agent ...` for that.

Each test starts its own server on a free port with a temp database and drives a real headless Chromium. Nothing needs to be running beforehand.

## Optional environment variables

| Variable | Purpose |
| --- | --- |
| `MOCK_APP_URL` | base URL the browser uses (default `http://127.0.0.1:5000`) |
| `INVOICEOP_HEADLESS` | `0` to show the browser window |
| `INVOICEOP_CHROMIUM_PATH` | use an existing Chrome/Chromium binary |
| `INVOICEOP_DB` | SQLite file path used by the mock app |
| `GROQ_API_KEY` | required for `run.py agent` |
| `LLM_MODEL` | default `openai/gpt-oss-20b` |
| `LLM_REASONING_EFFORT` | `low` (default) / `medium` / `high` |
| `APPROVAL_THRESHOLD` | default `200000`; amounts strictly above need approval |
| `MAX_RETRIES` | default `3`; AP rejections tolerated |
| `INVOICEOP_FAULT` | set by `serve --fault X`: `invoice_date_once`, `invoice_date_always`, `store_wrong_amount` |

## Layout

```
tools/browser.py     navigate, inspect_page, click, fill, submit (Playwright)
tools/policy.py      approval threshold + CLI approver (deterministic)
tools/verification.py independent read-only SQLite verifier (PASS/FAIL checks)
tools/invoice.py     InvoiceData validation + generic vendor filtering / latest selection
tools/mechanical.py  scripted baseline workflow (replaced by the LLM agent in Phase 3)
mock_app/            Flask app: server.py, database.py, templates/
agent/schema.py      strict parsing/validation of LLM output (Step, Plan)
agent/llm.py         Groq client (rate-limit retries) + structured() retry helper
agent/state.py       AgentState
agent/prompts.py     planner + agent prompts (no vendor-specific content)
agent/nodes.py       plan / observe / reason / act nodes and the guards
agent/graph.py       state machine + run_agent()
tests/fake_llm.py    deterministic LLM test doubles
tests/               end-to-end tests
run.py               entry point
```
