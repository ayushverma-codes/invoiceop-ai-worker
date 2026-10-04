"""Browser tool layer (Playwright, sync API).

Five tools the agent will call: navigate, inspect_page, click, fill, submit.
`inspect_page` returns a generic, site-agnostic structured observation
(tables, key/value tables, forms, links, messages) - nothing here knows about
vendors or invoices.

Environment variables (all optional):
  MOCK_APP_URL            base URL for relative navigation (default http://127.0.0.1:5000)
  INVOICEOP_HEADLESS      "0" to watch the browser (default headless)
  INVOICEOP_CHROMIUM_PATH use an existing Chromium/Chrome binary instead of Playwright's own
"""
import os

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

DEFAULT_BASE_URL = "http://127.0.0.1:5000"
SELECTOR_PREFIXES = ("#", ".", "[", "css=", "text=", "xpath=", "//")

_INSPECT_JS = """
() => {
  const txt = el => (el && (el.innerText || el.textContent) || '').trim().replace(/\\s+/g, ' ');
  const linksIn = root => [...root.querySelectorAll('a[href]')]
      .map(a => ({text: txt(a), href: a.getAttribute('href')}));

  const tables = [...document.querySelectorAll('table')].map(t => {
    const headers = [...t.querySelectorAll('thead th')].map(txt);
    if (headers.length) {
      const rows = [...t.querySelectorAll('tbody tr')]
        .filter(tr => !tr.querySelector('td[colspan]'))
        .map(tr => {
          const cells = [...tr.children].map(txt);
          const rec = {};
          headers.forEach((h, i) => { rec[h] = cells[i] ?? ''; });
          return {cells: rec, links: linksIn(tr)};
        });
      return {id: t.id || null, headers, rows};
    }
    const kv = {};
    t.querySelectorAll('tr').forEach(tr => {
      const th = tr.querySelector('th'), td = tr.querySelector('td');
      if (th && td) kv[txt(th)] = txt(td);
    });
    return {id: t.id || null, key_values: kv};
  });

  const forms = [...document.forms].map(f => ({
    id: f.id || null,
    action: f.getAttribute('action'),
    method: (f.method || 'get').toLowerCase(),
    fields: [...f.querySelectorAll('input,select,textarea')]
      .filter(i => !['hidden', 'submit', 'button'].includes(i.type))
      .map(i => ({name: i.name, label: txt(i.closest('label')), value: i.value})),
    buttons: [...f.querySelectorAll('button,input[type=submit]')]
      .map(b => txt(b) || b.value || ''),
  }));

  const messages = [];
  document.querySelectorAll('.error,.ok,[role=alert]').forEach(el => {
    const kind = el.classList.contains('ok') ? 'ok' : 'error';
    const items = [...el.querySelectorAll('li')];
    if (items.length) items.forEach(li => messages.push({kind, text: txt(li)}));
    else messages.push({kind, text: txt(el)});
  });

  const main = document.querySelector('main') || document.body;
  return {
    url: location.href,
    title: document.title,
    headings: [...document.querySelectorAll('h1,h2,h3')].map(txt),
    links: linksIn(document),
    tables, forms, messages,
    text: txt(main).slice(0, 2000),
  };
}
"""


class BrowserError(Exception):
    """A browser tool call failed in a way the caller may be able to recover from."""


class Browser:
    def __init__(self, base_url=None, headless=None, timeout_ms=5000):
        self.base_url = (base_url or os.environ.get("MOCK_APP_URL") or DEFAULT_BASE_URL).rstrip("/")
        if headless is None:
            headless = os.environ.get("INVOICEOP_HEADLESS", "1") != "0"
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.actions = []  # trace of every tool call: {"action", "args", "ok"}
        self._pw = self._browser = self.page = None

    # lifecycle ---------------------------------------------------------
    def start(self):
        self._pw = sync_playwright().start()
        kwargs = {"headless": self.headless}
        exe = os.environ.get("INVOICEOP_CHROMIUM_PATH")
        if exe:
            kwargs["executable_path"] = exe
        if hasattr(os, "geteuid") and os.geteuid() == 0:  # root in containers needs this
            kwargs["args"] = ["--no-sandbox"]
        self._browser = self._pw.chromium.launch(**kwargs)
        self.page = self._browser.new_page()
        self.page.set_default_timeout(self.timeout_ms)
        return self

    def close(self):
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()
        self._browser = self._pw = self.page = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.close()

    # tools -------------------------------------------------------------
    def _record(self, action, args, ok):
        self.actions.append({"action": action, "args": args, "ok": ok})

    def _url(self, url):
        return url if url.startswith("http") else self.base_url + "/" + url.lstrip("/")

    def navigate(self, url):
        """Open a URL (absolute, or a path relative to the app). Returns {url, status}."""
        try:
            resp = self.page.goto(self._url(url))
        except PlaywrightError as e:
            self._record("navigate", {"url": url}, False)
            raise BrowserError(f"navigate({url}) failed: {e}") from e
        self._record("navigate", {"url": url}, True)
        return {"url": self.page.url, "status": resp.status if resp else None}

    def inspect_page(self):
        """Structured observation of the current page. Read-only."""
        obs = self.page.evaluate(_INSPECT_JS)
        self._record("inspect_page", {}, True)
        return obs

    def click(self, target):
        """Click an element. `target` is a CSS/text selector (starts with #, ., [, css=,
        text=, xpath=) or the visible name of a link/button."""
        try:
            if target.startswith(SELECTOR_PREFIXES):
                loc = self.page.locator(target).first
            else:
                loc = self.page.get_by_role("link", name=target, exact=True)
                if loc.count() == 0:
                    loc = self.page.get_by_role("button", name=target, exact=True)
                loc = loc.first
            loc.click()
            self.page.wait_for_load_state()
        except (PlaywrightTimeout, PlaywrightError) as e:
            self._record("click", {"target": target}, False)
            raise BrowserError(f"click({target!r}) failed: {str(e).splitlines()[0]}") from e
        self._record("click", {"target": target}, True)
        return {"url": self.page.url}

    def fill(self, field, value):
        """Type `value` into the input named `field` (falls back to matching its label)."""
        try:
            loc = self.page.locator(f'[name="{field}"]')
            if loc.count() == 0:
                loc = self.page.get_by_label(field)
            loc.first.fill(str(value))
        except (PlaywrightTimeout, PlaywrightError) as e:
            self._record("fill", {"field": field, "value": value}, False)
            raise BrowserError(f"fill({field!r}) failed: {str(e).splitlines()[0]}") from e
        self._record("fill", {"field": field, "value": value}, True)
        return {"field": field}

    def submit(self):
        """Submit the form on the page. Returns {url, status} of the resulting page."""
        try:
            with self.page.expect_navigation() as nav:
                self.page.locator("form button[type=submit], form input[type=submit]").first.click()
            resp = nav.value
        except (PlaywrightTimeout, PlaywrightError) as e:
            self._record("submit", {}, False)
            raise BrowserError(f"submit() failed: {str(e).splitlines()[0]}") from e
        self._record("submit", {}, True)
        return {"url": self.page.url, "status": resp.status if resp else None}
