# Configuration

BareCDP can be configured through a JSON file, environment variables, or direct Python
constructor arguments. All three can be combined; environment variables take highest priority.

## Generating a starter config

```bash
python -m bare_cdp --write-default-config bare-cdp.json
bare-cdp --write-default-config bare-cdp.json   # same, using the console script
```

## Config file schema

```json
{
  "chrome": {
    "mode": "connect",
    "host": "127.0.0.1",
    "port": 9222,
    "ws_url": null,
    "executable": null,
    "user_data_dir": null,
    "headless": true,
    "extra_args": []
  },
  "timeouts": {
    "default": 10.0
  }
}
```

| Key | Type | Description |
| --- | --- | --- |
| `chrome.mode` | `"connect"` \| `"launch"` | `connect` attaches to a running Chrome; `launch` starts it |
| `chrome.host` | string | Debugging host (always use `127.0.0.1`) |
| `chrome.port` | integer | Debugging port (default `9222`) |
| `chrome.ws_url` | string \| null | Direct WebSocket debugger URL; skips discovery |
| `chrome.executable` | string \| null | Path to Chrome/Chromium binary; auto-detected when null |
| `chrome.user_data_dir` | string \| null | Chrome profile directory; temp dir created when null |
| `chrome.headless` | boolean | Whether to launch in headless mode |
| `chrome.extra_args` | array | Additional Chrome command-line flags |
| `timeouts.default` | number | Default timeout in seconds for all operations |

## Environment variables

Environment variables override the JSON file when set and non-empty.

| Variable | Config key |
| --- | --- |
| `BARE_CDP_HOST` | `chrome.host` |
| `BARE_CDP_PORT` | `chrome.port` |
| `BARE_CDP_WS_URL` | `chrome.ws_url` |
| `BARE_CDP_CHROME` | `chrome.executable` |
| `BARE_CDP_USER_DATA_DIR` | `chrome.user_data_dir` |
| `BARE_CDP_HEADLESS` | `chrome.headless` (`true`/`false`/`1`/`0`) |
| `BARE_CDP_TIMEOUT` | `timeouts.default` |

## Using a config from Python

```python
from bare_cdp import Browser

browser = Browser.from_config("bare-cdp.json")
page = browser.page()
page.navigate("https://example.com")
print(page.extract_text())
browser.close()
```

## Offline and closed-system use

BareCDP itself makes no requests beyond the local Chrome debugging endpoint. All it needs is:

- Python 3.9+ (standard library only — no pip install required for the module itself)
- A Chrome or Chromium binary reachable on the local machine

In air-gapped or locked-down environments, copy `bare_cdp.py` into the project and import
it directly — no package manager, no registry access.
