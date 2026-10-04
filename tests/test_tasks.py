"""Phase 2 end-to-end tests: scripted workflow through a REAL browser against a REAL local server.

Each test gets its own server process and its own temp SQLite DB, so tests are independent.
"""
import contextlib
import os
import pathlib
import re
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request

import pytest

from tests.helpers import stop_process
from tools.browser import Browser
from tools.invoice import AmbiguousSelection, InvoiceData, InvoiceValidationError, VendorNotFound
from tools.mechanical import process_latest_invoice

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def app(tmp_path):
    port, db = _free_port(), tmp_path / "test.db"
    env = {**os.environ, "PORT": str(port), "INVOICEOP_DB": str(db)}
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "mock_app" / "server.py"), "--reset"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            urllib.request.urlopen(url + "/inbox", timeout=1)
            break
        except OSError:
            time.sleep(0.1)
    else:
        proc.kill()
        pytest.fail("mock app did not start")
    yield {"url": url, "db": str(db)}
    stop_process(proc)


@pytest.fixture
def browser(app):
    with Browser(base_url=app["url"]) as b:
        yield b


def ap_rows(db):
    with contextlib.closing(sqlite3.connect(db)) as c:
        return c.execute("SELECT invoice_id, vendor, amount, invoice_date, due_date, status FROM invoices").fetchall()


# --- browser tools -------------------------------------------------------
def test_inspect_page_sees_all_inbox_invoices(browser):
    browser.navigate("/inbox")
    obs = browser.inspect_page()
    table = obs["tables"][0]
    assert len(table["rows"]) == 6
    assert {"Invoice", "Vendor", "Invoice date", "Amount"} <= set(table["headers"])


# --- end-to-end ----------------------------------------------------------
def test_latest_acme_invoice_end_to_end(browser, app):
    r = process_latest_invoice(browser, "Acme Corp")
    assert r["status"] == "submitted"
    assert r["invoice"] == {
        "invoice_id": "AC-2026-104", "vendor": "Acme Corp", "amount": 125000,
        "invoice_date": "2026-10-02", "due_date": "2026-10-30",
    }
    # newest chosen over AC-2026-103 and AC-2026-101, purely from observed dates
    assert [c[0] for c in r["selection_evidence"]["candidates"]] == ["AC-2026-104", "AC-2026-103", "AC-2026-101"]
    # state really persisted in SQLite (direct DB read in the test, not via the UI)
    assert ap_rows(app["db"]) == [("AC-2026-104", "Acme Corp", 125000, "2026-10-02", "2026-10-30", "submitted")]


def test_latest_globex_invoice_same_code_different_data(browser, app):
    r = process_latest_invoice(browser, "Globex")  # partial vendor name works
    assert r["status"] == "submitted"
    assert r["invoice"]["invoice_id"] == "GX-2026-221"
    assert [row[0] for row in ap_rows(app["db"])] == ["GX-2026-221"]


def test_unknown_vendor_is_a_controlled_failure(browser, app):
    with pytest.raises(VendorNotFound):
        process_latest_invoice(browser, "Umbrella Corp")
    assert ap_rows(app["db"]) == []


def test_duplicate_submission_is_reported_as_rejected_not_success(browser, app):
    assert process_latest_invoice(browser, "Acme Corp")["status"] == "submitted"
    r = process_latest_invoice(browser, "Acme Corp")
    assert r["status"] == "rejected"
    assert any("already exists" in e for e in r["ui_errors"])
    assert len(ap_rows(app["db"])) == 1


# --- unit-level guards ---------------------------------------------------
def test_invoice_data_rejects_bad_extraction():
    with pytest.raises(InvoiceValidationError, match="missing field: due_date"):
        InvoiceData.from_key_values({"Invoice ID": "X", "Vendor": "V", "Amount": "10", "Invoice date": "2026-01-01"})
    with pytest.raises(InvoiceValidationError, match="not an ISO date"):
        InvoiceData.from_key_values(
            {"Invoice ID": "X", "Vendor": "V", "Amount": "10", "Invoice date": "02/10/2026", "Due date": "2026-01-01"})


def test_no_vendor_specific_code_in_tools():
    """Generalization guard: vendor names must not appear in tool/agent source."""
    pattern = re.compile(r"acme|globex|initech", re.IGNORECASE)
    for folder in ("tools", "agent"):
        for path in (ROOT / folder).rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            assert not pattern.search(text), f"vendor name hardcoded in {path.name}"
