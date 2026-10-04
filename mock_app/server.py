"""Mock company web app: vendor invoice portal (/inbox) + internal AP system (/ap)."""
import os
import sys

from flask import Flask, abort, redirect, render_template, request, url_for

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import database as db  # noqa: E402

app = Flask(__name__)

REQUIRED_FIELDS = [
    ("invoice_id", "Invoice ID"),
    ("vendor", "Vendor"),
    ("amount", "Amount"),
    ("invoice_date", "Invoice date"),
    ("due_date", "Due date"),
]


@app.get("/")
def index():
    return redirect(url_for("inbox"))


@app.get("/inbox")
def inbox():
    return render_template("inbox.html", invoices=db.list_inbox())


@app.get("/invoice/<invoice_id>")
def invoice_detail(invoice_id):
    inv = db.get_inbox_invoice(invoice_id)
    if inv is None:
        abort(404)
    return render_template("invoice.html", inv=inv)


@app.get("/ap")
def ap_home():
    return render_template("ap.html", invoices=db.list_ap(), flash=request.args.get("created"))


def validate(form):
    """Return a list of human-readable errors for the AP create form."""
    errors = []
    for key, label in REQUIRED_FIELDS:
        if not form.get(key, "").strip():
            errors.append(f"{label} is required.")
    amount = form.get("amount", "").strip()
    if amount and not amount.isdigit():
        errors.append("Amount must be a whole number.")
    if form.get("invoice_id", "").strip() and db.get_ap_invoice(form["invoice_id"].strip()):
        errors.append(f"Invoice {form['invoice_id'].strip()} already exists in AP.")
    return errors


@app.route("/ap/create", methods=["GET", "POST"])
def ap_create():
    if request.method == "GET":
        return render_template("ap_create.html", errors=[], values={})
    errors = validate(request.form)
    if errors:
        return render_template("ap_create.html", errors=errors, values=request.form), 400
    f = request.form
    db.create_ap_invoice(
        f["invoice_id"].strip(), f["vendor"].strip(), int(f["amount"]),
        f["invoice_date"].strip(), f["due_date"].strip(),
    )
    return redirect(url_for("ap_home", created=f["invoice_id"].strip()))


def create_app(reset_db=False):
    db.init_db(reset=reset_db)
    return app


if __name__ == "__main__":
    create_app(reset_db="--reset" in sys.argv)
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5000)), debug=False)
