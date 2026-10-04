"""Shared test helpers: throwaway mock-app server (own port + temp DB) and DB reads."""
import contextlib
import os
import pathlib
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent


def stop_process(proc):
    """Kill the server AND its children. On Windows a venv's python.exe is a launcher that spawns the
    real interpreter as a child; proc.kill() alone would leave the server running and holding the DB file."""
    if proc.poll() is None:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
        else:
            proc.kill()
    proc.wait()


@contextlib.contextmanager
def running_app(fault=""):
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp, socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        db = os.path.join(tmp, "test.db")
        env = {**os.environ, "PORT": str(port), "INVOICEOP_DB": db, "INVOICEOP_FAULT": fault}
        proc = subprocess.Popen([sys.executable, str(ROOT / "mock_app" / "server.py"), "--reset"],
                                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        url = f"http://127.0.0.1:{port}"
        try:
            for _ in range(80):
                try:
                    urllib.request.urlopen(url + "/inbox", timeout=1)
                    break
                except OSError:
                    time.sleep(0.1)
            else:
                raise RuntimeError("mock app did not start")
            yield {"url": url, "db": db}
        finally:
            stop_process(proc)


def ap_rows(db):
    with contextlib.closing(sqlite3.connect(db)) as c:  # `with connect()` alone does not close on Windows
        return c.execute("SELECT invoice_id, vendor, amount, invoice_date, due_date, status FROM invoices").fetchall()
