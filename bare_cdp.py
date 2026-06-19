#!/usr/bin/env python3
"""
BareCDP: bare-metal Chrome DevTools Protocol automation for Python.

BareCDP is a stdlib-only browser control layer for scripts and orchestrators
that need to drive Chrome/Chromium without Playwright, Selenium, WebDriver,
or runtime dependencies. It talks directly to Chrome's DevTools Protocol over
an RFC-6455 WebSocket implemented with Python's standard library.

Quick start with a running Chrome debug port:

    chrome --remote-debugging-port=9222 --user-data-dir=/tmp/bare-cdp-profile

    from bare_cdp import Browser

    browser = Browser(port=9222)
    page = browser.connect()
    page.navigate("https://example.com")
    print(page.extract_text())
    browser.close()

Launch Chrome from Python:

    from bare_cdp import Browser, launch_chrome, terminate_chrome

    proc = launch_chrome(port=9222, headless=True)
    browser = Browser(port=9222)
    try:
        page = browser.connect()
        page.navigate("https://example.com")
    finally:
        browser.close()
        terminate_chrome(proc)

Use a JSON config file:

    from bare_cdp import Browser

    browser = Browser.from_config("bare-cdp.json")
    page = browser.page()
    page.navigate("https://example.com")

CLI examples:

    python -m bare_cdp --navigate https://example.com --extract-text
    python -m bare_cdp --eval "document.title"
    python -m bare_cdp --screenshot page.png
"""

import argparse
import base64
import collections
import copy
import hashlib
import json
import os
import shutil
import socket
import ssl
import struct
import sys
import time
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional

__version__ = "0.1.2"


_WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_WS_OPCODE_CONTINUATION = 0x0
_WS_OPCODE_TEXT = 0x1
_WS_OPCODE_BINARY = 0x2
_WS_OPCODE_CLOSE = 0x8
_WS_OPCODE_PING = 0x9
_WS_OPCODE_PONG = 0xA
_WS_MAX_PAYLOAD = 64 * 1024 * 1024  # 64 MiB


# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------

class CDPError(Exception):
    """Base exception for all BareCDP errors."""


class CDPConnectionError(CDPError, ConnectionError):
    """WebSocket or transport-level failure."""


class CDPProtocolError(CDPError):
    """Unexpected or malformed CDP protocol data."""


class CDPTimeoutError(CDPError, TimeoutError):
    """A CDP call or readiness wait exceeded its deadline."""


class CDPCommandError(CDPError, RuntimeError):
    """Chrome returned an error or errorText for a CDP command."""


class SelectorError(CDPError, LookupError):
    """A CSS selector matched no element."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_CONFIG: Dict[str, Any] = {
    "chrome": {
        "mode": "connect",            # "connect" or "launch"
        "host": "127.0.0.1",
        "port": 9222,
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
    merged = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _bool_from_env(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def load_config(path: Optional[str] = None) -> Dict[str, Any]:
    """Load BareCDP JSON configuration and apply environment overrides.

    Environment variables:
        BARE_CDP_HOST
        BARE_CDP_PORT
        BARE_CDP_WS_URL
        BARE_CDP_CHROME
        BARE_CDP_USER_DATA_DIR
        BARE_CDP_HEADLESS
        BARE_CDP_TIMEOUT
    """
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if path:
        with open(path, "r", encoding="utf-8") as f:
            cfg = _deep_merge(cfg, json.load(f))

    env_map = {
        "BARE_CDP_HOST": ("chrome", "host", str),
        "BARE_CDP_PORT": ("chrome", "port", int),
        "BARE_CDP_WS_URL": ("chrome", "ws_url", str),
        "BARE_CDP_CHROME": ("chrome", "executable", str),
        "BARE_CDP_USER_DATA_DIR": ("chrome", "user_data_dir", str),
        "BARE_CDP_HEADLESS": ("chrome", "headless", _bool_from_env),
        "BARE_CDP_TIMEOUT": ("timeouts", "default", float),
    }
    for env_name, (section, key, caster) in env_map.items():
        if env_name in os.environ and os.environ[env_name] != "":
            cfg[section][key] = caster(os.environ[env_name])
    return cfg


def write_default_config(path: str = "bare-cdp.json") -> str:
    """Write a default JSON config file and return its path."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(DEFAULT_CONFIG, f, indent=2)
        f.write("\n")
    return path


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
    buf = b""
    while len(buf) < n:
        chunk = recv_fn(n - len(buf))
        if not chunk:
            raise CDPConnectionError("WebSocket connection closed unexpectedly")
        buf += chunk
    return buf


def _ws_decode_frame(recv_fn) -> tuple:
    """Read one WebSocket frame; return (fin, opcode, payload)."""
    header = _recv_exactly(recv_fn, 2)
    fin = bool(header[0] & 0x80)
    opcode = header[0] & 0x0F
    masked = bool(header[1] & 0x80)
    payload_len = header[1] & 0x7F
    if payload_len == 126:
        payload_len = struct.unpack(">H", _recv_exactly(recv_fn, 2))[0]
    elif payload_len == 127:
        payload_len = struct.unpack(">Q", _recv_exactly(recv_fn, 8))[0]
    if payload_len > _WS_MAX_PAYLOAD:
        raise CDPProtocolError(
            f"WebSocket frame too large: {payload_len} bytes (max {_WS_MAX_PAYLOAD})"
        )
    mask_key = _recv_exactly(recv_fn, 4) if masked else b""
    raw = _recv_exactly(recv_fn, payload_len)
    payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(raw)) if masked else raw
    return fin, opcode, payload


class _WSReceiver:
    """Buffered WebSocket reader; handles fragmentation, ping/pong, and close."""

    def __init__(self, sock: socket.socket, initial_buf: bytes = b""):
        self._sock = sock
        self._buf = initial_buf

    def _recv(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise CDPConnectionError("WebSocket: unexpected EOF")
            self._buf += chunk
        data, self._buf = self._buf[:n], self._buf[n:]
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
                fragments.append(payload)
            else:
                base_opcode = opcode
                fragments = [payload]
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


# ---------------------------------------------------------------------------
# CDPConnection
# ---------------------------------------------------------------------------

class CDPConnection:
    """Low-level Chrome DevTools Protocol connection over raw WebSocket."""

    def __init__(self, ws_url: str, timeout: float = 10.0):
        self._ws_url = ws_url
        self._timeout = timeout
        self._id = 0
        self._sock: Optional[socket.socket] = None
        self._ws: Optional[_WSReceiver] = None
        self.events: collections.deque = collections.deque(maxlen=2000)
        self._page_enabled = False
        self._connect()

    def __enter__(self) -> "CDPConnection":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        self.close()
        return False

    def _connect(self):
        parsed = urllib.parse.urlparse(self._ws_url)
        host = parsed.hostname or "127.0.0.1"
        use_ssl = parsed.scheme == "wss"
        port = parsed.port or (443 if use_ssl else 80)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query

        raw = socket.create_connection((host, port), timeout=self._timeout)
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

            resp = b""
            while b"\r\n\r\n" not in resp:
                chunk = raw.recv(4096)
                if not chunk:
                    raise CDPConnectionError("WebSocket handshake: server closed connection")
                resp += chunk

            parts = resp.split(b"\r\n\r\n", 1)
            header_text = parts[0].decode(errors="replace")
            leftover = parts[1] if len(parts) > 1 else b""

            lines = header_text.split("\r\n")
            if not lines[0].startswith("HTTP/1.1 101"):
                raise CDPConnectionError(f"WebSocket upgrade failed: {lines[0]}")

            resp_headers: Dict[str, str] = {}
            for line in lines[1:]:
                if ": " in line:
                    k, v = line.split(": ", 1)
                    resp_headers[k.lower()] = v

            expected = _ws_accept_key(key)
            got = resp_headers.get("sec-websocket-accept", "")
            if got != expected:
                raise CDPConnectionError(
                    f"Sec-WebSocket-Accept mismatch: expected {expected!r}, got {got!r}"
                )

            self._ws = _WSReceiver(raw, leftover)
        except Exception:
            self._sock = None
            try:
                raw.close()
            finally:
                raise

    def call(
        self,
        method: str,
        params: Optional[Dict] = None,
        timeout: Optional[float] = None,
        session_id: Optional[str] = None,
    ) -> Dict:
        """Send a CDP command and return the matching response result.

        Event frames and out-of-order responses are retained in ``self.events``
        and ignored until the response with this call's id arrives.
        ``session_id`` supports flattened Target.attachToTarget sessions.
        """
        if not method or not isinstance(method, str):
            raise ValueError("method must be a non-empty string")
        if params is not None and not isinstance(params, dict):
            raise TypeError("params must be a dict when provided")
        if self._ws is None or self._sock is None:
            raise CDPConnectionError("CDPConnection is closed")

        self._id += 1
        call_id = self._id
        msg: Dict = {"id": call_id, "method": method}
        if params is not None:
            msg["params"] = params
        if session_id:
            msg["sessionId"] = session_id
        self._ws.send_text(json.dumps(msg, separators=(",", ":")))

        deadline = time.monotonic() + (timeout if timeout is not None else self._timeout)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CDPTimeoutError(f"CDP call '{method}' timed out")
            self._sock.settimeout(remaining)
            try:
                _, payload = self._ws.read_message()
            except socket.timeout:
                self.close()
                raise CDPTimeoutError(f"CDP call '{method}' timed out")
            except (CDPProtocolError, CDPConnectionError):
                self.close()
                raise
            data = json.loads(payload.decode())
            if data.get("id") == call_id:
                if "error" in data:
                    raise CDPCommandError(f"CDP error for '{method}': {data['error']}")
                return data.get("result", {})
            self.events.append(data)

    def close(self):
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

    def _enable_page_domain(self):
        """Enable the Page domain idempotently."""
        if not self._page_enabled:
            self.call("Page.enable")
            self._page_enabled = True

    def wait_for_event(
        self,
        event_name: Any,
        predicate: Optional[Callable[[Dict], bool]] = None,
        timeout: Optional[float] = None,
    ) -> Dict:
        """Wait for one or more CDP events; drains buffered events first, then pumps socket."""
        deadline = time.monotonic() + (timeout if timeout is not None else self._timeout)
        event_names = {event_name} if isinstance(event_name, str) else set(event_name)

        # Drain already-buffered events, consuming the first match
        remaining_events: collections.deque = collections.deque(maxlen=2000)
        found: Optional[Dict] = None
        for ev in self.events:
            if found is None and ev.get("method") in event_names:
                params = ev.get("params", {})
                if predicate is None or predicate(params):
                    found = params
                    continue
            remaining_events.append(ev)
        self.events = remaining_events
        if found is not None:
            return found

        if self._ws is None or self._sock is None:
            raise CDPConnectionError("CDPConnection is closed")

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CDPTimeoutError(f"Event {event_name!r} not received within timeout")
            self._sock.settimeout(remaining)
            try:
                _, payload = self._ws.read_message()
            except socket.timeout:
                self.close()
                raise CDPTimeoutError(f"Event {event_name!r} timed out")
            except (CDPProtocolError, CDPConnectionError):
                self.close()
                raise
            data = json.loads(payload.decode())
            if data.get("method") in event_names:
                params = data.get("params", {})
                if predicate is None or predicate(params):
                    return params
            self.events.append(data)

    def evaluate(self, expression: str, return_by_value: bool = True, timeout: Optional[float] = None) -> Any:
        """Evaluate JavaScript in the current page and return the CDP value."""
        result = self.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": return_by_value, "awaitPromise": True},
            timeout=timeout,
        )
        remote = result.get("result", {})
        if result.get("exceptionDetails"):
            raise CDPCommandError(f"Runtime.evaluate exception: {result['exceptionDetails']}")
        return remote.get("value") if return_by_value else remote

    def navigate(self, url: str, wait: bool = True, timeout: Optional[float] = None) -> Dict:
        """Navigate the current page; optionally wait for the matching navigation event."""
        self._enable_page_domain()
        nav = self.call("Page.navigate", {"url": url}, timeout=timeout)
        if nav.get("errorText"):
            raise CDPCommandError(f"Page.navigate error: {nav['errorText']}")
        if wait:
            frame_id = nav.get("frameId")

            def _frame_matches(params: Dict) -> bool:
                return frame_id is None or params.get("frameId") == frame_id

            self.wait_for_event(
                ("Page.frameStoppedLoading", "Page.navigatedWithinDocument"),
                predicate=_frame_matches,
                timeout=timeout,
            )
        return nav

    def wait_for_ready_state(self, states: tuple = ("interactive", "complete"), timeout: Optional[float] = None) -> str:
        deadline = time.monotonic() + (timeout if timeout is not None else self._timeout)
        last = ""
        while time.monotonic() < deadline:
            try:
                last = self.evaluate("document.readyState", timeout=max(0.1, deadline - time.monotonic())) or ""
                if last in states:
                    return last
            except Exception:
                pass
            time.sleep(0.05)
        raise CDPTimeoutError(f"document.readyState did not reach {states}; last={last!r}")

    def wait_for_selector(self, selector: str, timeout: Optional[float] = None) -> bool:
        deadline = time.monotonic() + (timeout if timeout is not None else self._timeout)
        while time.monotonic() < deadline:
            remaining = max(0.1, deadline - time.monotonic())
            try:
                value = self.evaluate(
                    f"!!document.querySelector({json.dumps(selector)})",
                    timeout=remaining,
                )
                if value:
                    return True
            except Exception:
                pass
            time.sleep(0.05)
        raise CDPTimeoutError(f"Selector {selector!r} not found within timeout")

    def click(self, selector: str):
        """Click a CSS selector using real CDP mouse events when possible."""
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
        self.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1})
        self.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1})

    def input_text(self, selector: str, text: str, clear: bool = True, press_enter: bool = False):
        """Focus element, optionally clear, insert text, optionally press Enter."""
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

    def attach_to_target(self, target_id: str) -> str:
        """Attach to a target with flattened sessions and return sessionId."""
        result = self.call("Target.attachToTarget", {"targetId": target_id, "flatten": True})
        session_id = result.get("sessionId")
        if not session_id:
            raise CDPCommandError("Target.attachToTarget did not return a sessionId")
        return session_id


# ---------------------------------------------------------------------------
# Orchestrator-friendly high-level wrapper
# ---------------------------------------------------------------------------

class ChromeCDPAdapter:
    """High-level orchestrator wrapper for CDP connections."""

    def __init__(self, host: str = "127.0.0.1", port: int = 9222, timeout: float = 10.0):
        self._host = host
        self._port = port
        self._timeout = timeout
        self._conn: Optional[CDPConnection] = None
        self._process: Any = None

    def __enter__(self) -> "ChromeCDPAdapter":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        self.close()
        return False

    def connect(self, ws_url: Optional[str] = None) -> CDPConnection:
        if ws_url is None:
            ws_url = discover_ws_url(self._host, self._port, timeout=self._timeout)
        self._conn = CDPConnection(ws_url, timeout=self._timeout)
        return self._conn

    @classmethod
    def from_config(cls, path: Optional[str] = None) -> "ChromeCDPAdapter":
        cfg = load_config(path)
        chrome = cfg["chrome"]
        timeout = float(cfg["timeouts"].get("default", 10.0))
        browser = cls(host=chrome.get("host", "127.0.0.1"), port=int(chrome.get("port", 9222)), timeout=timeout)
        if chrome.get("mode") == "launch":
            browser._process = launch_chrome(
                executable=chrome.get("executable"),
                port=int(chrome.get("port", 9222)),
                headless=bool(chrome.get("headless", True)),
                user_data_dir=chrome.get("user_data_dir"),
                extra_args=chrome.get("extra_args") or [],
            )
        if chrome.get("ws_url"):
            browser.connect(chrome["ws_url"])
        return browser

    def page(self) -> CDPConnection:
        """Return the current page connection, connecting lazily if needed."""
        return self.connection

    @property
    def connection(self) -> CDPConnection:
        if self._conn is None:
            return self.connect()
        return self._conn

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
        conn, self._conn = self._conn, None
        proc, self._process = self._process, None
        if conn:
            conn.close()
        if proc:
            terminate_chrome(proc)

    def __getattr__(self, name: str):
        """Delegate high-level actions to the active CDPConnection."""
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self.connection, name)


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
    target_url = f"http://{host}:{port}/json/new?{urllib.parse.quote(url)}"
    req = urllib.request.Request(target_url, method="PUT")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def wait_until_ready(
    host: str = "127.0.0.1",
    port: int = 9222,
    timeout: float = 10.0,
) -> None:
    """Poll /json/version until Chrome is ready to accept connections."""
    url = f"http://{host}:{port}/json/version"
    deadline = time.monotonic() + timeout
    last_exc: Optional[Exception] = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as resp:
                json.loads(resp.read())
                return
        except Exception as exc:
            last_exc = exc
            time.sleep(0.1)
    raise CDPTimeoutError(
        f"Chrome not ready at {host}:{port} after {timeout}s"
    ) from last_exc


def terminate_chrome(proc: Any, timeout: float = 5.0) -> None:
    """Terminate a Chrome process and remove its temp profile if one was created."""
    if proc is None:
        return
    temp_dir: Optional[str] = getattr(proc, "_bare_cdp_temp_dir", None)
    try:
        proc.terminate()
        try:
            proc.wait(timeout=timeout)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=2.0)
            except Exception:
                pass
    except Exception:
        pass
    if temp_dir:
        shutil.rmtree(temp_dir, ignore_errors=True)


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
    port: int = 9222,
    headless: bool = True,
    user_data_dir: Optional[str] = None,
    extra_args: Optional[List[str]] = None,
    ready_timeout: float = 10.0,
) -> Any:
    """Launch Chrome with remote debugging enabled. Returns subprocess.Popen."""
    import subprocess
    import tempfile

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

    temp_profile: Optional[str] = None
    if user_data_dir is None:
        temp_profile = tempfile.mkdtemp(prefix="chrome_cdp_")
        user_data_dir = temp_profile

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
    if extra_args:
        cmd.extend(extra_args)

    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    if temp_profile is not None:
        proc._bare_cdp_temp_dir = temp_profile  # type: ignore[attr-defined]

    if ready_timeout:
        deadline = time.monotonic() + ready_timeout
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                terminate_chrome(proc)
                raise CDPConnectionError(
                    f"Chrome process exited early (returncode={proc.returncode})"
                )
            try:
                wait_until_ready(
                    host="127.0.0.1",
                    port=port,
                    timeout=min(0.5, max(0.0, deadline - time.monotonic())),
                )
                return proc
            except CDPTimeoutError:
                pass
        if proc.poll() is not None:
            terminate_chrome(proc)
            raise CDPConnectionError(
                f"Chrome process exited early (returncode={proc.returncode})"
            )
        terminate_chrome(proc)
        raise CDPTimeoutError(
            f"Chrome did not become ready on port {port} within {ready_timeout}s"
        )

    return proc


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bare_cdp",
        description="BareCDP — stdlib-only Chrome DevTools Protocol automation.",
    )
    p.add_argument("--config", help="Path to a BareCDP JSON config file")
    p.add_argument("--write-default-config", metavar="FILE", help="Write a default config file and exit")
    p.add_argument("--host", help="Debugging host (default: config or 127.0.0.1)")
    p.add_argument("--port", type=int, help="Debugging port (default: config or 9222)")
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
    host = args.host or cfg["chrome"].get("host", "127.0.0.1")
    port = args.port or int(cfg["chrome"].get("port", 9222))
    timeout = float(cfg["timeouts"].get("default", 10.0))
    process = None
    if args.launch or cfg["chrome"].get("mode") == "launch":
        process = launch_chrome(
            executable=cfg["chrome"].get("executable"),
            port=port,
            headless=bool(cfg["chrome"].get("headless", True)),
            user_data_dir=cfg["chrome"].get("user_data_dir"),
            extra_args=cfg["chrome"].get("extra_args") or [],
        )

    if args.new_tab:
        result = new_tab_from_port(args.new_tab, host=host, port=port, timeout=timeout)
        print(json.dumps(result, indent=2))
        if process:
            terminate_chrome(process)
        return 0

    ws_url = args.ws_url or cfg["chrome"].get("ws_url") or discover_ws_url(host=host, port=port, timeout=timeout)
    conn = CDPConnection(ws_url, timeout=timeout)
    try:
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
        conn.close()
        if process:
            terminate_chrome(process)

    return 0


def main(argv: Optional[List[str]] = None) -> int:
    """Public console-script entry point."""
    return _main(argv)


if __name__ == "__main__":
    sys.exit(main())
