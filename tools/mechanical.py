"""Scripted baseline (Phase 2): the whole invoice workflow with every decision in plain code.

Purpose: prove the browser tools work end-to-end before any LLM is involved. In Phase 3
the decisions made here (which invoice, what to click next) move to the LLM agent, while
the browser tools stay exactly the same.
"""
from tools.invoice import (
    InvoiceData,
    InvoiceError,
    find_invoice_table,
    pick_latest,
    rows_for_vendor,
)


def process_latest_invoice(browser, vendor_query):
    """Find the latest invoice for `vendor_query`, copy it into the AP system, return a result dict.

    status: "submitted" (AP showed its confirmation) or "rejected" (AP showed errors).
    Raises InvoiceError subclasses for controlled failures (unknown/ambiguous vendor, bad data).
    """
    # 1. Observe the invoice list and choose from what is actually on the page.
    browser.navigate("/inbox")
    obs = browser.inspect_page()
    table = find_invoice_table(obs["tables"])
    rows = rows_for_vendor(table, vendor_query)
    row, evidence = pick_latest(table, rows)

    # 2. Open the chosen invoice and read its fields.
    link = row["links"][0]["text"]
    browser.click(link)
    detail = browser.inspect_page()
    kv = next((t["key_values"] for t in detail["tables"] if t.get("key_values")), None)
    if not kv:
        raise InvoiceError("invoice detail page has no readable fields")
    invoice = InvoiceData.from_key_values(kv)

    # 3. Open the AP create form and fill whichever fields the form actually has.
    browser.navigate("/ap/create")
    form = browser.inspect_page()["forms"][0]
    values = invoice.to_dict()
    for field in form["fields"]:
        if field["name"] in values:
            browser.fill(field["name"], values[field["name"]])

    # 4. Submit and read the outcome from the page, not from the HTTP code alone.
    result = browser.submit()
    after = browser.inspect_page()
    errors = [m["text"] for m in after["messages"] if m["kind"] == "error"]
    confirmed = any(m["kind"] == "ok" and invoice.invoice_id in m["text"] for m in after["messages"])
    in_list = any(
        invoice.invoice_id in r["cells"].values()
        for t in after["tables"] for r in t.get("rows", [])
    )
    status = "submitted" if (confirmed and in_list and not errors) else "rejected"
    return {
        "status": status,
        "invoice": invoice.to_dict(),
        "selection_evidence": evidence,
        "http_status": result["status"],
        "ui_errors": errors,
        "actions": len(browser.actions),
    }
