# Limitations

ZeroCDP is intentionally small. It is not a full Playwright or Selenium replacement.

## Not yet implemented

- Full locator engine (no auto-waiting heuristics, no nth-match, no chained filters)
- Browser-context or incognito-context abstraction
- Request interception and network routing wrappers
- HAR, video, and tracing helpers
- Frame and shadow-DOM convenience layer
- Download and upload helpers

Most of these capabilities are reachable via `CDPConnection.call()` using raw CDP commands.
Wrappers are not implemented yet.

## Known gaps

- **One command or event wait at a time**: `CDPConnection` does not multiplex concurrent CDP calls;
  socket access is serialized within a single connection.
- **No async API**: the client is synchronous; use threads, processes, separate connections,
  or orchestrator-level fan-out for concurrent work.
- **Deliberately small wait model**: navigation waits for loader-correlated lifecycle events
  or same-document navigation events. `wait_for_selector()` polls a CSS selector and raises
  `SelectorError` immediately for invalid selector syntax. ZeroCDP does not implement
  Playwright-style actionability checks, retrying locators, or network-idle heuristics.
- **Best-effort interaction primitives**: `click()`, `input_text()`, and `press()` are compact
  CDP helpers, not a full actionability engine. They are suitable for controlled pages and
  simple smoke checks; complex production UI automation may still need stricter visibility,
  hit-test, controlled-input, and keyboard mapping rules.
- **Minimal Chrome-oriented WebSocket**: the built-in WebSocket path validates the Chrome
  handshake shape, frame sizes, ping/pong, close, and fragmentation enough for local Chrome
  CDP use. It is not intended as a general-purpose RFC-6455 client for arbitrary peers.
- **Single browser engine**: ZeroCDP targets Chrome/Chromium CDP. It does not provide
  cross-browser abstraction for Firefox/WebKit.
