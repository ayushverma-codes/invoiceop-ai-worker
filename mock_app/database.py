"""SQLite persistence for the mock company environment.

Two separate tables model two separate systems:
- inbox_invoices: the vendor invoice portal (source documents the agent READS)
- invoices:       the internal AP system (records the agent WRITES)
"""
import os
import sqlite3

DB_PATH = os.environ.get(
    "INVOICEOP_DB",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "company.db"),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS inbox_invoices (
    invoice_id   TEXT PRIMARY KEY,
    vendor       TEXT NOT NULL,
    amount       INTEGER NOT NULL,
    invoice_date TEXT NOT NULL,
    due_date     TEXT NOT NULL,
    description  TEXT
);
CREATE TABLE IF NOT EXISTS invoices (
    invoice_id   TEXT PRIMARY KEY,
    vendor       TEXT NOT NULL,
    amount       INTEGER NOT NULL,
    invoice_date TEXT NOT NULL,
    due_date     TEXT NOT NULL,
    status       TEXT NOT NULL
);
"""

# Deliberately unordered, with multiple vendors, so "latest" must come from dates.
SEED_INBOX = [
    ("AC-2026-104", "Acme Corp", 125000, "2026-10-02", "2026-10-30", "Q3 industrial supplies"),
    ("GX-2026-221", "Globex Inc", 87500, "2026-09-28", "2026-10-28", "Consulting services"),
    ("AC-2026-103", "Acme Corp", 98000, "2026-09-05", "2026-10-05", "Q2 maintenance contract"),
    ("GX-2026-219", "Globex Inc", 42000, "2026-08-15", "2026-09-14", "Software licences"),
    ("IN-2026-310", "Initech LLC", 500000, "2026-10-01", "2026-11-15", "Data centre hardware"),
    ("AC-2026-101", "Acme Corp", 61000, "2026-07-20", "2026-08-19", "Spare parts"),
]


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(reset=False):
    """Create tables and seed the inbox. reset=True wipes everything first."""
    if reset and os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        if conn.execute("SELECT COUNT(*) FROM inbox_invoices").fetchone()[0] == 0:
            conn.executemany(
                "INSERT INTO inbox_invoices VALUES (?,?,?,?,?,?)", SEED_INBOX
            )


def list_inbox():
    with get_conn() as conn:
        return conn.execute("SELECT * FROM inbox_invoices").fetchall()


def get_inbox_invoice(invoice_id):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM inbox_invoices WHERE invoice_id = ?", (invoice_id,)
        ).fetchone()


def list_ap():
    with get_conn() as conn:
        return conn.execute("SELECT * FROM invoices ORDER BY invoice_id").fetchall()


def get_ap_invoice(invoice_id):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM invoices WHERE invoice_id = ?", (invoice_id,)
        ).fetchone()


def create_ap_invoice(invoice_id, vendor, amount, invoice_date, due_date, status="submitted"):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO invoices VALUES (?,?,?,?,?,?)",
            (invoice_id, vendor, amount, invoice_date, due_date, status),
        )
