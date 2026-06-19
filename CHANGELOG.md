# Changelog

All notable changes to BareCDP are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- Explicit `Browser` / `ChromeCDPAdapter` pass-through methods for common page actions,
  making orchestrator-facing verbs discoverable to readers, IDEs, and type checkers.
- Single-file provenance metadata (`__author__`, `__license__`, `__url__`) for vendored copies.

### Documented
- Added README vendoring guidance with source, issue tracker, and license links.

## [0.2.0] — 2026-06-19

### Added
- `CDPEvent` typed event objects, event cursors, `recent_events()`, and visible
  `dropped_event_count` for bounded queue overflow diagnostics.
- `ANY_SESSION` sentinel and session-aware `wait_for_event(..., session_id=..., after_sequence=...)`.
- `CDPSession` plus `attach_session(target_id)` for flattened CDP target sessions.
- `LaunchedChrome` return object from `launch_chrome()`, carrying process, actual port,
  browser WebSocket URL, profile path, and cleanup ownership.
- `ChromeCDPAdapter.open_connection()` for explicit multi-connection use.

### Changed
- The synchronous contract is now enforced with a connection-level `RLock`; one command or
  event wait owns the WebSocket at a time, and high-level actions run as atomic transactions.
- `call()` now treats wrong response IDs and response `sessionId` mismatches as protocol
  errors instead of retaining unmatched responses as events.
- `navigate()` now enables lifecycle events and correlates cross-document completion using
  `Page.lifecycleEvent` `frameId` + `loaderId`; same-document navigation uses
  `Page.navigatedWithinDocument` after the pre-navigation event cursor.
- `launch_chrome()` defaults to `port=0`, reads `DevToolsActivePort`, verifies `/json/version`,
  and binds to the spawned profile/process instead of assuming a fixed port.
- `ChromeCDPAdapter` now tracks and closes every connection it opens; replacement connects
  the new target before closing the previous connection.

### Fixed
- Prevented stale navigation events and unrelated session events from satisfying later waits.
- Prevented fixed-port discovery from accidentally attaching to an older Chrome instance when
  the caller lets Chrome choose an ephemeral debugging port.
- Startup failures preserve recent Chrome stderr diagnostics in raised errors.

## [0.1.2] — 2026-06-19

### Fixed
- Windows launch discovery now checks PATH via `shutil.which(...)`, common `ProgramW6432`,
  `Program Files`, `Program Files (x86)`, and `LOCALAPPDATA` `chrome.exe` locations, plus Edge-compatible CDP
  binaries. Locked-down Windows machines can still pass `chrome.executable` / `executable=`
  explicitly or use connect mode.

### Documented
- Clarified that BareCDP is synchronous and sends one command at a time per connection; async
  and orchestrator-level concurrency are outside the current API.

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
