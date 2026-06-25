# Configuration

ZeroCDP can be configured through a JSON file, environment variables, or direct Python
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
    "port": null,
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
| `chrome.port` | integer \| null | `null` is mode-dependent: connect mode uses `9222`; launch mode uses `0` so Chrome chooses an ephemeral port. Explicit nonzero launch ports are honored only if free. |
| `chrome.ws_url` | string \| null | Direct WebSocket debugger URL; connect mode only. Launch mode rejects it to avoid controlling a browser other than the spawned process. |
| `chrome.executable` | string \| null | Path to Chrome/Chromium binary; auto-detected when null |
| `chrome.user_data_dir` | string \| null | Chrome profile directory; temp dir created when null |
| `chrome.headless` | boolean | Whether to launch in headless mode |
| `chrome.extra_args` | array of strings | Additional Chrome command-line flags. Launch mode rejects ownership-critical flags: `--remote-debugging-port`, `--remote-debugging-address`, and `--user-data-dir`. |
| `timeouts.default` | finite positive number | Default timeout in seconds for all operations |

When `chrome.executable` is null, launch mode checks PATH via `shutil.which(...)`, common macOS app-bundle paths, Linux Chrome/Chromium binary names, and Windows `ProgramW6432`, `Program Files`, `Program Files (x86)`, and `LOCALAPPDATA` `chrome.exe` locations. Locked-down machines may still need an explicit executable path.

Config values are validated strictly. Typos such as `"laucn"` for `chrome.mode`, string booleans such as `"false"`, string ports such as `"9222"`, non-list `extra_args`, and `NaN`/`Infinity` timeouts are rejected instead of coerced.

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
browser.navigate("https://example.com")
print(browser.extract_text())
browser.close()
```

## Offline and closed-system use

ZeroCDP itself makes no requests beyond the local Chrome debugging endpoint. All it needs is:

- Python 3.9+ (standard library only — no pip install required for the module itself)
- A Chrome or Chromium binary reachable on the local machine

In air-gapped or locked-down environments, copy `bare_cdp.py` into the project and import
it directly — no package manager, no registry access.
