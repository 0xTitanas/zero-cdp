# Contributing to ZeroCDP

## The stdlib-only invariant

ZeroCDP's core design constraint: **`zero_cdp.py` imports only Python standard-library modules**.
No third-party packages may be added to runtime imports, even as optional dependencies.
Before opening a PR that adds an import, verify the module is available on a stock Python
installation without `pip install`.

The test suite enforces this: the import-audit test reads `zero_cdp.py` and asserts that every
top-level imported module resolves from stdlib.

## Running the tests

```bash
# Syntax check
python -m py_compile zero_cdp.py tests/test_zero_cdp.py

# Unit tests (ResourceWarning treated as errors)
python -W error::ResourceWarning -m unittest discover -s tests -v

# CLI smoke
python -m zero_cdp --help

# After pip install:
zero-cdp --help
```

No third-party packages are required to run the tests.

## Security

ZeroCDP controls a live browser process. A few hard constraints:

- **127.0.0.1 only**: the default host is always `127.0.0.1`. Do not add code or flags that
  bind the debugging port to a routable address without an explicit security review.
- **Selector safety**: all CSS selectors and text values passed to JavaScript must go through
  `json.dumps()`. New JS execution paths must follow the same pattern.
- **No credential logging**: extraction and screenshot helpers must not log cookies, auth tokens,
  or raw page content. If you add logging, document the sensitivity clearly.

Report security issues privately before opening a public issue.

## Live Chrome smoke test

The live Chrome smoke is not part of the unit suite. Run this locally before submitting
changes that touch launch, navigation, input, click, or screenshot behavior:

```bash
# Start Chrome with a throwaway profile
chrome --remote-debugging-port=9222 \
       --user-data-dir=/tmp/zero-cdp-smoke \
       --headless=new \
       --no-first-run

# In another terminal
python -m zero_cdp --navigate https://example.com --extract-text
zero-cdp --navigate https://example.com --extract-text
```

## Pull requests

- Keep the module single-file (`zero_cdp.py`).
- Add or update unit tests for any new behavior. Tests must be stdlib-only (no pytest,
  no third-party fixtures).
- Update `CHANGELOG.md` under `## [Unreleased]` with a brief description of the change.
