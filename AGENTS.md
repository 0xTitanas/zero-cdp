# Repository Guidelines

## Project Boundary

ZeroCDP is a small, auditable, synchronous Chrome DevTools Protocol client with zero runtime dependencies. It is not Playwright, Selenium, a browser farm, a stealth layer, or a general WebSocket framework.

Preserve the core promise: one compact Python browser-control layer that can be vendored, inspected, and tested with standard-library tooling.

## Development Commands

Run from the repository root:

```sh
python -m py_compile zero_cdp.py tests/test_zero_cdp.py tests/test_live_chrome.py examples/*.py
python -m unittest discover -s tests -v
python -m zero_cdp --help
python -S -c "import sys; sys.path.insert(0, '.'); import zero_cdp; print(zero_cdp.__version__)"
```

Use fake-CDP tests for normal verification. Treat live Chrome checks as explicit smoke tests, not a substitute for unit/protocol coverage.

## Coding Rules

- Keep runtime imports in the Python standard library only.
- Prefer direct, readable protocol code over broad abstraction layers.
- Keep APIs synchronous unless a task explicitly changes the public direction.
- Preserve strict config validation and clear errors for unsafe or ambiguous launch/connect settings.
- Do not add stealth, anti-bot, credential extraction, password/2FA automation, or authenticated-page dumping features.
- When adding examples, keep them safe for local/disposable pages or clearly document the target.

## Browser Safety

Chrome remote debugging is powerful. Bind debugging endpoints to 127.0.0.1, use dedicated or temporary profiles, never expose CDP ports through public tunnels/proxies, and never log cookies, tokens, local storage, or raw authenticated page dumps.

Ask before changing browser-debugging defaults, expanding live-browser coverage, or adding any behavior that touches real user browser profiles.
