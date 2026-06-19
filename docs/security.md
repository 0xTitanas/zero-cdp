# Security

Chrome remote debugging gives full control over the browser profile — treat the debugging
port as equivalent to local shell access to the browser.

## Keep the port local

- **Always bind to `127.0.0.1`** (the BareCDP default). Never pass `--remote-debugging-address`
  or any flag that exposes the port to a routable address.
- Do not expose the debugging port through a reverse proxy, SSH tunnel to an untrusted host,
  or any network path reachable from outside the machine.
- Firewall the port if other users share the machine.

## `--remote-allow-origins` (optional)

Chrome accepts an optional `--remote-allow-origins=<origins>` flag to restrict which HTTP
`Origin` headers are accepted on WebSocket upgrade. For programmatic Python clients (which
send no `Origin` header), this flag has no effect. If you operate a shared debugging
endpoint accessible from a browser-based DevTools client, restrict origins explicitly:

```bash
chrome --remote-debugging-port=9222 \
       --remote-allow-origins=http://localhost:9222
```

## Use a dedicated profile

- Pass `--user-data-dir` pointing to a directory created solely for automation.
- For CI and untrusted pages, use a throwaway directory and delete it after the run.
- `launch_chrome()` creates a temporary directory automatically when `user_data_dir` is
  not specified. Call `terminate_chrome(proc)` or `Browser.close()` when using a config whose
  `chrome.mode` is `"launch"` so BareCDP can stop Chrome and remove that temporary profile.
  User-supplied profile directories are preserved.

## Profile hygiene

- Do not log cookies, tokens, local storage, or raw authenticated page dumps.
- Do not automate credential entry through generic scripts.
- Do not reuse an automation profile for personal browsing.

## Safe launch shape

```bash
chrome \
  --remote-debugging-port=9222 \
  --user-data-dir=/tmp/bare-cdp-profile \
  --no-first-run \
  --no-default-browser-check \
  --disable-extensions
```

For headless CI:

```bash
chrome \
  --remote-debugging-port=9222 \
  --user-data-dir=/tmp/bare-cdp-profile \
  --headless=new \
  --no-first-run \
  --no-default-browser-check \
  --disable-extensions
```
