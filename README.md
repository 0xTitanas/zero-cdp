# BareCDP

<p align="center">
  <strong>Bare-metal Chrome DevTools Protocol automation for Python.</strong><br>
  One file. No runtime dependencies. Drive Chrome/Chromium from your own scripts.
</p>

<p align="center">
  <img alt="Python 3.9+" src="https://img.shields.io/badge/python-3.9%2B-blue">
  <img alt="Runtime dependencies: zero" src="https://img.shields.io/badge/runtime%20deps-zero-brightgreen">
  <img alt="Automation: CDP" src="https://img.shields.io/badge/automation-CDP-orange">
  <img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-lightgrey">
</p>

---

**BareCDP** is a small Python browser-control layer for scripts, CLIs, test harnesses, and orchestrators that need to drive Chrome or Chromium without installing Playwright, Selenium, WebDriver, `requests`, `websockets`, or any other runtime package.

It talks directly to the **Chrome DevTools Protocol** over a raw RFC-6455 WebSocket implemented with the Python standard library.

```python
from bare_cdp import Browser

browser = Browser(port=9222)
page = browser.connect()

page.navigate("https://example.com")
print(page.extract_text())

browser.close()
```

## Why BareCDP?

Sometimes you don't need a full browser automation framework. You need a compact browser actuator that can be dropped into another system and called deterministically.

BareCDP is designed for:

- locked-down, offline, or air-gapped environments where only Python and a Chrome binary are available;
- internal tools that need one vendorable file;
- orchestrator scripts that need to control a browser without becoming a framework;
- CI smoke checks that only need navigation, extraction, screenshots, and form input;
- agentic systems or non-agent systems that need a minimal browser-control primitive;
- debugging and protocol experiments where direct CDP access is useful.

BareCDP's public API is synchronous. A single `CDPConnection` serializes socket access and supports one active command or event wait at a time; systems that need parallel browser work should open separate connections, use threads or processes, or let their orchestrator own concurrency.

## What it is — and what it is not

BareCDP is:

- a **stdlib-only** Chrome DevTools Protocol client;
- a **single-file** module you can copy into a project;
- a practical wrapper around common CDP actions;
- a raw `call(method, params)` escape hatch for any CDP command.

BareCDP is not:

- a full Playwright replacement;
- a full Selenium replacement;
- a browser farm manager;
- a locator engine with years of auto-waiting heuristics;
- a stealth or anti-detection library.

If you need robust cross-browser testing, tracing, network routing, HAR/video capture, isolation contexts, downloads/uploads, and deep locator semantics, use Playwright. If you need a tiny dependency-free CDP actuator, BareCDP is the sharper tool.

## Features

- **Zero runtime dependencies** — imports only Python standard-library modules.
- **Raw WebSocket implementation** — handshake validation, masked client frames, ping/pong, close handling, fragmentation support, timeouts.
- **CDP endpoint discovery** — `/json/list`, `/json/version`, `/json/new`.
- **Chrome launch discovery** — PATH lookup plus common macOS, Linux, and Windows Chrome/Chromium install paths.
- **High-level page actions**:
  - launch Chrome/Chromium;
  - connect to an existing debug port or WebSocket URL;
  - list targets;
  - open new tabs;
  - select targets;
  - navigate;
  - evaluate JavaScript;
  - wait for selectors;
  - click elements;
  - fill text inputs;
  - press keys;
  - extract rendered text;
  - extract HTML;
  - capture screenshots.
- **Low-level escape hatch** — `CDPConnection.call(...)` for arbitrary protocol methods.
- **Configurable** — JSON config file plus environment variable overrides.
- **CLI included** — `python -m bare_cdp` or `bare-cdp` console script; useful for shell scripts and quick probes.
- **Tested without Chrome** — fake stdlib WebSocket/CDP server tests protocol behavior.

## Installation

### Option 1: copy one file

Download or copy `bare_cdp.py` into your project:

```text
your_project/
  bare_cdp.py
  your_script.py
```

Then:

```python
from bare_cdp import Browser
```

### Option 2: use as a local package

```bash
git clone https://github.com/0xTitanas/bare-cdp.git
cd bare-cdp
python -m pip install .
```

Then:

```python
from bare_cdp import Browser
```

After installation, the `bare-cdp` console script is available alongside `python -m bare_cdp`.

### Vendoring note

BareCDP is intentionally copyable as a single file. If you vendor `bare_cdp.py`
into another project, preserve the module metadata/header so downstream users can
find the canonical source, license, and issue tracker:

- Source: https://github.com/0xTitanas/bare-cdp
- Issues: https://github.com/0xTitanas/bare-cdp/issues
- License: MIT

## Requirements

- Python 3.9+
- Chrome, Chromium, Chrome for Testing, or another CDP-compatible browser
- A browser launched with a local debugging endpoint, for example:

```bash
chrome --remote-debugging-port=9222 --user-data-dir=/tmp/bare-cdp-profile
```

On macOS, the Chrome binary is often:

```text
/Applications/Google Chrome.app/Contents/MacOS/Google Chrome
~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome
```

BareCDP checks common macOS, Linux, Windows, and Chrome-for-Testing locations when launching Chrome.
On Windows, BareCDP also checks `shutil.which(...)`, `ProgramW6432`, `Program Files`, `Program Files (x86)`, and `LOCALAPPDATA` for `chrome.exe`/Chromium/Edge-compatible CDP binaries. If your machine is locked down or Chrome lives elsewhere, either pass `executable="C:\\path\\to\\chrome.exe"` to `launch_chrome()` / `chrome.executable`, or start Chrome yourself and use connect mode.

## Quick start

### Connect to an existing Chrome instance

Start Chrome:

```bash
chrome --remote-debugging-port=9222 --user-data-dir=/tmp/bare-cdp-profile
```

Run Python:

```python
from bare_cdp import Browser

browser = Browser(host="127.0.0.1", port=9222)
page = browser.connect()
page.navigate("https://example.com")

print(page.evaluate("document.title"))
print(page.extract_text())

browser.close()
```

### Launch Chrome from Python

```python
from bare_cdp import Browser, launch_chrome, terminate_chrome

launch = launch_chrome(headless=True)  # uses an ephemeral CDP port by default
browser = Browser(port=launch.port)
try:
    page = browser.connect()
    page.navigate("https://example.com")
    print(page.extract_text())
finally:
    browser.close()           # closes owned CDP WebSocket connections
    terminate_chrome(launch)  # stops Chrome and removes BareCDP's temp profile, if any
```

When `user_data_dir` is not supplied, `launch_chrome()` creates a temporary directory under
the system temp path and reads Chrome's `DevToolsActivePort` file to bind to the spawned
process instead of guessing a fixed port. Call `terminate_chrome(launch)` to stop Chrome and
remove that temporary profile. User-supplied profile directories are preserved.

### Fill a form

```python
from bare_cdp import Browser

browser = Browser(port=9222)
page = browser.connect()

page.navigate("https://example.com/search")
page.wait_for_selector("input[name=q]")
page.input_text("input[name=q]", "Chrome DevTools Protocol", press_enter=True)

print(page.extract_text())
browser.close()
```

### Take a screenshot

```python
from bare_cdp import Browser

browser = Browser(port=9222)
page = browser.connect()
page.navigate("https://example.com")
page.screenshot("example.png")
browser.close()
```

### Send raw CDP commands

```python
from bare_cdp import Browser

page = Browser(port=9222).connect()

result = page.call("Runtime.evaluate", {
    "expression": "document.documentElement.outerHTML",
    "returnByValue": True,
})

html = result["result"]["value"]
print(html[:500])
```

## Configuration

BareCDP can be configured with JSON and environment variables.

Create a default config:

```bash
python -m bare_cdp --write-default-config bare-cdp.json
```

Example:

```json
{
  "chrome": {
    "mode": "connect",
    "host": "127.0.0.1",
    "port": 9222,
    "ws_url": null,
    "executable": null,
    "user_data_dir": "./.chrome-profile",
    "headless": true,
    "extra_args": []
  },
  "timeouts": {
    "default": 10.0
  }
}
```

Use it:

```python
from bare_cdp import Browser

browser = Browser.from_config("bare-cdp.json")
page = browser.page()
page.navigate("https://example.com")
print(page.extract_text())
browser.close()
```

Environment overrides:

| Variable | Meaning |
| --- | --- |
| `BARE_CDP_HOST` | Debugging host |
| `BARE_CDP_PORT` | Debugging port |
| `BARE_CDP_WS_URL` | Direct WebSocket debugger URL |
| `BARE_CDP_CHROME` | Chrome/Chromium executable path |
| `BARE_CDP_USER_DATA_DIR` | Chrome user-data directory |
| `BARE_CDP_HEADLESS` | `true` / `false` |
| `BARE_CDP_TIMEOUT` | Default timeout in seconds |

## CLI

```bash
python -m bare_cdp --help
bare-cdp --help              # same; available after pip install
```

Common examples:

```bash
# Extract rendered text
python -m bare_cdp --navigate https://example.com --extract-text

# Extract HTML
python -m bare_cdp --navigate https://example.com --extract-html

# Evaluate JavaScript
python -m bare_cdp --eval "document.title"

# Screenshot
python -m bare_cdp --navigate https://example.com --screenshot example.png

# Launch Chrome first, then run
python -m bare_cdp --launch --navigate https://example.com --extract-text

# Use config
python -m bare_cdp --config bare-cdp.json --navigate https://example.com --extract-text
```

## API overview

### Browser / target helpers

```python
from bare_cdp import (
    Browser,
    CDPConnection,
    CDPError,
    CDPConnectionError,
    CDPProtocolError,
    CDPTimeoutError,
    CDPCommandError,
    NavigationError,
    SelectorError,
    CDPEvent,
    CDPSession,
    LaunchedChrome,
    ANY_SESSION,
    discover_ws_url,
    list_targets_from_port,
    new_tab_from_port,
    launch_chrome,
    wait_until_ready,
    terminate_chrome,
)
```

- `Browser(host="127.0.0.1", port=9222, timeout=10.0)`
- `Browser.from_config(path)`
- `browser.connect(ws_url=None, replace=True)`
- `browser.page()`
- `browser.list_targets()`
- `browser.select_target(...)`
- `browser.new_tab(url="about:blank", connect=True)`
- `browser.close()`
- `launch_chrome(..., ready_timeout=10.0) -> LaunchedChrome`
- `wait_until_ready(host="127.0.0.1", port=9222, timeout=10.0)`
- `terminate_chrome(proc, timeout=5.0)`

### Page / connection actions

`Browser.connect()` returns a `CDPConnection` object:

- `call(method, params=None, timeout=None, session_id=None)`
- `wait_for_event(event_name, predicate=None, timeout=None, session_id=None, after_sequence=None)`
- `event_cursor()` / `recent_events()` / `dropped_event_count`
- `attach_session(target_id)`
- `attach_to_target(target_id)` *(compatibility helper returning the session ID string)*
- `navigate(url, wait=True, timeout=None, wait_until="load")`
- `wait_for_ready_state(states=("interactive", "complete"), timeout=None)`
- `evaluate(expression, return_by_value=True, timeout=None)`
- `wait_for_selector(selector, timeout=None)`
- `click(selector)`
- `input_text(selector, text, clear=True, press_enter=False)`
- `press(key)`
- `extract_text(selector=None)`
- `extract_html(selector=None)`
- `screenshot(path=None, format="png")`
- `close()`

## Security notes

Chrome remote debugging is powerful. Treat it like local control of the browser profile.

Recommended defaults:

- Bind the debugging endpoint to `127.0.0.1`, not `0.0.0.0`. BareCDP defaults to `127.0.0.1`.
- Do **not** pass `--remote-debugging-address` or other flags that expose the port to a
  routable network address.
- Use a dedicated `--user-data-dir` for automation.
- Do not expose the debugging port to a network or through a reverse proxy.
- Do not log cookies, tokens, local storage, or full page dumps from authenticated apps.
- Prefer disposable profiles for CI and untrusted pages.
- Do not automate password or 2FA entry through generic scripts.

`--remote-allow-origins` (optional): Chrome accepts this flag to restrict which web origins
can initiate a WebSocket upgrade to the debugging port. For programmatic Python clients it
has no effect, but it is worth setting if a browser-based DevTools client might also connect
to the same port.

A safe launch shape:

```bash
chrome \
  --remote-debugging-port=9222 \
  --user-data-dir=/tmp/bare-cdp-profile \
  --no-first-run \
  --no-default-browser-check \
  --disable-extensions
```

See [docs/security.md](docs/security.md) for a full reference.

## Testing

Run the unit tests:

```bash
python -m unittest discover -s tests -v
```

Run the module smoke check:

```bash
python -m py_compile bare_cdp.py tests/test_bare_cdp.py
python -m bare_cdp --help
bare-cdp --help  # after pip install
```

The tests use only the Python standard library. They include a small fake WebSocket/CDP server
to verify handshake behavior, client frame masking, CDP event filtering, selector safety,
navigation events, WebSocket control frames, screenshot decoding, endpoint discovery, config
overrides, and process cleanup. Run the commands above before submitting changes.

## Design notes

BareCDP intentionally keeps the core small:

- one JSON-RPC command or event wait at a time per connection, enforced with a connection-level lock;
- synchronous API only; BareCDP does not provide an async client today;
- direct CDP primitives instead of a large abstraction layer;
- `navigate()` checks `Page.navigate` errors and waits for loader-correlated lifecycle events or same-document navigation events (`wait=True`);
- JavaScript snippets use `json.dumps(...)` for safe selector/text interpolation;
- common browser interactions are thin wrappers over CDP.

This makes the module easy to audit and easy to vendor.

## Limitations

BareCDP does not currently provide:

- an async API;
- full Playwright-style locator semantics;
- automatic retries around every action;
- frame/shadow-DOM convenience wrappers;
- download/upload helpers;
- request interception wrappers;
- tracing/HAR/video helpers;
- browser context isolation wrappers;
- mobile/device emulation convenience presets.

Most of those capabilities are reachable through raw CDP commands. They are not yet wrapped as first-class APIs.

## Roadmap

Possible future additions:

- async client;
- generated CDP method helpers;
- richer selector strategies;
- frame and shadow DOM helpers;
- network interception convenience APIs;
- trace and performance helpers;
- packaged single-file release artifact;
- optional live-Chrome smoke test command.

## Similar projects

If you need more abstraction or typed protocol wrappers, look at:

- Playwright
- Selenium
- pychrome
- PyChromeDevTools
- PyCDP / chrome-devtools-protocol
- zerodep CDP

BareCDP is for the specific niche where **small, auditable, stdlib-only, directly vendorable browser control** is the primary goal.

## License

MIT. See [LICENSE](LICENSE).
