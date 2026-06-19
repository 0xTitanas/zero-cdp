# Limitations

BareCDP is intentionally small. It is not a full Playwright or Selenium replacement.

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

- **One command at a time**: `CDPConnection` does not multiplex concurrent CDP calls;
  commands are serialized within a single connection.
- **No async API**: the client is synchronous and sends one command at a time per
  `CDPConnection`; use threads, processes, or orchestrator-level fan-out for concurrent work.
- **Deliberately small wait model**: navigation waits for the target frame to stop loading,
  and `wait_for_selector()` polls a CSS selector. BareCDP does not implement Playwright-style
  actionability checks, retrying locators, or network-idle heuristics.
- **Single browser engine**: BareCDP targets Chrome/Chromium CDP. It does not provide
  cross-browser abstraction for Firefox/WebKit.
