"""Invoice helpers: parsing/validating extracted data and generic selection logic.

Nothing here is vendor-specific: the vendor is always an input, and "latest" is
decided from the dates observed on the page.
"""
import re
from dataclasses import asdict, dataclass
from datetime import date

FIELDS = ("invoice_id", "vendor", "amount", "invoice_date", "due_date")


class InvoiceError(Exception):
    """Base class for controlled, explainable invoice-processing failures."""


class InvoiceValidationError(InvoiceError):
    pass


class VendorNotFound(InvoiceError):
    pass


class AmbiguousSelection(InvoiceError):
    pass


def normalize_label(label):
    """'Invoice date' -> 'invoice_date'."""
    return re.sub(r"[^a-z0-9]+", "_", label.strip().lower()).strip("_")


@dataclass
class InvoiceData:
    invoice_id: str
    vendor: str
    amount: int
    invoice_date: str
    due_date: str

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_key_values(cls, kv):
        """Build from a page's label->value pairs; raises InvoiceValidationError listing every problem."""
        norm = {normalize_label(k): v for k, v in kv.items()}
        problems = [f"missing field: {f}" for f in FIELDS if not str(norm.get(f, "")).strip()]
        if problems:
            raise InvoiceValidationError("; ".join(problems))
        amount_raw = re.sub(r"[,\s]", "", norm["amount"])
        if not amount_raw.isdigit() or int(amount_raw) <= 0:
            problems.append(f"amount is not a positive whole number: {norm['amount']!r}")
        for f in ("invoice_date", "due_date"):
            try:
                date.fromisoformat(norm[f])
            except ValueError:
                problems.append(f"{f} is not an ISO date: {norm[f]!r}")
        if not problems and date.fromisoformat(norm["due_date"]) < date.fromisoformat(norm["invoice_date"]):
            problems.append("due_date is before invoice_date")
        if problems:
            raise InvoiceValidationError("; ".join(problems))
        return cls(
            invoice_id=norm["invoice_id"].strip(), vendor=norm["vendor"].strip(),
            amount=int(amount_raw), invoice_date=norm["invoice_date"], due_date=norm["due_date"],
        )


def find_invoice_table(tables):
    """The observed table whose headers include a vendor column and a date column."""
    for t in tables:
        headers = [h.lower() for h in t.get("headers", [])]
        if any("vendor" in h for h in headers) and any("date" in h for h in headers):
            return t
    raise InvoiceError("no invoice table found on this page")


def _col(headers, word):
    return next(h for h in headers if word in h.lower())


def _norm(text):
    """'Example Co.' -> 'example co' (case, punctuation and spacing don't matter when matching names)."""
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def rows_for_vendor(table, vendor_query):
    """Rows whose vendor cell contains the query (ignoring case/punctuation). Raises if none or if
    the query matches more than one distinct vendor (ambiguous)."""
    vcol = _col(table["headers"], "vendor")
    q = _norm(vendor_query)
    rows = [r for r in table["rows"] if q and q in _norm(r["cells"][vcol])]
    if not rows:
        seen = sorted({r["cells"][vcol] for r in table["rows"]})
        raise VendorNotFound(f"no invoices found for vendor {vendor_query!r}; vendors on this page: {seen}")
    vendors = sorted({r["cells"][vcol] for r in rows})
    if len(vendors) > 1:
        raise AmbiguousSelection(f"{vendor_query!r} matches several vendors: {vendors}")
    return rows


def pick_latest(table, rows):
    """Choose the row with the newest invoice date. Returns (row, evidence)."""
    dcol = _col(table["headers"], "date")
    icol = table["headers"][0]
    ranked = sorted(rows, key=lambda r: date.fromisoformat(r["cells"][dcol]), reverse=True)
    if len(ranked) > 1 and ranked[0]["cells"][dcol] == ranked[1]["cells"][dcol]:
        raise AmbiguousSelection("two invoices share the newest invoice date")
    evidence = {
        "selected": ranked[0]["cells"][icol],
        "candidates": [(r["cells"][icol], r["cells"][dcol]) for r in ranked],
        "rule": "newest invoice date among invoices for the requested vendor",
    }
    return ranked[0], evidence
