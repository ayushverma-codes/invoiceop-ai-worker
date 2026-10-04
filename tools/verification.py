"""Independent verifier: reads the AP database directly, read-only, with no input from the LLM.

It only receives the invoice data the agent intended to record. It returns explicit PASS/FAIL checks,
and the task may be reported "completed" only if every check passes. A browser/UI success message is
never evidence on its own.
"""
import contextlib
import pathlib
import sqlite3

from mock_app.database import DB_PATH
from tools.policy import requires_approval

FIELDS = ("vendor", "amount", "invoice_date", "due_date")
CHECKS = ("database_readable", "invoice_exists", "vendor_matches", "amount_matches",
          "invoice_date_matches", "due_date_matches", "status_confirmed",
          "matches_source_record", "approval_respected")


def verify_submission(db_path, invoice, approval_status=None):
    """Compare the stored AP record with `invoice` (and with the source portal record)."""
    checks, details = {}, {}

    def record(name, ok, detail=""):
        checks[name] = "PASS" if ok else "FAIL"
        if not ok:
            details[name] = detail

    db_path = db_path or DB_PATH
    try:
        uri = pathlib.Path(db_path).resolve().as_uri() + "?mode=ro"
        with contextlib.closing(sqlite3.connect(uri, uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            ap = conn.execute("SELECT * FROM invoices WHERE invoice_id = ?", (invoice["invoice_id"],)).fetchone()
            src = conn.execute("SELECT * FROM inbox_invoices WHERE invoice_id = ?", (invoice["invoice_id"],)).fetchone()
        record("database_readable", True)
    except (sqlite3.Error, OSError) as e:
        record("database_readable", False, str(e))
        ap = src = None

    record("invoice_exists", ap is not None, "no AP record with this invoice_id")
    for f in FIELDS:
        name = f"{f}_matches"
        if ap is None:
            record(name, False, "no AP record")
        else:
            record(name, str(ap[f]) == str(invoice[f]), f"AP has {ap[f]!r}, expected {invoice[f]!r}")
    record("status_confirmed", ap is not None and ap["status"] == "submitted",
           f"status is {ap['status']!r}" if ap is not None else "no AP record")
    record("matches_source_record",
           src is not None and all(str(src[f]) == str(invoice[f]) for f in FIELDS),
           "extracted data differs from the source invoice" if src is not None else "source invoice not found")
    record("approval_respected", (not requires_approval(invoice)) or approval_status == "approved",
           f"amount needs approval but approval_status={approval_status!r}")
    ordered = {name: checks[name] for name in CHECKS}
    return {"passed": all(v == "PASS" for v in ordered.values()), "checks": ordered, "details": details}
