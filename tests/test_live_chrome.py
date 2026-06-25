"""
Live Chrome smoke tests for zero_cdp.py — stdlib only, opt-in.

These tests drive a real Chrome/Chromium process over the Chrome DevTools
Protocol. They are skipped unless ``ZERO_CDP_LIVE_CHROME=1`` is set, so the
default unit run (``python -m unittest discover -s tests``) stays headless and
browser-free.

Run locally:

    ZERO_CDP_LIVE_CHROME=1 python -W error::ResourceWarning \
        -m unittest tests.test_live_chrome -v

Optionally point at a specific binary with ``ZERO_CDP_CHROME=/path/to/chrome``;
otherwise ZeroCDP auto-discovers Chrome/Chromium. Chrome is launched headless
with a disposable profile via ``launch_chrome`` and torn down (process +
temp profile) after the suite. Screenshots and temp artifacts use the system
temporary directory.
"""

import os
import pathlib
import shutil
import sys
import tempfile
import time
import unittest
import urllib.parse

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import zero_cdp


LIVE = os.environ.get("ZERO_CDP_LIVE_CHROME") == "1"
CHROME_EXE = os.environ.get("ZERO_CDP_CHROME") or None
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff"

# Small self-contained pages. No external network is touched; everything is a
# data: URL so the tests are deterministic and offline-friendly.
TEXT_HTML = (
    "<!doctype html><html><head><meta charset='utf-8'>"
    "<title>ZeroCDP Live</title></head>"
    "<body><main id='content'>Hello from the ZeroCDP live smoke test.</main></body></html>"
)
INTERACTION_HTML = (
    "<!doctype html><html><head><meta charset='utf-8'>"
    "<title>ZeroCDP Interactions</title></head><body>"
    "<input id='field' type='text'>"
    "<button id='btn'>Go</button>"
    "<div id='status'>idle</div>"
    "<div id='keys'>none</div>"
    "<script>"
    "document.getElementById('btn').addEventListener('click',function(){"
    "document.getElementById('status').textContent='clicked';});"
    "document.getElementById('field').addEventListener('keydown',function(e){"
    "if(e.key==='Enter'){document.getElementById('keys').textContent='enter';}});"
    "</script></body></html>"
)
DELAYED_HTML = (
    "<!doctype html><html><head><meta charset='utf-8'>"
    "<title>ZeroCDP Delayed</title></head><body>"
    "<div id='immediate'>here</div>"
    "<script>setTimeout(function(){"
    "var d=document.createElement('div');d.id='delayed';d.textContent='ready';"
    "document.body.appendChild(d);},200);</script>"
    "</body></html>"
)
BOX_HTML = (
    "<!doctype html><html><head><meta charset='utf-8'>"
    "<title>ZeroCDP Shot</title></head>"
    "<body style='margin:0'>"
    "<div style='width:200px;height:120px;background:#3366cc'></div>"
    "</body></html>"
)


def _data_url(html):
    return "data:text/html;charset=utf-8," + urllib.parse.quote(html)


@unittest.skipUnless(LIVE, "set ZERO_CDP_LIVE_CHROME=1 to run live Chrome tests")
class LiveChromeTest(unittest.TestCase):
    """Exercise the real launch/connect/act/cleanup path against Chrome."""

    launch = None

    @classmethod
    def setUpClass(cls):
        try:
            cls.launch = zero_cdp.launch_chrome(
                executable=CHROME_EXE,
                headless=True,
                ready_timeout=30.0,
            )
        except FileNotFoundError as exc:
            raise unittest.SkipTest(
                "no Chrome/Chromium binary found; set ZERO_CDP_CHROME to a "
                f"browser executable to run live tests ({exc})"
            )
        cls.port = cls.launch.port

    @classmethod
    def tearDownClass(cls):
        if cls.launch is not None:
            zero_cdp.terminate_chrome(cls.launch)
            cls.launch = None

    # -- helpers ----------------------------------------------------------

    def _browser(self):
        browser = zero_cdp.Browser(port=self.port)
        self.addCleanup(browser.close)
        return browser

    def _page(self, html):
        """Open a fresh tab and navigate it to a data: URL of ``html``."""
        browser = self._browser()
        page = browser.new_tab("about:blank")
        page.navigate(_data_url(html))
        return page

    # -- tests ------------------------------------------------------------

    def test_launch_metadata_and_evaluate(self):
        """A launched browser exposes a real endpoint and evaluates JS."""
        self.assertIsInstance(self.port, int)
        self.assertGreater(self.port, 0)
        self.assertTrue(self.launch.browser_ws_url.startswith("ws://"))
        self.assertTrue(self.launch.owns_user_data_dir)
        self.assertTrue(os.path.isdir(self.launch.user_data_dir))

        browser = self._browser()
        page = browser.connect()
        self.assertEqual(page.evaluate("1 + 2"), 3)
        self.assertEqual(page.evaluate("'ze' + 'ro' + '-cdp'"), "zero-cdp")
        user_agent = page.evaluate("navigator.userAgent")
        self.assertIsInstance(user_agent, str)
        self.assertIn("Chrome", user_agent)

    def test_navigate_and_extract_text(self):
        """Navigation to a data URL renders extractable text and title."""
        page = self._page(TEXT_HTML)
        self.assertEqual(page.evaluate("document.title"), "ZeroCDP Live")

        whole = page.extract_text()
        self.assertIn("Hello from the ZeroCDP live smoke test.", whole)

        scoped = page.extract_text("#content")
        self.assertEqual(scoped.strip(), "Hello from the ZeroCDP live smoke test.")

        html = page.extract_html("#content")
        self.assertIn("id=\"content\"", html)
        self.assertIn("Hello from the ZeroCDP live smoke test.", html)

    def test_wait_for_selector_success(self):
        """wait_for_selector resolves an element added after a delay."""
        page = self._page(DELAYED_HTML)
        self.assertTrue(page.wait_for_selector("#immediate", timeout=5.0))
        self.assertTrue(page.wait_for_selector("#delayed", timeout=5.0))
        self.assertEqual(
            page.evaluate("document.querySelector('#delayed').textContent"),
            "ready",
        )

    def test_wait_for_selector_missing_times_out(self):
        """A selector that never appears raises CDPTimeoutError."""
        page = self._page(TEXT_HTML)
        with self.assertRaises(zero_cdp.CDPTimeoutError):
            page.wait_for_selector("#does-not-exist", timeout=0.75)

    def test_wait_for_selector_invalid_raises(self):
        """Invalid CSS syntax surfaces as SelectorError, not a timeout."""
        page = self._page(TEXT_HTML)
        with self.assertRaises(zero_cdp.SelectorError):
            page.wait_for_selector("::::", timeout=2.0)

    def test_input_text_punctuation_unicode_quotes(self):
        """input_text round-trips tricky strings through the focused field."""
        page = self._page(INTERACTION_HTML)
        cases = [
            "a.b-c_d/e:f;g,h!?(){}[]",
            "He said \"hi\" & 'bye' `tick` \\ done",
            "café — naïve ☃ 日本語 \U0001F680",
        ]
        for value in cases:
            with self.subTest(value=value):
                page.input_text("#field", value)
                read_back = page.evaluate("document.querySelector('#field').value")
                self.assertEqual(read_back, value)

    def test_input_text_missing_selector_raises(self):
        """Targeting a missing element raises SelectorError before inserting."""
        page = self._page(INTERACTION_HTML)
        with self.assertRaises(zero_cdp.SelectorError):
            page.input_text("#missing-field", "text")

    def test_click_updates_dom_state(self):
        """A real CDP mouse click triggers the page's click handler."""
        page = self._page(INTERACTION_HTML)
        self.assertEqual(
            page.evaluate("document.querySelector('#status').textContent"),
            "idle",
        )
        page.click("#btn")
        self.assertEqual(
            page.evaluate("document.querySelector('#status').textContent"),
            "clicked",
        )

    def test_press_enter_updates_key_state(self):
        """press('Enter') dispatches a keydown the focused field observes."""
        page = self._page(INTERACTION_HTML)
        page.input_text("#field", "hello")  # focuses #field
        self.assertEqual(
            page.evaluate("document.querySelector('#keys').textContent"),
            "none",
        )
        page.press("Enter")
        self.assertEqual(
            page.evaluate("document.querySelector('#keys').textContent"),
            "enter",
        )

    def test_screenshot_bytes_and_path(self):
        """Screenshots return PNG bytes and can be written to /tmp."""
        page = self._page(BOX_HTML)
        page.wait_for_selector("div", timeout=5.0)

        data = page.screenshot()
        self.assertIsInstance(data, bytes)
        self.assertGreater(len(data), 100)
        self.assertTrue(data.startswith(PNG_MAGIC))

        tmpdir = tempfile.mkdtemp(prefix="zero_cdp_live_")
        self.addCleanup(shutil.rmtree, tmpdir, ignore_errors=True)
        path = os.path.join(tmpdir, "shot.png")
        written = page.screenshot(path=path)
        self.assertTrue(os.path.isfile(path))
        self.assertGreater(os.path.getsize(path), 100)
        with open(path, "rb") as handle:
            on_disk = handle.read()
        self.assertEqual(on_disk, written)
        self.assertTrue(on_disk.startswith(PNG_MAGIC))

        jpeg = page.screenshot(format="jpeg")
        self.assertIsInstance(jpeg, bytes)
        self.assertTrue(jpeg.startswith(JPEG_MAGIC))

    def test_multiple_tabs_and_target_selection(self):
        """Distinct tabs show up as targets and are selectable by title."""
        browser = self._browser()
        browser.new_tab(
            _data_url("<title>Tab Alpha</title><p>alpha</p>"), connect=False
        )
        browser.new_tab(
            _data_url("<title>Tab Beta</title><p>beta</p>"), connect=False
        )

        deadline = time.monotonic() + 5.0
        titles = set()
        while time.monotonic() < deadline:
            pages = [t for t in browser.list_targets() if t.get("type") == "page"]
            titles = {t.get("title") for t in pages}
            if {"Tab Alpha", "Tab Beta"} <= titles:
                break
            time.sleep(0.1)

        self.assertIn("Tab Alpha", titles)
        self.assertIn("Tab Beta", titles)

        page = browser.select_target(title_contains="Tab Beta")
        self.assertEqual(page.evaluate("document.title"), "Tab Beta")

    def test_event_wait_and_recent_events(self):
        """wait_for_event observes a live CDP event; history stays sane."""
        page = self._page(TEXT_HTML)
        page.call("Runtime.enable")
        cursor = page.event_cursor()
        self.assertIsInstance(cursor, int)

        page.evaluate("console.log('zero-cdp-live-event')")
        params = page.wait_for_event(
            "Runtime.consoleAPICalled",
            timeout=5.0,
            after_sequence=cursor,
        )
        values = [arg.get("value") for arg in params.get("args", [])]
        self.assertIn("zero-cdp-live-event", values)

        recent = page.recent_events()
        self.assertIsInstance(recent, tuple)
        self.assertGreaterEqual(page.event_cursor(), cursor)
        self.assertIsInstance(page.dropped_event_count, int)
        self.assertGreaterEqual(page.dropped_event_count, 0)

    def test_terminate_chrome_cleans_process_and_profile(self):
        """A fresh launch is fully torn down: process exits, temp profile gone."""
        launch = zero_cdp.launch_chrome(
            executable=CHROME_EXE,
            headless=True,
            ready_timeout=30.0,
        )
        # Safety net if an assertion below fails before explicit termination.
        self.addCleanup(zero_cdp.terminate_chrome, launch)

        profile = launch.user_data_dir
        self.assertTrue(launch.owns_user_data_dir)
        self.assertTrue(os.path.isdir(profile))
        self.assertIsNone(launch.process.poll())  # still running

        # A throwaway connection proves the endpoint is live before teardown.
        browser = zero_cdp.Browser(port=launch.port)
        try:
            self.assertEqual(browser.connect().evaluate("21 * 2"), 42)
        finally:
            browser.close()

        zero_cdp.terminate_chrome(launch)
        self.assertIsNotNone(launch.process.poll())  # exited
        self.assertFalse(os.path.exists(profile))    # temp profile removed
        if launch.stderr_path:
            self.assertFalse(os.path.exists(launch.stderr_path))


if __name__ == "__main__":
    unittest.main()
