# Changelog

All notable changes to BareCDP are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.1.1] — 2026-06-19

### Added
- Exception hierarchy with backward-compatible built-in bases:
  `CDPConnectionError`, `CDPProtocolError`, `CDPTimeoutError`, `CDPCommandError`, and
  `SelectorError`.
- `wait_until_ready()` and `ready_timeout` support in `launch_chrome()`.
- `terminate_chrome()` helper for process shutdown and BareCDP-created temp profile cleanup.
- Context manager support for `CDPConnection` and `Browser`.
- `bare-cdp` console entry point (equivalent to `python -m bare_cdp`; installed by `pip install`).

### Changed
- `CDPConnection.events` is now a bounded `collections.deque(maxlen=2000)` rather than an
  unbounded list, preventing event-buffer growth during long sessions.

### Fixed
- **Selector safety**: `input_text()` now raises `SelectorError` when the focus/clear script
  fails, preventing text from being inserted into the wrong focused element.
- **Navigation race**: `navigate(wait=True)` now waits for the matching
  `Page.frameStoppedLoading` event and checks `Page.navigate` `errorText`.
- **Launch race**: `launch_chrome()` now waits for `/json/version` before returning and raises
  if Chrome exits early or never becomes ready.
- **CDP errors**: JSON-RPC responses with `error` now raise `CDPCommandError`.
- **WebSocket hardening**: inbound frames are size-capped; ping/pong, close frames,
  fragmentation, and socket timeouts are handled more defensively.
- **Process cleanup**: Chrome launched via `launch_chrome()` can be terminated with
  `terminate_chrome()`, which also removes BareCDP-created temporary profiles. `Browser.close()`
  performs the same cleanup when the browser was launched through `Browser.from_config()`.

### Documented
- Security guide (`docs/security.md`), configuration reference (`docs/configuration.md`),
  limitations overview (`docs/limitations.md`), and contribution guide (`CONTRIBUTING.md`).

## [0.1.0] — initial release

### Added
- stdlib-only Chrome DevTools Protocol client with zero runtime dependencies.
- RFC-6455 WebSocket: handshake, frame masking/unmasking, fragmentation, ping/pong, close.
- `CDPConnection`: `call()`, `navigate()`, `evaluate()`, `wait_for_selector()`, `click()`,
  `input_text()`, `press()`, `extract_text()`, `extract_html()`, `screenshot()`.
- `Browser` (`ChromeCDPAdapter`): connect to running Chrome, config-driven launch,
  multi-target selection, new-tab helper.
- `launch_chrome()`: auto-discovers Chrome/Chromium on macOS and Linux.
- CLI (`python -m bare_cdp`) with navigation, text/HTML extraction, screenshot, JS eval,
  and config file generation.
- JSON config file (`bare-cdp.json`) with environment variable overrides.
- Unit tests with a fake stdlib WebSocket/CDP server (no Chrome required).
