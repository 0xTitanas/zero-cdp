#!/usr/bin/env python3
"""
ZeroCDP: zero-dependency Chrome DevTools Protocol automation for Python.

ZeroCDP is a stdlib-only browser control layer for scripts and orchestrators
that need to drive Chrome/Chromium without Playwright, Selenium, WebDriver,
or runtime dependencies. It talks directly to Chrome's DevTools Protocol over
an intentionally minimal Chrome-oriented WebSocket implemented with the Python standard library.

Quick start with a running Chrome debug port:

    chrome --remote-debugging-port=9222 --user-data-dir=/tmp/zero-cdp-profile

    from zero_cdp import Browser
    browser = Browser(port=9222)
    browser.navigate("https://example.com")
    print(browser.extract_text())
    browser.close()

Launch Chrome from Python:

    from zero_cdp import Browser, launch_chrome, terminate_chrome

    launch = launch_chrome(headless=True)
    browser = Browser(port=launch.port)
    try:
        browser.navigate("https://example.com")
    finally:
        browser.close()
        terminate_chrome(launch)

Use a JSON config file:

    from zero_cdp import Browser

    browser = Browser.from_config("zero-cdp.json")
    browser.navigate("https://example.com")
    print(browser.extract_text())
    browser.close()

CLI examples:

    python -m zero_cdp --navigate https://example.com --extract-text
    python -m zero_cdp --eval "document.title"
    python -m zero_cdp --screenshot page.png
"""

import argparse
import base64
import collections
import contextlib
import copy
import dataclasses
import hashlib
import json
import math
import os
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional

__version__ = "0.2.3"
__author__ = "ZeroCDP contributors"
__license__ = "MIT"
__url__ = "https://github.com/0xTitanas/zero-cdp"


_WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_WS_OPCODE_CONTINUATION = 0x0
_WS_OPCODE_TEXT = 0x1
_WS_OPCODE_BINARY = 0x2
_WS_OPCODE_CLOSE = 0x8
_WS_OPCODE_PING = 0x9
_WS_OPCODE_PONG = 0xA
_WS_MAX_PAYLOAD = 64 * 1024 * 1024  # 64 MiB

ANY_SESSION = object()


@dataclasses.dataclass(frozen=True)
class CDPEvent:
    sequence: int
    method: str
    params: Dict[str, Any]
    session_id: Optional[str]


@dataclasses.dataclass
class LaunchedChrome:
    process: subprocess.Popen
    port: int
    browser_ws_url: str
    user_data_dir: str
    owns_user_data_dir: bool
    stderr_path: Optional[str] = None

    def terminate(self, timeout: float = 5.0) -> None:
        terminate_chrome(self, timeout=timeout)


# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------

class CDPError(Exception):
    """Base exception for all ZeroCDP errors."""


class CDPConnectionError(CDPError, ConnectionError):
    """WebSocket or transport-level failure."""


class CDPProtocolError(CDPError):
    """Unexpected or malformed CDP protocol data."""


class CDPTimeoutError(CDPError, TimeoutError):
    """A CDP call or readiness wait exceeded its deadline."""


class CDPCommandError(CDPError, RuntimeError):
    """Chrome returned an error for a CDP command."""

    def __init__(
        self,
        message: str,
        method: Optional[str] = None,
        code: Optional[int] = None,
        data: Any = None,
        session_id: Optional[str] = None,
    ):
        self.method = method
        self.code = code
        self.message = message
        self.data = data
        self.session_id = session_id
        prefix = f"{method}: " if method else ""
        code_part = f"[{code}] " if code is not None else ""
        super().__init__(f"{prefix}{code_part}{message}")

    @classmethod
    def from_response(
        cls,
        method: str,
        response: Dict[str, Any],
        session_id: Optional[str] = None,
    ) -> "CDPCommandError":
        return cls(
            message=response.get("message", "Unknown CDP error"),
            method=method,
            code=response.get("code"),
            data=response.get("data"),
            session_id=session_id,
        )


class NavigationError(CDPCommandError):
    """Page.navigate returned an errorText for the requested URL."""

    def __init__(self, url: str, error_text: str, frame_id: Optional[str] = None):
        self.url = url
        self.error_text = error_text
        self.frame_id = frame_id
        super().__init__(
            message=error_text,
            method="Page.navigate",
            data={"url": url, "frameId": frame_id},
        )


class SelectorError(CDPError, LookupError):
    """A CSS selector matched no element."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_CONFIG: Dict[str, Any] = {
    "chrome": {
        "mode": "connect",            # "connect" or "launch"
        "host": "127.0.0.1",
        "port": None,
        "ws_url": None,
        "executable": None,
        "user_data_dir": None,
        "headless": True,
        "extra_args": [],
    },
    "timeouts": {
        "default": 10.0,
    },
}


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(override, dict):
        raise TypeError("config must be a JSON object")
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _bool_from_env(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _validate_timeout(value: Any, name: str = "timeout") -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number greater than zero")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite number greater than zero") from exc
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{name} must be a finite number greater than zero")
    return number


def _validate_port(value: Any, name: str = "port", *, allow_zero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    minimum = 0 if allow_zero else 1
    if not minimum <= value <= 65535:
        if allow_zero:
            raise ValueError(f"{name} must be between 0 and 65535")
        raise ValueError(f"{name} must be between 1 and 65535")
    return value


def _validate_optional_str(value: Any, name: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string or null")
    return value


def _validate_extra_args(value: Any) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise TypeError("chrome.extra_args must be a list of strings")
    for arg in value:
        if not isinstance(arg, str):
            raise TypeError("chrome.extra_args must be a list of strings")
    return value


def _validate_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(cfg, dict):
        raise TypeError("config must be a JSON object")
    chrome = cfg.get("chrome")
    if not isinstance(chrome, dict):
        raise TypeError("chrome config must be an object")
    timeouts = cfg.get("timeouts")
    if not isinstance(timeouts, dict):
        raise TypeError("timeouts config must be an object")

    mode = chrome.get("mode", "connect")
    if mode not in {"connect", "launch"}:
        raise ValueError("chrome.mode must be 'connect' or 'launch'")
    chrome["mode"] = mode
    chrome["host"] = _validate_optional_str(chrome.get("host"), "chrome.host") or "127.0.0.1"
    chrome["ws_url"] = _validate_optional_str(chrome.get("ws_url"), "chrome.ws_url")
    chrome["executable"] = _validate_optional_str(chrome.get("executable"), "chrome.executable")
    chrome["user_data_dir"] = _validate_optional_str(chrome.get("user_data_dir"), "chrome.user_data_dir")
    if not isinstance(chrome.get("headless", True), bool):
        raise TypeError("chrome.headless must be a boolean")
    chrome["extra_args"] = _validate_extra_args(chrome.get("extra_args"))
    if chrome.get("port") is not None:
        chrome["port"] = _validate_port(chrome["port"], "chrome.port", allow_zero=(mode == "launch"))
    default_timeout = timeouts.get("default", 10.0)
    if isinstance(default_timeout, bool) or not isinstance(default_timeout, (int, float)):
        raise TypeError("timeout must be a finite positive number")
    timeouts["default"] = _validate_timeout(default_timeout, "timeout")
    return cfg


def load_config(path: Optional[str] = None) -> Dict[str, Any]:
    """Load ZeroCDP JSON configuration and apply environment overrides.

    Environment variables:
        ZERO_CDP_HOST
        ZERO_CDP_PORT
        ZERO_CDP_WS_URL
        ZERO_CDP_CHROME
        ZERO_CDP_USER_DATA_DIR
        ZERO_CDP_HEADLESS
        ZERO_CDP_TIMEOUT
    """
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if path:
        with open(path, "r", encoding="utf-8") as f:
            cfg = _deep_merge(cfg, json.load(f))

    env_map = {
        "ZERO_CDP_HOST": ("chrome", "host", str),
        "ZERO_CDP_PORT": ("chrome", "port", int),
        "ZERO_CDP_WS_URL": ("chrome", "ws_url", str),
        "ZERO_CDP_CHROME": ("chrome", "executable", str),
        "ZERO_CDP_USER_DATA_DIR": ("chrome", "user_data_dir", str),
        "ZERO_CDP_HEADLESS": ("chrome", "headless", _bool_from_env),
        "ZERO_CDP_TIMEOUT": ("timeouts", "default", float),
    }
    for env_name, (section, key, caster) in env_map.items():
        if env_name in os.environ and os.environ[env_name] != "":
            cfg[section][key] = caster(os.environ[env_name])
    return _validate_config(cfg)


def write_default_config(path: str = "zero-cdp.json") -> str:
    """Write a default JSON config file and return its path."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(DEFAULT_CONFIG, f, indent=2)
        f.write("\n")
    return path


def _mode_default_port(configured_port: Any, launch_mode: bool) -> int:
    if configured_port is None:
        return 0 if launch_mode else 9222
    return _validate_port(configured_port, "port", allow_zero=launch_mode)


def _launch_extra_args(extra_args: Optional[List[str]]) -> List[str]:
    args = _validate_extra_args(extra_args)
    reserved = {
        "--remote-debugging-port",
        "--remote-debugging-address",
        "--user-data-dir",
    }
    for arg in args:
        name = arg.split("=", 1)[0]
        if name in reserved:
            raise ValueError(f"reserved Chrome launch argument: {arg}")
    return args


def _port_is_in_use(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.2):
            return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# WebSocket frame codec
# ---------------------------------------------------------------------------

def _ws_client_key() -> str:
    return base64.b64encode(os.urandom(16)).decode()


def _ws_accept_key(client_key: str) -> str:
    digest = hashlib.sha1((client_key + _WS_MAGIC).encode()).digest()
    return base64.b64encode(digest).decode()


def _ws_encode_frame(payload: bytes, opcode: int = _WS_OPCODE_TEXT) -> bytes:
    """Encode a masked client→server frame (RFC-6455 §5.2)."""
    mask = os.urandom(4)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    n = len(payload)
    header = bytes([0x80 | opcode])
    if n < 126:
        header += bytes([0x80 | n])
    elif n < 65536:
        header += bytes([0x80 | 126]) + struct.pack(">H", n)
    else:
        header += bytes([0x80 | 127]) + struct.pack(">Q", n)
    return header + mask + masked


def _recv_exactly(recv_fn, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = recv_fn(n - len(buf))
        if not chunk:
            raise CDPConnectionError("WebSocket connection closed unexpectedly")
        buf.extend(chunk)
    return bytes(buf)


def _ws_decode_frame(recv_fn) -> tuple:
    """Read one WebSocket frame; return (fin, opcode, payload)."""
    header = _recv_exactly(recv_fn, 2)
    rsv = header[0] & 0x70
    if rsv:
        raise CDPProtocolError("WebSocket frame has unsupported RSV bits set")
    fin = bool(header[0] & 0x80)
    opcode = header[0] & 0x0F
    masked = bool(header[1] & 0x80)
    if masked:
        raise CDPProtocolError("WebSocket server frames must not be masked")
    valid_opcodes = {
        _WS_OPCODE_CONTINUATION,
        _WS_OPCODE_TEXT,
        _WS_OPCODE_BINARY,
        _WS_OPCODE_CLOSE,
        _WS_OPCODE_PING,
        _WS_OPCODE_PONG,
    }
    if opcode not in valid_opcodes:
        raise CDPProtocolError(f"Reserved WebSocket opcode received: {opcode:#x}")
    payload_len = header[1] & 0x7F
    if payload_len == 126:
        payload_len = struct.unpack(">H", _recv_exactly(recv_fn, 2))[0]
    elif payload_len == 127:
        payload_len = struct.unpack(">Q", _recv_exactly(recv_fn, 8))[0]
    if opcode in {_WS_OPCODE_CLOSE, _WS_OPCODE_PING, _WS_OPCODE_PONG}:
        if not fin:
            raise CDPProtocolError("WebSocket control frames must not be fragmented")
        if payload_len > 125:
            raise CDPProtocolError("WebSocket control frame too large")
    if payload_len > _WS_MAX_PAYLOAD:
        raise CDPProtocolError(
            f"WebSocket frame too large: {payload_len} bytes (max {_WS_MAX_PAYLOAD})"
        )
    raw = _recv_exactly(recv_fn, payload_len)
    return fin, opcode, raw


class _WSReceiver:
    """Buffered WebSocket reader; handles fragmentation, ping/pong, and close."""

    def __init__(self, sock: socket.socket, initial_buf: bytes = b""):
        self._sock = sock
        self._buf = bytearray(initial_buf)

    def _recv(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise CDPConnectionError("WebSocket: unexpected EOF")
            self._buf.extend(chunk)
        data = bytes(self._buf[:n])
        del self._buf[:n]
        return data

    def read_message(self) -> tuple:
        """Return (opcode, payload) for the next complete message."""
        fragments: List[bytes] = []
        base_opcode: Optional[int] = None
        while True:
            fin, opcode, payload = _ws_decode_frame(self._recv)
            if opcode == _WS_OPCODE_PING:
                self._sock.sendall(_ws_encode_frame(payload, _WS_OPCODE_PONG))
                continue
            if opcode == _WS_OPCODE_PONG:
                continue
            if opcode == _WS_OPCODE_CLOSE:
                try:
                    self._sock.sendall(_ws_encode_frame(b"", _WS_OPCODE_CLOSE))
                except Exception:
                    pass
                raise CDPConnectionError("WebSocket close frame received")
            if opcode == _WS_OPCODE_CONTINUATION:
                if base_opcode is None:
                    raise CDPProtocolError("WebSocket continuation without an active message")
                fragments.append(payload)
            else:
                if base_opcode is not None:
                    raise CDPProtocolError("New WebSocket message started before fragmented message completed")
                base_opcode = opcode
                fragments = [payload]
            if sum(len(fragment) for fragment in fragments) > _WS_MAX_PAYLOAD:
                raise CDPProtocolError(
                    f"WebSocket message too large after fragmentation (max {_WS_MAX_PAYLOAD})"
                )
            if fin:
                return base_opcode, b"".join(fragments)

    def send_frame(self, payload: bytes, opcode: int = _WS_OPCODE_TEXT):
        self._sock.sendall(_ws_encode_frame(payload, opcode))

    def send_text(self, text: str):
        self.send_frame(text.encode(), _WS_OPCODE_TEXT)

    def send_close(self):
        try:
            self.send_frame(b"", _WS_OPCODE_CLOSE)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Keyboard helpers
# ---------------------------------------------------------------------------

_KEY_CODES = {
    "Enter": ("Enter", 13),
    "Tab": ("Tab", 9),
    "Escape": ("Escape", 27),
    "Esc": ("Escape", 27),
    "Backspace": ("Backspace", 8),
    "Delete": ("Delete", 46),
    "ArrowLeft": ("ArrowLeft", 37),
    "ArrowUp": ("ArrowUp", 38),
    "ArrowRight": ("ArrowRight", 39),
    "ArrowDown": ("ArrowDown", 40),
    "Home": ("Home", 36),
    "End": ("End", 35),
    "PageUp": ("PageUp", 33),
    "PageDown": ("PageDown", 34),
}


def _key_event_info(key: str) -> Dict[str, Any]:
    key_name, vk = _KEY_CODES.get(key, (key, ord(key.upper()) if len(key) == 1 else 0))
    info: Dict[str, Any] = {"key": key_name, "code": key_name}
    if vk:
        info["windowsVirtualKeyCode"] = vk
        info["nativeVirtualKeyCode"] = vk
    if len(key) == 1:
        info["text"] = key
        info["unmodifiedText"] = key
    return info



def _urls_equivalent(observed: Optional[str], expected: str) -> bool:
    if observed == expected:
        return True
    if not observed:
        return False

    def normalize(value: str):
        parsed = urllib.parse.urlsplit(value)
        scheme = parsed.scheme.lower()
        netloc = parsed.netloc.lower()
        path = parsed.path or "/"
        return (scheme, netloc, path, parsed.query, parsed.fragment)

    try:
        return normalize(observed) == normalize(expected)
    except Exception:
        return False


def _is_transient_evaluation_error(exc: Exception) -> bool:
    text = str(exc)
    return isinstance(exc, CDPCommandError) and any(
        needle in text
        for needle in (
            "Execution context was destroyed",
            "Cannot find context with specified id",
            "Inspected target navigated",
        )
    )


# ---------------------------------------------------------------------------
# CDPConnection
# ---------------------------------------------------------------------------

class CDPConnection:
    """Low-level Chrome DevTools Protocol connection over raw WebSocket.

    The public contract is intentionally synchronous: one active command or
    event wait per connection. Events that arrive while a command is pending
    are queued with sequence numbers so later waits can correlate them without
    permitting concurrent JSON-RPC dispatch on the same socket.
    """

    def __init__(self, ws_url: str, timeout: float = 10.0):
        self._ws_url = ws_url
        self._timeout = _validate_timeout(timeout)
        self._id = 0
        self._sock: Optional[socket.socket] = None
        self._ws: Optional[_WSReceiver] = None
        self._io_lock = threading.RLock()
        self._event_sequence = 0
        self._events: collections.deque = collections.deque(maxlen=2000)
        self._dropped_event_count = 0
        self._page_enabled = False
        self._connect()

    def __enter__(self) -> "CDPConnection":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        self.close()
        return False

    @property
    def closed(self) -> bool:
        return self._sock is None or self._ws is None

    @property
    def events(self):
        """Snapshot of queued typed CDPEvent objects."""
        with self._io_lock:
            return tuple(self._events)

    @property
    def dropped_event_count(self) -> int:
        with self._io_lock:
            return self._dropped_event_count

    def recent_events(self) -> tuple:
        return self.events

    def event_cursor(self) -> int:
        with self._io_lock:
            return self._event_sequence

    @contextlib.contextmanager
    def transaction(self):
        """Serialize a multi-command high-level operation on this connection."""
        with self._io_lock:
            yield

    def _resolve_timeout(self, timeout: Optional[float]) -> float:
        return _validate_timeout(self._timeout if timeout is None else timeout)

    def _make_deadline(self, timeout: Optional[float]) -> float:
        return time.monotonic() + self._resolve_timeout(timeout)

    def _remaining(self, deadline: float, operation: str) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CDPTimeoutError(f"{operation} timed out")
        return remaining

    def _ensure_open(self) -> None:
        if self._ws is None or self._sock is None:
            raise CDPConnectionError("CDPConnection is closed")

    def _connect(self):
        parsed = urllib.parse.urlparse(self._ws_url)
        if parsed.scheme not in {"ws", "wss"}:
            raise ValueError("ws_url must use ws:// or wss://")
        if not parsed.hostname:
            raise ValueError("ws_url must include a host")
        host = parsed.hostname
        use_ssl = parsed.scheme == "wss"
        port = parsed.port or (443 if use_ssl else 80)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query

        try:
            raw = socket.create_connection((host, port), timeout=self._timeout)
        except OSError as exc:
            raise CDPConnectionError(f"Could not connect to CDP WebSocket at {self._ws_url}") from exc
        try:
            if use_ssl:
                ctx = ssl.create_default_context()
                raw = ctx.wrap_socket(raw, server_hostname=host)
            raw.settimeout(self._timeout)
            self._sock = raw

            key = _ws_client_key()
            handshake = (
                f"GET {path} HTTP/1.1\r\n"
                f"Host: {host}:{port}\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n"
                "\r\n"
            )
            raw.sendall(handshake.encode())

            resp = bytearray()
            while b"\r\n\r\n" not in resp:
                chunk = raw.recv(4096)
                if not chunk:
                    raise CDPConnectionError("WebSocket handshake: server closed connection")
                resp.extend(chunk)
                if len(resp) > 65536:
                    raise CDPConnectionError("WebSocket handshake header exceeded 65536 bytes")

            parts = bytes(resp).split(b"\r\n\r\n", 1)
            header_text = parts[0].decode(errors="replace")
            leftover = parts[1] if len(parts) > 1 else b""

            lines = header_text.split("\r\n")
            if not lines[0].startswith("HTTP/1.1 101"):
                raise CDPConnectionError(f"WebSocket upgrade failed: {lines[0]}")

            resp_headers: Dict[str, str] = {}
            for line in lines[1:]:
                if ":" in line:
                    k, _, v = line.partition(":")
                    resp_headers[k.lower()] = v.strip()

            upgrade = resp_headers.get("upgrade", "").lower()
            connection_tokens = {
                token.strip().lower()
                for token in resp_headers.get("connection", "").split(",")
            }
            if upgrade != "websocket" or "upgrade" not in connection_tokens:
                raise CDPConnectionError("WebSocket handshake missing Upgrade/Connection headers")

            expected = _ws_accept_key(key)
            got = resp_headers.get("sec-websocket-accept", "")
            if got != expected:
                raise CDPConnectionError(
                    f"Sec-WebSocket-Accept mismatch: expected {expected!r}, got {got!r}"
                )

            self._ws = _WSReceiver(raw, leftover)
        except OSError as exc:
            self._sock = None
            try:
                raw.close()
            finally:
                raise CDPConnectionError("CDP transport failed during WebSocket handshake") from exc
        except Exception:
            self._sock = None
            try:
                raw.close()
            finally:
                raise

    def _read_cdp_message(self, deadline: float) -> Dict[str, Any]:
        self._ensure_open()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CDPTimeoutError("CDP receive timed out")
        self._sock.settimeout(remaining)  # type: ignore[union-attr]
        try:
            opcode, payload = self._ws.read_message()  # type: ignore[union-attr]
        except socket.timeout:
            self.close()
            raise CDPTimeoutError("CDP receive timed out")
        except (CDPProtocolError, CDPConnectionError):
            self.close()
            raise
        except OSError as exc:
            self.close()
            raise CDPConnectionError("CDP transport failed while receiving") from exc
        if opcode != _WS_OPCODE_TEXT:
            self.close()
            raise CDPProtocolError(
                f"Expected WebSocket text message, received opcode {opcode:#x}"
            )
        try:
            data = json.loads(payload.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self.close()
            raise CDPProtocolError("Invalid CDP JSON message") from exc
        if not isinstance(data, dict):
            self.close()
            raise CDPProtocolError("CDP message must be a JSON object")
        has_id = "id" in data
        has_method = isinstance(data.get("method"), str)
        if has_id == has_method:
            self.close()
            raise CDPProtocolError("CDP message must be either a response or an event")
        return data

    def _make_event(self, data: Dict[str, Any]) -> CDPEvent:
        params = data.get("params", {})
        if params is None:
            params = {}
        if not isinstance(params, dict):
            self.close()
            raise CDPProtocolError("CDP event params must be a JSON object")
        self._event_sequence += 1
        return CDPEvent(
            sequence=self._event_sequence,
            method=data["method"],
            params=params,
            session_id=data.get("sessionId"),
        )

    def _append_event(self, event: CDPEvent) -> None:
        if len(self._events) == self._events.maxlen:
            self._dropped_event_count += 1
        self._events.append(event)

    def _queue_event(self, data: Dict[str, Any]) -> CDPEvent:
        event = self._make_event(data)
        self._append_event(event)
        return event

    def call(
        self,
        method: str,
        params: Optional[Dict] = None,
        timeout: Optional[float] = None,
        session_id: Optional[str] = None,
    ) -> Dict:
        """Send one CDP command and return the matching response result."""
        if not method or not isinstance(method, str):
            raise ValueError("method must be a non-empty string")
        if params is not None and not isinstance(params, dict):
            raise TypeError("params must be a dict when provided")
        if session_id is not None and (not isinstance(session_id, str) or not session_id):
            raise ValueError("session_id must be a non-empty string when provided")
        with self._io_lock:
            self._ensure_open()
            duration = self._resolve_timeout(timeout)
            self._id += 1
            call_id = self._id
            msg: Dict[str, Any] = {"id": call_id, "method": method}
            if params is not None:
                msg["params"] = params
            if session_id is not None:
                msg["sessionId"] = session_id
            encoded = json.dumps(msg, separators=(",", ":"))
            deadline = time.monotonic() + duration
            try:
                self._ws.send_text(encoded)  # type: ignore[union-attr]

                while True:
                    data = self._read_cdp_message(deadline)
                    if "method" in data:
                        self._queue_event(data)
                        continue
                    if data.get("id") != call_id:
                        self.close()
                        raise CDPProtocolError(
                            f"Unexpected CDP response id {data.get('id')!r}; expected {call_id}"
                        )
                    response_session = data.get("sessionId")
                    if response_session != session_id:
                        self.close()
                        raise CDPProtocolError(
                            f"Response session mismatch: expected {session_id!r}, received {response_session!r}"
                        )
                    if "error" in data:
                        raise CDPCommandError.from_response(
                            method=method,
                            response=data["error"],
                            session_id=session_id,
                        )
                    return data.get("result", {})
            except CDPTimeoutError:
                self.close()
                raise
            except OSError as exc:
                self.close()
                raise CDPConnectionError(
                    f"CDP transport failed during {method}"
                ) from exc

    def close(self):
        with self._io_lock:
            ws, self._ws = self._ws, None
            sock, self._sock = self._sock, None
        if ws:
            try:
                ws.send_close()
            except Exception:
                pass
        if sock:
            try:
                sock.close()
            except Exception:
                pass

    def _enable_page_domain(self, deadline: Optional[float] = None):
        """Enable the Page domain and lifecycle events idempotently."""
        if self._page_enabled:
            return
        if deadline is None:
            deadline = self._make_deadline(None)
        self.call("Page.enable", timeout=self._remaining(deadline, "Page.enable"))
        self.call(
            "Page.setLifecycleEventsEnabled",
            {"enabled": True},
            timeout=self._remaining(deadline, "Page.setLifecycleEventsEnabled"),
        )
        self._page_enabled = True

    def _matches_event(
        self,
        event: CDPEvent,
        event_names: set,
        predicate: Optional[Callable[[Dict], bool]],
        session_id: Any,
        after_sequence: Optional[int],
    ) -> bool:
        if event.method not in event_names:
            return False
        if session_id is not ANY_SESSION and event.session_id != session_id:
            return False
        if after_sequence is not None and event.sequence <= after_sequence:
            return False
        return predicate is None or predicate(event.params)

    def wait_for_event(
        self,
        event_name: Any,
        predicate: Optional[Callable[[Dict], bool]] = None,
        timeout: Optional[float] = None,
        *,
        session_id: Any = None,
        after_sequence: Optional[int] = None,
    ) -> Dict:
        """Wait for a CDP event, respecting flattened-session routing."""
        event_names = {event_name} if isinstance(event_name, str) else set(event_name)
        deadline = self._make_deadline(timeout)
        with self._io_lock:
            remaining_events: collections.deque = collections.deque(maxlen=self._events.maxlen)
            found: Optional[CDPEvent] = None
            for event in self._events:
                if found is None and self._matches_event(
                    event, event_names, predicate, session_id, after_sequence
                ):
                    found = event
                    continue
                remaining_events.append(event)
            self._events = remaining_events
            if found is not None:
                return found.params

            self._ensure_open()
            while True:
                data = self._read_cdp_message(deadline)
                if "method" not in data:
                    self.close()
                    raise CDPProtocolError(
                        f"Unexpected CDP response id {data.get('id')!r} while waiting for event"
                    )
                event = self._make_event(data)
                if self._matches_event(event, event_names, predicate, session_id, after_sequence):
                    return event.params
                self._append_event(event)

    def evaluate(self, expression: str, return_by_value: bool = True, timeout: Optional[float] = None) -> Any:
        """Evaluate JavaScript in the current page and return the CDP value."""
        result = self.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": return_by_value, "awaitPromise": True},
            timeout=timeout,
        )
        remote = result.get("result", {})
        if result.get("exceptionDetails"):
            raise CDPCommandError(f"Runtime.evaluate exception: {result['exceptionDetails']}", method="Runtime.evaluate")
        return remote.get("value") if return_by_value else remote

    def navigate(
        self,
        url: str,
        wait: bool = True,
        timeout: Optional[float] = None,
        *,
        wait_until: str = "load",
    ) -> Dict:
        """Navigate and correlate completion to the returned loader/session state."""
        if wait_until not in {"commit", "DOMContentLoaded", "load"}:
            raise ValueError("wait_until must be 'commit', 'DOMContentLoaded', or 'load'")
        if not wait:
            wait_until = "commit"
        with self.transaction():
            deadline = self._make_deadline(timeout)
            self._enable_page_domain(deadline)
            cursor = self.event_cursor()
            nav = self.call(
                "Page.navigate",
                {"url": url},
                timeout=self._remaining(deadline, "Page.navigate"),
            )
            if nav.get("errorText"):
                raise NavigationError(
                    url=url,
                    error_text=nav["errorText"],
                    frame_id=nav.get("frameId"),
                )
            if wait_until == "commit" or nav.get("isDownload"):
                return nav
            frame_id = nav.get("frameId")
            loader_id = nav.get("loaderId")
            if loader_id:
                self.wait_for_event(
                    "Page.lifecycleEvent",
                    predicate=lambda params: (
                        params.get("frameId") == frame_id
                        and params.get("loaderId") == loader_id
                        and params.get("name") == wait_until
                    ),
                    timeout=self._remaining(deadline, "navigation lifecycle"),
                    after_sequence=cursor,
                )
            else:
                self.wait_for_event(
                    "Page.navigatedWithinDocument",
                    predicate=lambda params: (
                        params.get("frameId") == frame_id
                        and _urls_equivalent(params.get("url"), url)
                    ),
                    timeout=self._remaining(deadline, "same-document navigation"),
                    after_sequence=cursor,
                )
            return nav

    def wait_for_ready_state(self, states: tuple = ("interactive", "complete"), timeout: Optional[float] = None) -> str:
        deadline = self._make_deadline(timeout)
        last = ""
        last_transient: Optional[Exception] = None
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                last = self.evaluate("document.readyState", timeout=max(0.001, remaining)) or ""
                if last in states:
                    return last
            except CDPCommandError as exc:
                if not _is_transient_evaluation_error(exc):
                    raise
                last_transient = exc
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        raise CDPTimeoutError(
            f"document.readyState did not reach {states}; last={last!r}"
        ) from last_transient

    def wait_for_selector(self, selector: str, timeout: Optional[float] = None) -> bool:
        deadline = self._make_deadline(timeout)
        last_transient: Optional[Exception] = None
        selector_j = json.dumps(selector)
        expression = (
            "(function(){"
            "try{"
            f"var el=document.querySelector({selector_j});"
            "return {valid:true,found:el!==null};"
            "}catch(error){"
            "return {valid:false,name:error.name,message:error.message};"
            "}"
            "})()"
        )
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                value = self.evaluate(expression, timeout=max(0.001, remaining))
                if isinstance(value, dict):
                    if value.get("valid") is False:
                        detail = value.get("message") or value.get("name") or "selector rejected by querySelector"
                        raise SelectorError(f"Invalid selector {selector!r}: {detail}")
                    if value.get("found"):
                        return True
            except CDPCommandError as exc:
                if not _is_transient_evaluation_error(exc):
                    raise
                last_transient = exc
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        raise CDPTimeoutError(f"Selector {selector!r} not found within timeout") from last_transient

    def click(self, selector: str):
        """Click a CSS selector using real CDP mouse events when possible."""
        with self.transaction():
            js = (
                "(function(){"
                f"var el=document.querySelector({json.dumps(selector)});"
                "if(!el){return null;}"
                "el.scrollIntoView({block:'center',inline:'center'});"
                "var r=el.getBoundingClientRect();"
                "return {x:r.left+r.width/2,y:r.top+r.height/2};"
                "})()"
            )
            point = self.evaluate(js)
            if not point:
                raise SelectorError(f"Selector {selector!r} not found")
            x = float(point["x"])
            y = float(point["y"])
            self.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
            self.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left", "buttons": 1, "clickCount": 1})
            self.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left", "buttons": 0, "clickCount": 1})

    def input_text(self, selector: str, text: str, clear: bool = True, press_enter: bool = False):
        """Focus element, optionally clear, insert text, optionally press Enter."""
        with self.transaction():
            sel_j = json.dumps(selector)
            js = (
                "(function(){"
                f"var el=document.querySelector({sel_j});"
                "if(!el){throw new Error('selector not found: '+" + sel_j + ");}"
                "el.focus();"
            )
            if clear:
                js += (
                    "if('value' in el){el.value='';}"
                    "el.dispatchEvent(new Event('input',{bubbles:true}));"
                    "el.dispatchEvent(new Event('change',{bubbles:true}));"
                )
            js += "return true;})()"
            try:
                self.evaluate(js)
            except CDPCommandError as exc:
                if "selector not found" in str(exc):
                    raise SelectorError(f"Selector {selector!r} not found") from exc
                raise
            if text:
                self.call("Input.insertText", {"text": text})
            if press_enter:
                self.press("Enter")

    def press(self, key: str):
        with self.transaction():
            info = _key_event_info(key)
            down = {"type": "keyDown", **info}
            up = {"type": "keyUp", **info}
            self.call("Input.dispatchKeyEvent", down)
            self.call("Input.dispatchKeyEvent", up)

    def extract_text(self, selector: Optional[str] = None) -> str:
        """Extract innerText from a selector element, or document.body."""
        if selector is not None:
            js = f"(document.querySelector({json.dumps(selector)})||{{innerText:''}}).innerText"
        else:
            js = "document.body ? document.body.innerText : ''"
        result = self.call("Runtime.evaluate", {"expression": js, "returnByValue": True})
        return result.get("result", {}).get("value", "")

    def extract_html(self, selector: Optional[str] = None) -> str:
        if selector is not None:
            js = f"(document.querySelector({json.dumps(selector)})||{{outerHTML:''}}).outerHTML"
        else:
            js = "document.documentElement ? document.documentElement.outerHTML : ''"
        result = self.call("Runtime.evaluate", {"expression": js, "returnByValue": True})
        return result.get("result", {}).get("value", "")

    def screenshot(self, path: Optional[str] = None, format: str = "png") -> bytes:
        """Capture a screenshot. If ``path`` is provided, write it and return bytes."""
        result = self.call("Page.captureScreenshot", {"format": format})
        data = base64.b64decode(result.get("data", ""))
        if path:
            with open(path, "wb") as f:
                f.write(data)
        return data

    def attach_session(self, target_id: str) -> "CDPSession":
        """Attach to a target with flattened sessions and return a bound session."""
        result = self.call("Target.attachToTarget", {"targetId": target_id, "flatten": True})
        session_id = result.get("sessionId")
        if not session_id:
            raise CDPCommandError("Target.attachToTarget did not return a sessionId", method="Target.attachToTarget")
        return CDPSession(self, session_id)

    def attach_to_target(self, target_id: str) -> str:
        """Deprecated compatibility helper returning only the flattened sessionId."""
        return self.attach_session(target_id).session_id


class CDPSession:
    """Session-bound facade for flattened Target.attachToTarget sessions."""

    def __init__(self, connection: CDPConnection, session_id: str):
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("session_id must be a non-empty string")
        self.connection = connection
        self.session_id = session_id

    def call(self, method: str, params: Optional[Dict] = None, timeout: Optional[float] = None) -> Dict:
        return self.connection.call(method, params, timeout=timeout, session_id=self.session_id)

    def wait_for_event(
        self,
        event_name: Any,
        predicate: Optional[Callable[[Dict], bool]] = None,
        timeout: Optional[float] = None,
        **kwargs: Any,
    ) -> Dict:
        if "session_id" in kwargs:
            raise TypeError(
                "CDPSession.wait_for_event() is already bound to its session; "
                "use the parent CDPConnection for cross-session waits"
            )
        return self.connection.wait_for_event(
            event_name,
            predicate,
            timeout,
            session_id=self.session_id,
            **kwargs,
        )

    def detach(self) -> None:
        self.connection.call("Target.detachFromTarget", {"sessionId": self.session_id})


# ---------------------------------------------------------------------------
# Orchestrator-friendly high-level wrapper
# ---------------------------------------------------------------------------

class ChromeCDPAdapter:
    """High-level orchestrator wrapper for owned CDP connections."""

    def __init__(self, host: str = "127.0.0.1", port: int = 9222, timeout: float = 10.0):
        self._host = host
        self._port = _validate_port(port, "port", allow_zero=False)
        self._timeout = _validate_timeout(timeout)
        self._conn: Optional[CDPConnection] = None
        self._connections: set = set()
        self._launch: Optional[LaunchedChrome] = None

    def __enter__(self) -> "ChromeCDPAdapter":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        self.close()
        return False

    def connect(self, ws_url: Optional[str] = None, *, replace: bool = True) -> CDPConnection:
        if ws_url is None:
            ws_url = discover_ws_url(self._host, self._port, timeout=self._timeout)
        new_connection = CDPConnection(ws_url, timeout=self._timeout)
        previous = self._conn
        self._connections.add(new_connection)
        self._conn = new_connection
        if replace and previous is not None:
            previous.close()
            self._connections.discard(previous)
        return new_connection

    def open_connection(self, ws_url: str) -> CDPConnection:
        connection = CDPConnection(ws_url, timeout=self._timeout)
        self._connections.add(connection)
        return connection

    @classmethod
    def from_config(cls, path: Optional[str] = None) -> "ChromeCDPAdapter":
        cfg = load_config(path)
        chrome = cfg["chrome"]
        timeout = _validate_timeout(cfg["timeouts"].get("default", 10.0))
        launch_mode = chrome.get("mode") == "launch"
        if launch_mode and chrome.get("ws_url"):
            raise ValueError("chrome.ws_url cannot be used with launch mode")
        host = chrome.get("host") or "127.0.0.1"
        port = _mode_default_port(chrome.get("port"), launch_mode)
        browser = cls(host=host, port=(port if port != 0 else 9222), timeout=timeout)
        try:
            if launch_mode:
                launch = launch_chrome(
                    executable=chrome.get("executable"),
                    port=port,
                    headless=chrome.get("headless", True),
                    user_data_dir=chrome.get("user_data_dir"),
                    extra_args=chrome.get("extra_args") or [],
                )
                browser._launch = launch
                browser._host = "127.0.0.1"
                browser._port = launch.port
            elif chrome.get("ws_url"):
                browser.connect(chrome["ws_url"])
            return browser
        except BaseException:
            browser.close()
            raise

    def page(self) -> CDPConnection:
        """Return the current page connection, connecting lazily if needed."""
        return self.connection

    @property
    def connection(self) -> CDPConnection:
        if self._conn is None:
            return self.connect()
        return self._conn

    @property
    def events(self):
        return self.connection.events

    @property
    def dropped_event_count(self) -> int:
        return self.connection.dropped_event_count

    def call(
        self,
        method: str,
        params: Optional[Dict] = None,
        timeout: Optional[float] = None,
        session_id: Optional[str] = None,
    ) -> Dict:
        return self.connection.call(method, params=params, timeout=timeout, session_id=session_id)

    def wait_for_event(
        self,
        event_name: Any,
        predicate: Optional[Callable[[Dict], bool]] = None,
        timeout: Optional[float] = None,
        *,
        session_id: Any = None,
        after_sequence: Optional[int] = None,
    ) -> Dict:
        return self.connection.wait_for_event(
            event_name,
            predicate=predicate,
            timeout=timeout,
            session_id=session_id,
            after_sequence=after_sequence,
        )

    def event_cursor(self) -> int:
        return self.connection.event_cursor()

    def recent_events(self) -> tuple:
        return self.connection.recent_events()

    def attach_session(self, target_id: str) -> CDPSession:
        return self.connection.attach_session(target_id)

    def attach_to_target(self, target_id: str) -> str:
        return self.connection.attach_to_target(target_id)

    def navigate(
        self,
        url: str,
        wait: bool = True,
        timeout: Optional[float] = None,
        *,
        wait_until: str = "load",
    ) -> Dict:
        return self.connection.navigate(url, wait=wait, timeout=timeout, wait_until=wait_until)

    def wait_for_ready_state(
        self,
        states: tuple = ("interactive", "complete"),
        timeout: Optional[float] = None,
    ) -> str:
        return self.connection.wait_for_ready_state(states=states, timeout=timeout)

    def evaluate(
        self,
        expression: str,
        return_by_value: bool = True,
        timeout: Optional[float] = None,
    ) -> Any:
        return self.connection.evaluate(expression, return_by_value=return_by_value, timeout=timeout)

    def wait_for_selector(self, selector: str, timeout: Optional[float] = None) -> bool:
        return self.connection.wait_for_selector(selector, timeout=timeout)

    def click(self, selector: str) -> None:
        return self.connection.click(selector)

    def input_text(
        self,
        selector: str,
        text: str,
        clear: bool = True,
        press_enter: bool = False,
    ) -> None:
        return self.connection.input_text(selector, text, clear=clear, press_enter=press_enter)

    def press(self, key: str) -> None:
        return self.connection.press(key)

    def extract_text(self, selector: Optional[str] = None) -> str:
        return self.connection.extract_text(selector=selector)

    def extract_html(self, selector: Optional[str] = None) -> str:
        return self.connection.extract_html(selector=selector)

    def screenshot(self, path: Optional[str] = None, format: str = "png") -> bytes:
        return self.connection.screenshot(path=path, format=format)

    def list_targets(self) -> List[Dict]:
        return list_targets_from_port(self._host, self._port, timeout=self._timeout)

    def select_target(
        self,
        target_id: Optional[str] = None,
        url_contains: Optional[str] = None,
        title_contains: Optional[str] = None,
        target_type: str = "page",
    ) -> CDPConnection:
        for target in self.list_targets():
            if target_type and target.get("type") != target_type:
                continue
            if target_id and target.get("id") != target_id:
                continue
            if url_contains and url_contains not in target.get("url", ""):
                continue
            if title_contains and title_contains not in target.get("title", ""):
                continue
            ws_url = target.get("webSocketDebuggerUrl")
            if ws_url:
                return self.connect(ws_url)
        raise ValueError("No matching Chrome target found")

    def new_tab(self, url: str = "about:blank", connect: bool = True):
        target = new_tab_from_port(url, self._host, self._port, timeout=self._timeout)
        if connect and target.get("webSocketDebuggerUrl"):
            return self.connect(target["webSocketDebuggerUrl"])
        return target

    def close(self):
        for connection in list(self._connections):
            connection.close()
        self._connections.clear()
        self._conn = None
        launch, self._launch = self._launch, None
        if launch is not None:
            launch.terminate()


Browser = ChromeCDPAdapter


# ---------------------------------------------------------------------------
# Module-level endpoint helpers
# ---------------------------------------------------------------------------

def discover_ws_url(
    host: str = "127.0.0.1",
    port: int = 9222,
    timeout: float = 5.0,
) -> str:
    """Discover a Chrome WebSocket debugger URL."""
    timeout = _validate_timeout(timeout)
    port = _validate_port(port, "port", allow_zero=False)
    try:
        targets = list_targets_from_port(host, port, timeout=timeout)
        for t in targets:
            if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                return t["webSocketDebuggerUrl"]
        for t in targets:
            ws = t.get("webSocketDebuggerUrl")
            if ws:
                return ws
    except Exception:
        pass

    url = f"http://{host}:{port}/json/version"
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        data = json.loads(resp.read())
    ws = data.get("webSocketDebuggerUrl", "")
    if not ws:
        raise CDPConnectionError("No WebSocket debugger URL found")
    return ws


def list_targets_from_port(
    host: str = "127.0.0.1",
    port: int = 9222,
    timeout: float = 5.0,
) -> List[Dict]:
    """Fetch /json/list and return the list of CDP target dicts."""
    timeout = _validate_timeout(timeout)
    port = _validate_port(port, "port", allow_zero=False)
    url = f"http://{host}:{port}/json/list"
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read())


def new_tab_from_port(
    url: str = "about:blank",
    host: str = "127.0.0.1",
    port: int = 9222,
    timeout: float = 5.0,
) -> Dict:
    """Open a new tab in Chrome via /json/new."""
    timeout = _validate_timeout(timeout)
    port = _validate_port(port, "port", allow_zero=False)
    target_url = f"http://{host}:{port}/json/new?{urllib.parse.quote(url)}"
    req = urllib.request.Request(target_url, method="PUT")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _browser_ws_url_from_version(
    host: str = "127.0.0.1",
    port: int = 9222,
    timeout: float = 5.0,
) -> str:
    timeout = _validate_timeout(timeout)
    port = _validate_port(port, "port", allow_zero=False)
    url = f"http://{host}:{port}/json/version"
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        data = json.loads(resp.read())
    ws_url = data.get("webSocketDebuggerUrl")
    if not ws_url:
        raise CDPConnectionError("/json/version did not include webSocketDebuggerUrl")
    return ws_url


def wait_until_ready(
    host: str = "127.0.0.1",
    port: int = 9222,
    timeout: float = 10.0,
) -> None:
    """Poll /json/version until Chrome is ready to accept connections."""
    timeout = _validate_timeout(timeout)
    port = _validate_port(port, "port", allow_zero=False)
    deadline = time.monotonic() + timeout
    last_exc: Optional[Exception] = None
    while time.monotonic() < deadline:
        try:
            _browser_ws_url_from_version(host, port, timeout=min(1.0, max(0.1, deadline - time.monotonic())))
            return
        except Exception as exc:
            last_exc = exc
            time.sleep(0.1)
    raise CDPTimeoutError(
        f"Chrome not ready at {host}:{port} after {timeout}s"
    ) from last_exc


def _read_file_tail(path: Optional[str]) -> str:
    if not path:
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
        return "\n".join(lines[-20:])
    except OSError:
        return ""


def _wait_for_devtools_active_port(
    process: subprocess.Popen,
    user_data_dir: str,
    deadline: float,
    previous_marker_text: Optional[str] = None,
) -> tuple:
    marker = os.path.join(user_data_dir, "DevToolsActivePort")
    last_error: Optional[Exception] = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise CDPConnectionError(
                f"Chrome exited during startup with code {process.returncode}"
            )
        try:
            with open(marker, "r", encoding="utf-8") as handle:
                text = handle.read()
            if previous_marker_text is not None and text == previous_marker_text:
                raise ValueError("DevToolsActivePort has not changed for this launch")
            lines = text.splitlines()
            if len(lines) < 2:
                raise ValueError("DevToolsActivePort is incomplete")
            port = int(lines[0])
            browser_path = lines[1].strip()
            if not 1 <= port <= 65535:
                raise ValueError(f"Invalid DevTools port: {port}")
            if not browser_path.startswith("/devtools/browser/"):
                raise ValueError(f"Invalid browser WebSocket path: {browser_path!r}")
            return port, f"ws://127.0.0.1:{port}{browser_path}"
        except (OSError, ValueError) as exc:
            last_error = exc
            time.sleep(0.05)
    raise CDPTimeoutError("Chrome did not create a valid DevToolsActivePort file") from last_error


def _verify_browser_endpoint(port: int, browser_ws_url: str, timeout: float) -> None:
    version_ws = _browser_ws_url_from_version("127.0.0.1", port, timeout=timeout)
    expected_path = urllib.parse.urlparse(browser_ws_url).path
    observed_path = urllib.parse.urlparse(version_ws).path
    if observed_path != expected_path:
        raise CDPConnectionError(
            f"/json/version WebSocket path mismatch: expected {expected_path!r}, got {observed_path!r}"
        )


def terminate_chrome(proc: Any, timeout: float = 5.0) -> None:
    """Terminate Chrome and remove ZeroCDP-owned temp profile/log artifacts."""
    if proc is None:
        return
    if isinstance(proc, LaunchedChrome):
        process = proc.process
        temp_dir = proc.user_data_dir if proc.owns_user_data_dir else None
        stderr_path = proc.stderr_path
    else:
        process = proc
        temp_dir = getattr(proc, "_zero_cdp_temp_dir", None)
        stderr_path = getattr(proc, "_zero_cdp_stderr_path", None)
    try:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=timeout)
            except Exception:
                try:
                    process.kill()
                    process.wait(timeout=2.0)
                except Exception:
                    pass
    except Exception:
        pass
    if temp_dir:
        _rmtree_with_retries(temp_dir)
    if stderr_path:
        try:
            os.unlink(stderr_path)
        except OSError:
            pass


def _rmtree_with_retries(path: str) -> None:
    """Remove a directory tree, retrying brief teardown races from Chrome child processes."""
    attempts = 5
    for attempt in range(attempts):
        shutil.rmtree(path, ignore_errors=True)
        if not os.path.exists(path):
            return
        if attempt < attempts - 1:
            time.sleep(0.05)


def _env_first(*names: str) -> Optional[str]:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
        lower = name.lower()
        for key, candidate in os.environ.items():
            if key.lower() == lower and candidate:
                return candidate
    return None


def _path_join(root: str, *parts: str) -> str:
    if "\\" in root or (len(root) >= 2 and root[1] == ":"):
        return root.rstrip("\\/") + "\\" + "\\".join(parts)
    return os.path.join(root, *parts)


def _chrome_executable_candidates() -> List[str]:
    """Return likely Chrome/Chromium executables for PATH, macOS, Linux, and Windows."""
    names = [
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
        "chrome",
        "chrome.exe",
        "msedge",
        "msedge.exe",
    ]
    candidates: List[str] = []

    def add(path: Optional[str]) -> None:
        if path and path not in candidates:
            candidates.append(path)

    for name in names:
        add(shutil.which(name))

    windows_roots = [
        _env_first("ProgramFiles"),
        _env_first("ProgramW6432", "PROGRAMW6432"),
        _env_first("ProgramFiles(x86)", "PROGRAMFILES(X86)"),
        _env_first("LOCALAPPDATA"),
    ]
    for root in windows_roots:
        if not root:
            continue
        add(_path_join(root, "Google", "Chrome", "Application", "chrome.exe"))
        add(_path_join(root, "Google", "Chrome Beta", "Application", "chrome.exe"))
        add(_path_join(root, "Google", "Chrome Dev", "Application", "chrome.exe"))
        add(_path_join(root, "Google", "Chrome SxS", "Application", "chrome.exe"))
        add(_path_join(root, "Chromium", "Application", "chrome.exe"))
        add(_path_join(root, "Microsoft", "Edge", "Application", "msedge.exe"))

    homes: List[str] = []
    for candidate in [os.path.expanduser("~"), os.environ.get("HOME", "")]:
        if candidate and candidate not in homes:
            homes.append(candidate)
    try:
        import pwd
        passwd_home = pwd.getpwuid(os.getuid()).pw_dir
        if passwd_home and passwd_home not in homes:
            homes.append(passwd_home)
    except Exception:
        pass
    for home in homes:
        add(os.path.join(home, "Applications/Google Chrome.app/Contents/MacOS/Google Chrome"))
        add(os.path.join(home, "Applications/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"))
        add(os.path.join(home, "Applications/Chromium.app/Contents/MacOS/Chromium"))

    add("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    add("/Applications/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing")
    add("/Applications/Chromium.app/Contents/MacOS/Chromium")
    for name in names:
        add(name)
    return candidates


def launch_chrome(
    executable: Optional[str] = None,
    port: int = 0,
    headless: bool = True,
    user_data_dir: Optional[str] = None,
    extra_args: Optional[List[str]] = None,
    ready_timeout: float = 10.0,
) -> LaunchedChrome:
    """Launch Chrome with remote debugging enabled and return endpoint metadata."""
    ready_timeout = _validate_timeout(ready_timeout, "ready_timeout")
    port = _validate_port(port, "port", allow_zero=True)
    args = _launch_extra_args(extra_args)
    if port != 0 and _port_is_in_use("127.0.0.1", port):
        raise CDPConnectionError(f"Chrome debugging port {port} is already in use")
    if executable is None:
        for c in _chrome_executable_candidates():
            try:
                subprocess.run(
                    [c, "--version"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=2,
                )
                executable = c
                break
            except Exception:
                continue
        if executable is None:
            raise FileNotFoundError("Chrome/Chromium executable not found")

    owns_profile = user_data_dir is None
    proc = None
    stderr_path: Optional[str] = None
    if user_data_dir is None:
        user_data_dir = tempfile.mkdtemp(prefix="chrome_cdp_")

    try:
        previous_marker_text: Optional[str] = None
        marker = os.path.join(user_data_dir, "DevToolsActivePort")
        if not owns_profile:
            try:
                with open(marker, "r", encoding="utf-8") as handle:
                    previous_marker_text = handle.read()
            except OSError:
                previous_marker_text = None

        stderr_file = tempfile.NamedTemporaryFile(
            prefix="zero_cdp_chrome_",
            suffix=".log",
            delete=False,
        )
        stderr_path = stderr_file.name

        cmd = [
            executable,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={user_data_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
        ]
        if headless:
            cmd.append("--headless=new")
        if args:
            cmd.extend(args)

        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=stderr_file)
        finally:
            stderr_file.close()

        # Backward-compatible cleanup hints for callers that still pass the raw Popen
        if owns_profile:
            proc._zero_cdp_temp_dir = user_data_dir  # type: ignore[attr-defined]
        proc._zero_cdp_stderr_path = stderr_path  # type: ignore[attr-defined]

        deadline = time.monotonic() + ready_timeout
        if port == 0:
            actual_port, browser_ws_url = _wait_for_devtools_active_port(
                proc,
                user_data_dir,
                deadline,
                previous_marker_text=previous_marker_text,
            )
        else:
            last_exc: Optional[Exception] = None
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    raise CDPConnectionError(
                        f"Chrome exited during startup with code {proc.returncode}"
                    )
                try:
                    browser_ws_url = _browser_ws_url_from_version(
                        "127.0.0.1",
                        port,
                        timeout=min(1.0, max(0.1, deadline - time.monotonic())),
                    )
                    actual_port = port
                    break
                except Exception as exc:
                    last_exc = exc
                    time.sleep(0.1)
            else:
                raise CDPTimeoutError(
                    f"Chrome not ready at 127.0.0.1:{port} after {ready_timeout}s"
                ) from last_exc
        _verify_browser_endpoint(actual_port, browser_ws_url, timeout=max(0.1, deadline - time.monotonic()))
        return LaunchedChrome(
            process=proc,
            port=actual_port,
            browser_ws_url=browser_ws_url,
            user_data_dir=user_data_dir,
            owns_user_data_dir=owns_profile,
            stderr_path=stderr_path,
        )
    except BaseException as exc:
        diag = "" if isinstance(exc, (KeyboardInterrupt, SystemExit)) else _read_file_tail(stderr_path)
        if proc is not None:
            terminate_chrome(
                LaunchedChrome(
                    process=proc,
                    port=port,
                    browser_ws_url="",
                    user_data_dir=user_data_dir,
                    owns_user_data_dir=owns_profile,
                    stderr_path=stderr_path,
                )
            )
        else:
            if owns_profile:
                _rmtree_with_retries(user_data_dir)
            if stderr_path:
                with contextlib.suppress(OSError):
                    os.unlink(stderr_path)
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        if diag:
            message = f"{exc}\nChrome stderr tail:\n{diag}"
            if isinstance(exc, CDPTimeoutError):
                raise CDPTimeoutError(message) from exc
            if isinstance(exc, CDPConnectionError):
                raise CDPConnectionError(message) from exc
        raise


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="zero_cdp",
        description="ZeroCDP — stdlib-only Chrome DevTools Protocol automation.",
    )
    p.add_argument("--config", help="Path to a ZeroCDP JSON config file")
    p.add_argument("--write-default-config", metavar="FILE", help="Write a default config file and exit")
    p.add_argument("--host", help="Debugging host (default: config or 127.0.0.1)")
    p.add_argument("--port", type=int, help="Debugging port (default: 9222 in connect mode, ephemeral in launch mode)")
    p.add_argument("--ws-url", dest="ws_url", metavar="URL", help="Direct WebSocket URL (skips discovery)")
    p.add_argument("--launch", action="store_true", help="Launch Chrome before connecting")
    p.add_argument("--new-tab", dest="new_tab", metavar="URL", help="Open a new tab at URL")
    p.add_argument("--navigate", metavar="URL", help="Navigate to URL")
    p.add_argument("--extract-text", dest="extract_text", action="store_true", help="Extract page text")
    p.add_argument("--extract-html", dest="extract_html", action="store_true", help="Extract page HTML")
    p.add_argument("--selector", metavar="CSS", help="CSS selector for extract/click/input operations")
    p.add_argument("--click", metavar="CSS", help="Click a CSS selector")
    p.add_argument("--input-text", dest="input_text", metavar="TEXT", help="Type TEXT into --selector")
    p.add_argument("--no-clear", dest="clear", action="store_false", help="Do not clear the field before --input-text")
    p.add_argument("--press-enter", action="store_true", help="Press Enter after --input-text")
    p.add_argument("--press", metavar="KEY", help="Press a key such as Enter, Tab, Escape")
    p.add_argument("--wait-for-selector", metavar="CSS", help="Wait for a CSS selector")
    p.add_argument("--screenshot", metavar="FILE", help="Save PNG screenshot to FILE")
    p.add_argument("--eval", metavar="JS", help="Evaluate a JavaScript expression and print the result")
    return p


def _main(argv: Optional[List[str]] = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    if args.write_default_config:
        print(write_default_config(args.write_default_config))
        return 0

    cfg = load_config(args.config)
    chrome = cfg["chrome"]
    launch_mode = bool(args.launch or chrome.get("mode") == "launch")
    follow_on_action = any([
        args.navigate,
        args.wait_for_selector,
        args.click,
        args.input_text is not None,
        args.press,
        args.eval,
        args.extract_text,
        args.extract_html,
        args.screenshot,
    ])
    if launch_mode and args.new_tab and not follow_on_action:
        parser.error("--launch --new-tab requires a follow-on action such as --eval, --navigate, --extract-text, or --screenshot")
    if launch_mode and (args.ws_url or chrome.get("ws_url")):
        parser.error("--ws-url / chrome.ws_url cannot be used with --launch or launch mode")
    host = args.host or chrome.get("host") or "127.0.0.1"
    configured_port = args.port if args.port is not None else chrome.get("port")
    port = _mode_default_port(configured_port, launch_mode)
    timeout = _validate_timeout(cfg["timeouts"].get("default", 10.0))
    launch = None
    conn = None
    try:
        if launch_mode:
            launch = launch_chrome(
                executable=chrome.get("executable"),
                port=port,
                headless=chrome.get("headless", True),
                user_data_dir=chrome.get("user_data_dir"),
                extra_args=chrome.get("extra_args") or [],
            )
            host = "127.0.0.1"
            port = launch.port

        ws_url = args.ws_url or chrome.get("ws_url")
        if args.new_tab:
            result = new_tab_from_port(args.new_tab, host=host, port=port, timeout=timeout)
            if not follow_on_action:
                print(json.dumps(result, indent=2))
                return 0
            ws_url = result.get("webSocketDebuggerUrl")
            if not ws_url:
                raise CDPConnectionError("New target did not provide webSocketDebuggerUrl")

        if ws_url is None:
            ws_url = discover_ws_url(host=host, port=port, timeout=timeout)
        conn = CDPConnection(ws_url, timeout=timeout)
        if args.navigate:
            print(json.dumps(conn.navigate(args.navigate), indent=2))
        if args.wait_for_selector:
            conn.wait_for_selector(args.wait_for_selector)
            print(f"Selector ready: {args.wait_for_selector}")
        if args.click:
            conn.click(args.click)
            print(f"Clicked: {args.click}")
        if args.input_text is not None:
            if not args.selector:
                parser.error("--input-text requires --selector")
            conn.input_text(args.selector, args.input_text, clear=args.clear, press_enter=args.press_enter)
            print(f"Input text into: {args.selector}")
        if args.press:
            conn.press(args.press)
            print(f"Pressed: {args.press}")
        if args.eval:
            print(conn.evaluate(args.eval))
        if args.extract_text:
            print(conn.extract_text(args.selector))
        if args.extract_html:
            print(conn.extract_html(args.selector))
        if args.screenshot:
            conn.screenshot(args.screenshot)
            print(f"Screenshot saved: {args.screenshot}")
    finally:
        if conn is not None:
            conn.close()
        if launch is not None:
            terminate_chrome(launch)

    return 0


def main(argv: Optional[List[str]] = None) -> int:
    """Public console-script entry point."""
    return _main(argv)


if __name__ == "__main__":
    sys.exit(main())
