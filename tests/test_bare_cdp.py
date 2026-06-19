"""
Tests for bare_cdp.py — strict TDD, stdlib only.

Covers:
- Import audit (only stdlib modules loaded by production script)
- Low-level WebSocket handshake + frame masking
- CDP call: ignores events, returns matching response id
- input_text: focus/clear JS, Input.insertText, optional Enter
- extract_text: selector vs whole-page
- Endpoint discovery: /json/version and /json/list via stdlib HTTP server
- Chrome launch discovery: PATH, macOS/Linux names, and Windows Program Files/LocalAppData
"""

import base64
import hashlib
import inspect
import json
import os
import shutil
import socket
import struct
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable, List, Optional, Tuple
from unittest import mock

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import bare_cdp as adapter


# ---------------------------------------------------------------------------
# Helpers: raw WebSocket frame codec (mirrors what the adapter must implement)
# ---------------------------------------------------------------------------

def _ws_accept_key(client_key: str) -> str:
    MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
    digest = hashlib.sha1((client_key + MAGIC).encode()).digest()
    return base64.b64encode(digest).decode()


def _decode_client_frame(data: bytes) -> Tuple[int, bytes]:
    """Decode a *masked* client → server WebSocket frame; return (opcode, payload)."""
    assert data[0] & 0x80, "FIN bit must be set"
    opcode = data[0] & 0x0F
    assert data[1] & 0x80, "client frames MUST be masked"
    payload_len = data[1] & 0x7F
    offset = 2
    if payload_len == 126:
        payload_len = struct.unpack(">H", data[offset:offset+2])[0]
        offset += 2
    elif payload_len == 127:
        payload_len = struct.unpack(">Q", data[offset:offset+8])[0]
        offset += 8
    mask = data[offset:offset+4]
    offset += 4
    payload = bytes(b ^ mask[i % 4] for i, b in enumerate(data[offset:offset+payload_len]))
    return opcode, payload


def _encode_server_frame(payload: bytes, opcode: int = 0x1) -> bytes:
    """Encode an *unmasked* server → client text frame."""
    header = bytes([0x80 | opcode])
    n = len(payload)
    if n < 126:
        header += bytes([n])
    elif n < 65536:
        header += bytes([126]) + struct.pack(">H", n)
    else:
        header += bytes([127]) + struct.pack(">Q", n)
    return header + payload


# ---------------------------------------------------------------------------
# Fake WS/CDP server
# ---------------------------------------------------------------------------

class FakeCDPServer:
    """
    A minimal TCP server that:
    1. Performs the HTTP→WebSocket upgrade handshake.
    2. Reads JSON-RPC frames from the client.
    3. Optionally fires 'event' frames before responding.
    4. Returns a matching JSON-RPC response.

    Handlers is a list of (method, response_dict | None) pairs.  For each
    incoming call the handler list is consumed in order.  Pass
    inject_events=[...] to fire those event frames *before* the matching
    response so we can verify the adapter filters them correctly.
    """

    def __init__(
        self,
        handlers: Optional[List[Tuple]] = None,
        inject_events: Optional[List[dict]] = None,
        pre_frames: Optional[List[bytes]] = None,
    ):
        self.handlers = list(handlers or [])
        self.inject_events = list(inject_events or [])
        self.pre_frames = list(pre_frames or [])
        self._received: List[dict] = []
        self.received_opcodes: List[int] = []
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self.port: int = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    @property
    def ws_url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/devtools/page/FAKE"

    @property
    def received(self) -> List[dict]:
        return self._received

    def close(self):
        try:
            self._sock.close()
        except Exception:
            pass
        self._thread.join(timeout=1)

    def _serve(self):
        try:
            conn, _ = self._sock.accept()
        except OSError:
            return
        try:
            self._handle(conn)
        except Exception:
            pass
        finally:
            conn.close()

    def _handle(self, conn: socket.socket):
        # 1. Read HTTP upgrade request
        raw = b""
        while b"\r\n\r\n" not in raw:
            chunk = conn.recv(4096)
            if not chunk:
                return
            raw += chunk

        lines = raw.decode(errors="replace").split("\r\n")
        headers = {}
        for line in lines[1:]:
            if ": " in line:
                k, v = line.split(": ", 1)
                headers[k.lower()] = v

        client_key = headers.get("sec-websocket-key", "")
        accept = _ws_accept_key(client_key)
        response = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n"
            "\r\n"
        )
        conn.sendall(response.encode())

        # 2. Process CDP frames
        for handler in self.handlers:
            expected_method, result_body = handler[0], handler[1]
            after_events = handler[2] if len(handler) > 2 else []
            for frame in self.pre_frames:
                conn.sendall(frame)

            frame_data = self._recv_frame(conn)
            opcode, payload = _decode_client_frame(frame_data)
            self.received_opcodes.append(opcode)
            msg = json.loads(payload.decode())
            self._received.append(msg)
            self._drain_control_frames(conn)
            self._assert_method(expected_method, msg)

            # Optionally inject events before the real response
            for ev in self.inject_events:
                ev_frame = _encode_server_frame(json.dumps(ev).encode())
                conn.sendall(ev_frame)

            resp: dict = {"id": msg["id"]}
            if isinstance(result_body, dict) and "__raw_response__" in result_body:
                resp.update(result_body["__raw_response__"])
            elif result_body is not None:
                resp["result"] = result_body
            resp_frame = _encode_server_frame(json.dumps(resp).encode())
            conn.sendall(resp_frame)

            for ev in after_events:
                ev_frame = _encode_server_frame(json.dumps(ev).encode())
                conn.sendall(ev_frame)

    def _assert_method(self, expected_method: str, msg: dict):
        if expected_method != "*":
            assert msg.get("method") == expected_method, (expected_method, msg)

    def _drain_control_frames(self, conn: socket.socket):
        conn.settimeout(0.05)
        try:
            while True:
                frame = self._recv_frame(conn)
                opcode, _ = _decode_client_frame(frame)
                self.received_opcodes.append(opcode)
        except Exception:
            pass
        finally:
            conn.settimeout(None)

    def _recv_frame(self, conn: socket.socket) -> bytes:
        first = conn.recv(2)
        if len(first) < 2:
            raise ConnectionError("server got EOF")
        payload_len = first[1] & 0x7F
        extra = b""
        if payload_len == 126:
            extra = conn.recv(2)
            payload_len = struct.unpack(">H", extra)[0]
        elif payload_len == 127:
            extra = conn.recv(8)
            payload_len = struct.unpack(">Q", extra)[0]
        mask = conn.recv(4)
        payload = b""
        while len(payload) < payload_len:
            chunk = conn.recv(payload_len - len(payload))
            if not chunk:
                raise ConnectionError("server got EOF")
            payload += chunk
        return first + extra + mask + payload


# ---------------------------------------------------------------------------
# Fake HTTP server for endpoint-discovery tests
# ---------------------------------------------------------------------------

class FakeDiscoveryServer:
    """Serves /json/version and /json/list endpoints."""

    def __init__(self, targets: Optional[List[dict]] = None, version_ws_url: Optional[str] = None):
        self._targets = targets or []
        self._version_ws_url = version_ws_url
        self._server = HTTPServer(("127.0.0.1", 0), self._make_handler())
        self._port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def port(self) -> int:
        return self._port

    def close(self):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=1)

    def _make_handler(self):
        targets = self._targets
        version_ws_url = self._version_ws_url
        port = [None]  # late-bind

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/json/version":
                    body = json.dumps({
                        "webSocketDebuggerUrl": version_ws_url or f"ws://127.0.0.1:{self.server.server_address[1]}/devtools/browser/FAKE",
                        "Browser": "Headless Chrome",
                    }).encode()
                elif self.path in ("/json", "/json/list"):
                    body = json.dumps(targets).encode()
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass  # suppress output

        return Handler


# ---------------------------------------------------------------------------
# Import audit
# ---------------------------------------------------------------------------

STDLIB_MODULES = frozenset({
    # built-ins
    "__future__", "abc", "ast", "asyncio", "base64", "binascii", "builtins",
    "cgi", "cgitb", "cmd", "code", "codecs", "codeop", "colorsys", "compileall",
    "concurrent", "concurrent.futures", "configparser", "contextlib", "copy",
    "copyreg", "csv", "dataclasses", "datetime", "decimal", "difflib", "dis",
    "doctest", "email", "encodings", "enum", "errno", "faulthandler", "fcntl",
    "filecmp", "fileinput", "fnmatch", "fractions", "ftplib", "functools",
    "gc", "getopt", "getpass", "gettext", "glob", "gzip", "hashlib",
    "heapq", "hmac", "html", "http", "http.client", "http.server",
    "imaplib", "importlib", "inspect", "io", "ipaddress", "itertools",
    "json", "keyword", "linecache", "locale", "logging", "lzma",
    "mailbox", "math", "mimetypes", "mmap", "modulefinder",
    "multiprocessing", "netrc", "nis", "nntplib", "numbers",
    "operator", "optparse", "os", "os.path", "pathlib", "pdb",
    "pickle", "pickletools", "pkgutil", "platform", "plistlib",
    "poplib", "posix", "posixpath", "pprint", "profile", "pstats",
    "pty", "pwd", "py_compile", "pyclbr", "pydoc", "queue",
    "quopri", "random", "re", "readline", "reprlib", "resource",
    "rlcompleter", "runpy", "sched", "secrets", "select",
    "selectors", "shelve", "shlex", "shutil", "signal", "site",
    "smtpd", "smtplib", "sndhdr", "socket", "socketserver",
    "spwd", "sqlite3", "ssl", "stat", "statistics",
    "string", "stringprep", "struct", "subprocess", "sunau",
    "symtable", "sys", "sysconfig", "syslog", "tabnanny",
    "tarfile", "telnetlib", "tempfile", "termios", "test",
    "textwrap", "threading", "time", "timeit", "tkinter",
    "token", "tokenize", "tomllib", "trace", "traceback",
    "tracemalloc", "tty", "turtle", "turtledemo", "types",
    "typing", "unicodedata", "unittest", "urllib", "urllib.error",
    "urllib.parse", "urllib.request", "uu", "uuid", "venv",
    "warnings", "wave", "weakref", "webbrowser", "wsgiref",
    "xdrlib", "xml", "xmlrpc", "zipapp", "zipfile", "zipimport",
    "zlib", "_thread", "argparse", "array", "atexit", "bisect",
    "cmath", "collections", "collections.abc", "ctypes",
    "curses", "dbm", "ensurepip", "grp", "imp", "lib2to3",
    "msvcrt", "nt", "ntpath", "ossaudiodev", "parser",
    "posixpath", "pprint", "pty",
})


class TestImportAudit(unittest.TestCase):
    """Verify the production script uses only stdlib modules."""

    def test_no_third_party_imports(self):
        import ast
        import pathlib
        src = pathlib.Path(__file__).resolve().parents[1] / "bare_cdp.py"
        self.assertTrue(src.exists(), f"Production script not found: {src}")
        tree = ast.parse(src.read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                else:
                    names = [node.module] if node.module else []
                for name in names:
                    top = name.split(".")[0]
                    self.assertIn(
                        top,
                        STDLIB_MODULES,
                        f"Non-stdlib import '{name}' found in production script",
                    )


# ---------------------------------------------------------------------------
# WebSocket low-level tests
# ---------------------------------------------------------------------------

class TestWebSocketHandshake(unittest.TestCase):
    """The adapter's WebSocket client must perform a valid RFC-6455 handshake."""

    def test_handshake_and_single_text_message(self):
        """Connect, send a text frame, receive a text frame back."""
        server = FakeCDPServer(
            handlers=[("Runtime.evaluate", {"result": {"value": 42}})],
        )
        try:
            mod = adapter

            conn = mod.CDPConnection(server.ws_url)
            result = conn.call("Runtime.evaluate", {"expression": "21+21"})
            self.assertEqual(result, {"result": {"value": 42}})
            conn.close()
        finally:
            server.close()


class TestClientFrameMasking(unittest.TestCase):
    """Client frames sent by the adapter MUST be masked per RFC-6455 §5.3."""

    def test_frames_are_masked(self):
        """Intercept raw bytes and verify mask bit is set."""
        # We reuse FakeCDPServer which asserts masking in _decode_client_frame
        server = FakeCDPServer(
            handlers=[("Page.navigate", {"frameId": "F1"})],
        )
        try:
            mod = adapter

            conn = mod.CDPConnection(server.ws_url)
            conn.call("Page.navigate", {"url": "https://example.com"})
            conn.close()
            # If we got here without AssertionError in server, masking was correct
            self.assertEqual(len(server.received), 1)
            self.assertEqual(server.received[0]["method"], "Page.navigate")
        finally:
            server.close()


# ---------------------------------------------------------------------------
# CDP call — event filtering
# ---------------------------------------------------------------------------

class TestCDPCallIgnoresEvents(unittest.TestCase):
    """CDPConnection.call() must skip incoming event frames and return the matching id."""

    def test_events_before_response_are_ignored(self):
        event = {"method": "Page.loadEventFired", "params": {"timestamp": 1.0}}
        server = FakeCDPServer(
            handlers=[("Runtime.evaluate", {"result": {"value": "hello"}})],
            inject_events=[event, event],  # two events before the real response
        )
        try:
            mod = adapter

            conn = mod.CDPConnection(server.ws_url)
            result = conn.call("Runtime.evaluate", {"expression": "'hello'"})
            self.assertEqual(result, {"result": {"value": "hello"}})
            self.assertEqual(len(conn.events), 2)
            conn.close()
        finally:
            server.close()

    def test_call_accepts_session_id_and_per_call_timeout(self):
        server = FakeCDPServer(
            handlers=[("Runtime.evaluate", {"__raw_response__": {"sessionId": "SESSION-1", "result": {"result": {"value": 7}}}})],
        )
        try:
            mod = adapter

            conn = mod.CDPConnection(server.ws_url)
            result = conn.call(
                "Runtime.evaluate",
                {"expression": "7", "returnByValue": True},
                timeout=2.0,
                session_id="SESSION-1",
            )
            self.assertEqual(result, {"result": {"value": 7}})
            self.assertEqual(server.received[0]["sessionId"], "SESSION-1")
            conn.close()
        finally:
            server.close()


# ---------------------------------------------------------------------------
# input_text tests
# ---------------------------------------------------------------------------

class InputTextRecorder(FakeCDPServer):
    """
    Extended fake that records multiple CDP calls in sequence.
    Handlers list length must equal the number of expected calls.
    """


class TestInputText(unittest.TestCase):
    """input_text must: focus element, optionally clear it, insertText, optionally press Enter."""

    def _load_adapter(self):
        return adapter

    def test_input_text_no_enter(self):
        """Without press_enter, only focus + clear + insertText calls are made."""
        # Expected CDP calls:
        # 1. Runtime.evaluate  (focus + clear script)
        # 2. Input.insertText
        handlers = [
            ("Runtime.evaluate", {}),
            ("Input.insertText", {}),
        ]
        server = FakeCDPServer(handlers=handlers)
        try:
            mod = self._load_adapter()
            conn = mod.CDPConnection(server.ws_url)
            # Use public input API against the fake CDP server.
            conn.input_text("#q", "hello world", clear=True, press_enter=False)
            conn.close()

            self.assertEqual(len(server.received), 2)
            # First call: Runtime.evaluate with focus+clear JS
            eval_call = server.received[0]
            self.assertEqual(eval_call["method"], "Runtime.evaluate")
            expr = eval_call["params"]["expression"]
            self.assertIn("focus", expr)
            self.assertIn("#q", expr)
            # Second call: Input.insertText
            insert_call = server.received[1]
            self.assertEqual(insert_call["method"], "Input.insertText")
            self.assertEqual(insert_call["params"]["text"], "hello world")
        finally:
            server.close()

    def test_input_text_with_enter(self):
        """With press_enter=True, a final Input.dispatchKeyEvent is sent."""
        handlers = [
            ("Runtime.evaluate", {}),
            ("Input.insertText", {}),
            ("Input.dispatchKeyEvent", {}),
            ("Input.dispatchKeyEvent", {}),
        ]
        server = FakeCDPServer(handlers=handlers)
        try:
            mod = self._load_adapter()
            conn = mod.CDPConnection(server.ws_url)
            conn.input_text("#q", "hello", clear=True, press_enter=True)
            conn.close()

            self.assertEqual(len(server.received), 4)
            key_call = server.received[2]
            self.assertEqual(key_call["method"], "Input.dispatchKeyEvent")
            self.assertEqual(key_call["params"]["type"], "keyDown")
            self.assertEqual(key_call["params"]["key"], "Enter")
            key_up_call = server.received[3]
            self.assertEqual(key_up_call["method"], "Input.dispatchKeyEvent")
            self.assertEqual(key_up_call["params"]["type"], "keyUp")
            self.assertEqual(key_up_call["params"]["key"], "Enter")
        finally:
            server.close()

    def test_input_text_json_escaping(self):
        """Selector and text must be safely JSON-encoded (no injection)."""
        handlers = [
            ("Runtime.evaluate", {}),
            ("Input.insertText", {}),
        ]
        # Text with characters that break naive string interpolation
        dangerous_text = 'say "hello" & goodbye\' <script>'
        server = FakeCDPServer(handlers=handlers)
        try:
            mod = self._load_adapter()
            conn = mod.CDPConnection(server.ws_url)
            conn.input_text("#field", dangerous_text, clear=True, press_enter=False)
            conn.close()

            insert_call = server.received[1]
            # The insertText text param should be the literal dangerous string
            self.assertEqual(insert_call["params"]["text"], dangerous_text)
        finally:
            server.close()


# ---------------------------------------------------------------------------
# extract_text tests
# ---------------------------------------------------------------------------

class TestExtractText(unittest.TestCase):

    def _load_adapter(self):
        return adapter

    def test_extract_text_with_selector(self):
        """extract_text(selector) calls Runtime.evaluate with a querySelector expression."""
        handlers = [("Runtime.evaluate", {"result": {"value": "page content"}})]
        server = FakeCDPServer(handlers=handlers)
        try:
            mod = self._load_adapter()
            conn = mod.CDPConnection(server.ws_url)
            text = conn.extract_text(selector="#main")
            conn.close()

            self.assertEqual(text, "page content")
            call = server.received[0]
            self.assertEqual(call["method"], "Runtime.evaluate")
            self.assertIn("querySelector", call["params"]["expression"])
            self.assertIn("#main", call["params"]["expression"])
        finally:
            server.close()

    def test_extract_text_whole_page(self):
        """extract_text() with no selector returns document.body.innerText."""
        handlers = [("Runtime.evaluate", {"result": {"value": "all text"}})]
        server = FakeCDPServer(handlers=handlers)
        try:
            mod = self._load_adapter()
            conn = mod.CDPConnection(server.ws_url)
            text = conn.extract_text(selector=None)
            conn.close()

            self.assertEqual(text, "all text")
            call = server.received[0]
            expr = call["params"]["expression"]
            self.assertIn("body", expr)
            self.assertNotIn("querySelector", expr)
        finally:
            server.close()


# ---------------------------------------------------------------------------
# Endpoint discovery tests
# ---------------------------------------------------------------------------

class TestEndpointDiscovery(unittest.TestCase):

    def _load_adapter(self):
        return adapter

    def test_discover_from_json_version(self):
        """`discover_ws_url` falls back to /json/version when no targets available."""
        disco = FakeDiscoveryServer(
            targets=[],
            version_ws_url="ws://127.0.0.1:9999/devtools/browser/BROWSER",
        )
        try:
            mod = self._load_adapter()
            url = mod.discover_ws_url(host="127.0.0.1", port=disco.port)
            # Should return the browser-level debugger URL from /json/version
            self.assertIn("ws://", url)
        finally:
            disco.close()

    def test_discover_from_json_list(self):
        """`discover_ws_url` prefers a page target from /json/list."""
        page_target = {
            "id": "PAGE1",
            "type": "page",
            "url": "https://example.com",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9999/devtools/page/PAGE1",
        }
        disco = FakeDiscoveryServer(targets=[page_target])
        try:
            mod = self._load_adapter()
            url = mod.discover_ws_url(host="127.0.0.1", port=disco.port)
            self.assertIn("PAGE1", url)
        finally:
            disco.close()

    def test_list_targets(self):
        """`list_targets_from_port` returns the raw list of target dicts."""
        targets = [
            {"id": "T1", "type": "page", "url": "https://a.com", "webSocketDebuggerUrl": "ws://..."},
            {"id": "T2", "type": "page", "url": "https://b.com", "webSocketDebuggerUrl": "ws://..."},
        ]
        disco = FakeDiscoveryServer(targets=targets)
        try:
            mod = self._load_adapter()
            result = mod.list_targets_from_port(host="127.0.0.1", port=disco.port)
            self.assertEqual(len(result), 2)
            self.assertEqual(result[0]["id"], "T1")
        finally:
            disco.close()


# ---------------------------------------------------------------------------
# Hardening regression tests
# ---------------------------------------------------------------------------

class TestExceptionHierarchy(unittest.TestCase):
    def test_exception_hierarchy_is_backward_compatible(self):
        self.assertTrue(issubclass(adapter.CDPConnectionError, ConnectionError))
        self.assertTrue(issubclass(adapter.CDPProtocolError, adapter.CDPError))
        self.assertTrue(issubclass(adapter.CDPTimeoutError, TimeoutError))
        self.assertTrue(issubclass(adapter.CDPCommandError, RuntimeError))
        self.assertTrue(issubclass(adapter.SelectorError, LookupError))
        for cls in [
            adapter.CDPConnectionError,
            adapter.CDPProtocolError,
            adapter.CDPTimeoutError,
            adapter.CDPCommandError,
            adapter.SelectorError,
        ]:
            self.assertTrue(issubclass(cls, adapter.CDPError))


class TestSecurityRegressions(unittest.TestCase):
    def test_input_text_missing_selector_raises_and_skips_insert(self):
        raw = {
            "result": {
                "result": {"type": "undefined"},
                "exceptionDetails": {"text": "Uncaught Error: selector not found: #missing"},
            }
        }
        server = FakeCDPServer(handlers=[("Runtime.evaluate", {"__raw_response__": raw})])
        try:
            conn = adapter.CDPConnection(server.ws_url)
            with self.assertRaises(adapter.SelectorError):
                conn.input_text("#missing", "secret")
            conn.close()
            self.assertEqual([m["method"] for m in server.received], ["Runtime.evaluate"])
        finally:
            server.close()

    def test_call_error_response_raises_command_error(self):
        raw = {"error": {"code": -32000, "message": "boom"}}
        server = FakeCDPServer(handlers=[("Runtime.evaluate", {"__raw_response__": raw})])
        try:
            conn = adapter.CDPConnection(server.ws_url)
            with self.assertRaises(adapter.CDPCommandError):
                conn.call("Runtime.evaluate", {"expression": "boom"})
            conn.close()
        finally:
            server.close()


class TestClickAndExtractHtml(unittest.TestCase):
    def test_click_dispatches_mouse_events_at_element_center(self):
        handlers = [
            ("Runtime.evaluate", {"result": {"value": {"x": 10.0, "y": 20.0}}}),
            ("Input.dispatchMouseEvent", {}),
            ("Input.dispatchMouseEvent", {}),
            ("Input.dispatchMouseEvent", {}),
        ]
        server = FakeCDPServer(handlers=handlers)
        try:
            conn = adapter.CDPConnection(server.ws_url)
            conn.click("#go")
            conn.close()
            methods = [m["method"] for m in server.received]
            self.assertEqual(methods, ["Runtime.evaluate", "Input.dispatchMouseEvent", "Input.dispatchMouseEvent", "Input.dispatchMouseEvent"])
            self.assertEqual(server.received[1]["params"]["type"], "mouseMoved")
            self.assertEqual(server.received[2]["params"]["type"], "mousePressed")
            self.assertEqual(server.received[3]["params"]["type"], "mouseReleased")
            self.assertEqual(server.received[2]["params"]["x"], 10.0)
            self.assertEqual(server.received[2]["params"]["y"], 20.0)
        finally:
            server.close()

    def test_click_missing_selector_raises_selector_error(self):
        server = FakeCDPServer(handlers=[("Runtime.evaluate", {"result": {"value": None}})])
        try:
            conn = adapter.CDPConnection(server.ws_url)
            with self.assertRaises(adapter.SelectorError):
                conn.click("#missing")
            conn.close()
        finally:
            server.close()

    def test_extract_html_selector_and_whole_document(self):
        server = FakeCDPServer(handlers=[
            ("Runtime.evaluate", {"result": {"value": "<main>ok</main>"}}),
            ("Runtime.evaluate", {"result": {"value": "<html><body>ok</body></html>"}}),
        ])
        try:
            conn = adapter.CDPConnection(server.ws_url)
            self.assertEqual(conn.extract_html("main"), "<main>ok</main>")
            self.assertEqual(conn.extract_html(), "<html><body>ok</body></html>")
            conn.close()
            self.assertIn("outerHTML", server.received[0]["params"]["expression"])
            self.assertIn("documentElement", server.received[1]["params"]["expression"])
        finally:
            server.close()

    def test_screenshot_decodes_and_writes_file(self):
        png = b"fake-png-bytes"
        server = FakeCDPServer(handlers=[("Page.captureScreenshot", {"data": base64.b64encode(png).decode()})])
        fd, path = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        try:
            conn = adapter.CDPConnection(server.ws_url)
            data = conn.screenshot(path)
            conn.close()
            self.assertEqual(data, png)
            self.assertEqual(pathlib.Path(path).read_bytes(), png)
        finally:
            server.close()
            try:
                os.remove(path)
            except FileNotFoundError:
                pass


class TestNavigationHardening(unittest.TestCase):
    def test_navigate_waits_for_matching_lifecycle_loader(self):
        events = [
            {"method": "Page.lifecycleEvent", "params": {"frameId": "F1", "loaderId": "OLD", "name": "load"}},
            {"method": "Page.lifecycleEvent", "params": {"frameId": "F1", "loaderId": "NEW", "name": "load"}},
        ]
        server = FakeCDPServer(handlers=[
            ("Page.enable", {}),
            ("Page.setLifecycleEventsEnabled", {}),
            ("Page.navigate", {"frameId": "F1", "loaderId": "NEW"}, events),
        ])
        try:
            conn = adapter.CDPConnection(server.ws_url)
            result = conn.navigate("data:text/html,ok", wait=True)
            conn.close()
            self.assertEqual(result["frameId"], "F1")
            self.assertEqual(result["loaderId"], "NEW")
            self.assertEqual(
                [m["method"] for m in server.received],
                ["Page.enable", "Page.setLifecycleEventsEnabled", "Page.navigate"],
            )
        finally:
            server.close()

    def test_navigate_buffers_lifecycle_event_before_response(self):
        event = {"method": "Page.lifecycleEvent", "params": {"frameId": "F1", "loaderId": "L1", "name": "load"}}
        server = FakeCDPServer(
            handlers=[
                ("Page.enable", {}),
                ("Page.setLifecycleEventsEnabled", {}),
                ("Page.navigate", {"frameId": "F1", "loaderId": "L1"}),
            ],
            inject_events=[event],
        )
        try:
            conn = adapter.CDPConnection(server.ws_url)
            result = conn.navigate("data:text/html,ok", wait=True)
            conn.close()
            self.assertEqual(result["loaderId"], "L1")
        finally:
            server.close()

    def test_navigate_accepts_same_document_event_for_matching_url(self):
        events = [
            {"method": "Page.navigatedWithinDocument", "params": {"frameId": "F1", "url": "https://example.com/#wrong"}},
            {"method": "Page.navigatedWithinDocument", "params": {"frameId": "F1", "url": "https://example.com/#section"}},
        ]
        server = FakeCDPServer(handlers=[
            ("Page.enable", {}),
            ("Page.setLifecycleEventsEnabled", {}),
            ("Page.navigate", {"frameId": "F1"}, events),
        ])
        try:
            conn = adapter.CDPConnection(server.ws_url)
            result = conn.navigate("https://example.com/#section", wait=True)
            conn.close()
            self.assertEqual(result["frameId"], "F1")
        finally:
            server.close()

    def test_navigate_error_text_raises_navigation_error(self):
        server = FakeCDPServer(handlers=[
            ("Page.enable", {}),
            ("Page.setLifecycleEventsEnabled", {}),
            ("Page.navigate", {"frameId": "F1", "errorText": "net::ERR_NAME_NOT_RESOLVED"}),
        ])
        try:
            conn = adapter.CDPConnection(server.ws_url)
            with self.assertRaises(adapter.NavigationError) as ctx:
                conn.navigate("http://missing.invalid", wait=False)
            self.assertEqual(ctx.exception.url, "http://missing.invalid")
            self.assertEqual(ctx.exception.error_text, "net::ERR_NAME_NOT_RESOLVED")
            self.assertEqual(ctx.exception.frame_id, "F1")
            conn.close()
        finally:
            server.close()

    def test_navigate_wait_false_skips_event_wait(self):
        server = FakeCDPServer(handlers=[
            ("Page.enable", {}),
            ("Page.setLifecycleEventsEnabled", {}),
            ("Page.navigate", {"frameId": "F1", "loaderId": "L1"}),
        ])
        try:
            conn = adapter.CDPConnection(server.ws_url)
            conn.navigate("data:text/html,ok", wait=False)
            conn.close()
            self.assertEqual(
                [m["method"] for m in server.received],
                ["Page.enable", "Page.setLifecycleEventsEnabled", "Page.navigate"],
            )
        finally:
            server.close()

    def test_download_navigation_returns_without_load_wait(self):
        server = FakeCDPServer(handlers=[
            ("Page.enable", {}),
            ("Page.setLifecycleEventsEnabled", {}),
            ("Page.navigate", {"frameId": "F1", "loaderId": "L1", "isDownload": True}),
        ])
        try:
            conn = adapter.CDPConnection(server.ws_url)
            result = conn.navigate("https://example.com/file.zip", wait=True)
            conn.close()
            self.assertTrue(result["isDownload"])
        finally:
            server.close()


class TestWebSocketHardening(unittest.TestCase):
    def test_server_ping_gets_client_pong(self):
        ping = _encode_server_frame(b"hello", opcode=0x9)
        server = FakeCDPServer(
            handlers=[("Runtime.evaluate", {"result": {"value": 1}})],
            pre_frames=[ping],
        )
        try:
            conn = adapter.CDPConnection(server.ws_url)
            self.assertEqual(conn.evaluate("1"), 1)
            conn.close()
            self.assertIn(0xA, server.received_opcodes)
        finally:
            server.close()

    def test_server_pong_is_ignored(self):
        pong = _encode_server_frame(b"ignored", opcode=0xA)
        server = FakeCDPServer(
            handlers=[("Runtime.evaluate", {"result": {"value": 2}})],
            pre_frames=[pong],
        )
        try:
            conn = adapter.CDPConnection(server.ws_url)
            self.assertEqual(conn.evaluate("2"), 2)
            conn.close()
        finally:
            server.close()

    def test_oversized_frame_raises_protocol_error(self):
        huge_header = bytes([0x81, 127]) + struct.pack(">Q", adapter._WS_MAX_PAYLOAD + 1)
        server = FakeCDPServer(
            handlers=[("Runtime.evaluate", {"result": {"value": 3}})],
            pre_frames=[huge_header],
        )
        try:
            conn = adapter.CDPConnection(server.ws_url)
            with self.assertRaises(adapter.CDPProtocolError):
                conn.call("Runtime.evaluate", {"expression": "3"})
            self.assertIsNone(conn._sock)
            conn.close()
        finally:
            server.close()


class TestLaunchAndConfigHardening(unittest.TestCase):
    def test_wait_until_ready_success_and_timeout(self):
        disco = FakeDiscoveryServer(targets=[])
        try:
            self.assertIsNone(adapter.wait_until_ready(port=disco.port, timeout=1.0))
        finally:
            disco.close()
        with self.assertRaises(adapter.CDPTimeoutError):
            adapter.wait_until_ready(port=9, timeout=0.1)

    def test_launch_chrome_prefers_path_candidate_from_shutil_which(self):
        expected = r"C:\\Tools\\Chrome\\chrome.exe"
        launched = []

        def fake_run(cmd, **kwargs):
            if cmd[0] != expected:
                raise FileNotFoundError(cmd[0])
            return mock.Mock(returncode=0)

        with mock.patch.object(adapter.shutil, "which", return_value=expected), \
             mock.patch("subprocess.run", side_effect=fake_run), \
             mock.patch("subprocess.Popen", side_effect=lambda cmd, **kwargs: launched.append(cmd) or mock.Mock()):
            adapter.launch_chrome(ready_timeout=0, user_data_dir="C:\\Temp\\BareCDP")

        self.assertEqual(launched[0][0], expected)

    def test_launch_chrome_checks_programw6432_on_windows(self):
        env = {"ProgramW6432": "C:\\Program Files"}
        expected = "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"
        launched = []

        def fake_run(cmd, **kwargs):
            if cmd[0] != expected:
                raise FileNotFoundError(cmd[0])
            return mock.Mock(returncode=0)

        with mock.patch.object(adapter.shutil, "which", return_value=None), \
             mock.patch.dict(os.environ, env, clear=False), \
             mock.patch("subprocess.run", side_effect=fake_run), \
             mock.patch("subprocess.Popen", side_effect=lambda cmd, **kwargs: launched.append(cmd) or mock.Mock()):
            adapter.launch_chrome(ready_timeout=0, user_data_dir="C:\\Temp\\BareCDP")

        self.assertEqual(launched[0][0], expected)

    def test_launch_chrome_checks_windows_program_files_and_local_appdata(self):
        env = {
            "ProgramFiles": "C:\\Program Files",
            "ProgramFiles(x86)": "C:\\Program Files (x86)",
            "LOCALAPPDATA": "C:\\Users\\alice\\AppData\\Local",
        }
        expected = "C:\\Users\\alice\\AppData\\Local\\Google\\Chrome\\Application\\chrome.exe"
        launched = []

        def fake_run(cmd, **kwargs):
            if cmd[0] != expected:
                raise FileNotFoundError(cmd[0])
            return mock.Mock(returncode=0)

        with mock.patch.object(adapter.shutil, "which", return_value=None), \
             mock.patch.dict(os.environ, env, clear=False), \
             mock.patch("subprocess.run", side_effect=fake_run), \
             mock.patch("subprocess.Popen", side_effect=lambda cmd, **kwargs: launched.append(cmd) or mock.Mock()):
            adapter.launch_chrome(ready_timeout=0, user_data_dir="C:\\Temp\\BareCDP")

        self.assertEqual(launched[0][0], expected)

    @unittest.skipUnless(os.name == "posix", "uses POSIX /usr/bin/false fixture")
    def test_launch_chrome_detects_early_exit(self):
        with self.assertRaises(adapter.CDPConnectionError):
            adapter.launch_chrome(executable="/usr/bin/false", port=9, ready_timeout=0.5)

    @unittest.skipUnless(os.name == "posix", "uses POSIX /bin/echo fixture")
    def test_terminate_chrome_removes_created_profile_but_preserves_user_profile(self):
        launch = adapter.launch_chrome(executable="/bin/echo", ready_timeout=0)
        temp_dir = launch.user_data_dir
        self.assertTrue(launch.owns_user_data_dir)
        self.assertTrue(os.path.isdir(temp_dir))
        adapter.terminate_chrome(launch)
        self.assertFalse(os.path.exists(temp_dir))

        user_dir = tempfile.mkdtemp(prefix="barecdp-user-profile-")
        try:
            launch2 = adapter.launch_chrome(executable="/bin/echo", user_data_dir=user_dir, ready_timeout=0)
            adapter.terminate_chrome(launch2)
            self.assertTrue(os.path.isdir(user_dir))
        finally:
            shutil.rmtree(user_dir, ignore_errors=True)

    def test_load_config_environment_overrides(self):
        env = {
            "BARE_CDP_HOST": "localhost",
            "BARE_CDP_PORT": "9333",
            "BARE_CDP_HEADLESS": "false",
            "BARE_CDP_TIMEOUT": "2.5",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            cfg = adapter.load_config(None)
        self.assertEqual(cfg["chrome"]["host"], "localhost")
        self.assertEqual(cfg["chrome"]["port"], 9333)
        self.assertFalse(cfg["chrome"]["headless"])
        self.assertEqual(cfg["timeouts"]["default"], 2.5)

    def test_context_managers_close(self):
        server = FakeCDPServer(handlers=[])
        try:
            with adapter.CDPConnection(server.ws_url) as conn:
                self.assertIsNotNone(conn._sock)
            self.assertIsNone(conn._sock)
        finally:
            server.close()


class TestV020CoreRouting(unittest.TestCase):
    def test_empty_session_id_is_rejected_before_send(self):
        server = FakeCDPServer(handlers=[])
        try:
            conn = adapter.CDPConnection(server.ws_url)
            with self.assertRaisesRegex(ValueError, "session_id"):
                conn.call("Runtime.evaluate", {"expression": "1"}, session_id="")
            conn.close()
            self.assertEqual(server.received, [])
            with self.assertRaisesRegex(ValueError, "session_id"):
                adapter.CDPSession(conn, "")
        finally:
            server.close()

    def test_wrong_response_id_raises_protocol_error_and_closes(self):
        server = FakeCDPServer(
            handlers=[("Runtime.evaluate", {"__raw_response__": {"id": 999, "result": {}}})],
        )
        try:
            conn = adapter.CDPConnection(server.ws_url)
            with self.assertRaises(adapter.CDPProtocolError):
                conn.call("Runtime.evaluate", {"expression": "1"})
            self.assertTrue(conn.closed)
        finally:
            server.close()

    def test_response_session_mismatch_raises_protocol_error_and_closes(self):
        server = FakeCDPServer(
            handlers=[("Runtime.evaluate", {"__raw_response__": {"sessionId": "OTHER", "result": {}}})],
        )
        try:
            conn = adapter.CDPConnection(server.ws_url)
            with self.assertRaises(adapter.CDPProtocolError):
                conn.call("Runtime.evaluate", {"expression": "1"}, session_id="SESSION")
            self.assertTrue(conn.closed)
        finally:
            server.close()

    def test_events_are_session_aware_and_any_session_is_explicit(self):
        events = [
            {"method": "Runtime.consoleAPICalled", "sessionId": "A", "params": {"value": "a"}},
            {"method": "Runtime.consoleAPICalled", "sessionId": "B", "params": {"value": "b"}},
        ]
        server = FakeCDPServer(
            handlers=[("Runtime.evaluate", {"result": {"value": 1}})],
            inject_events=events,
        )
        try:
            conn = adapter.CDPConnection(server.ws_url)
            conn.call("Runtime.evaluate", {"expression": "1"})
            self.assertEqual(
                conn.wait_for_event("Runtime.consoleAPICalled", session_id="B"),
                {"value": "b"},
            )
            self.assertEqual(
                conn.wait_for_event("Runtime.consoleAPICalled", session_id=adapter.ANY_SESSION),
                {"value": "a"},
            )
            conn.close()
        finally:
            server.close()

    def test_after_sequence_ignores_earlier_buffered_event(self):
        conn = object.__new__(adapter.CDPConnection)
        conn._io_lock = threading.RLock()
        conn._event_sequence = 0
        conn._events = adapter.collections.deque(maxlen=2000)
        conn._dropped_event_count = 0
        conn._timeout = 1.0
        conn._sock = None
        conn._ws = None
        conn._queue_event({"method": "Page.lifecycleEvent", "params": {"name": "old"}})
        cursor = conn.event_cursor()
        conn._queue_event({"method": "Page.lifecycleEvent", "params": {"name": "new"}})
        self.assertEqual(
            conn.wait_for_event("Page.lifecycleEvent", after_sequence=cursor),
            {"name": "new"},
        )

    def test_event_queue_overflow_increments_dropped_count(self):
        conn = object.__new__(adapter.CDPConnection)
        conn._io_lock = threading.RLock()
        conn._event_sequence = 0
        conn._events = adapter.collections.deque(maxlen=2)
        conn._dropped_event_count = 0
        conn._timeout = 1.0
        conn._sock = None
        conn._ws = None
        for idx in range(3):
            conn._queue_event({"method": "Event", "params": {"idx": idx}})
        self.assertEqual(conn.dropped_event_count, 1)
        self.assertEqual([ev.params["idx"] for ev in conn.recent_events()], [1, 2])


    def test_session_bound_wait_rejects_explicit_session_id(self):
        session = adapter.CDPSession(mock.Mock(), "SESSION")
        with self.assertRaisesRegex(TypeError, "already bound"):
            session.wait_for_event("Runtime.consoleAPICalled", session_id=adapter.ANY_SESSION)

    def test_two_threads_calling_same_connection_are_serialized(self):
        server = FakeCDPServer(handlers=[
            ("Runtime.evaluate", {"result": {"value": "one"}}),
            ("Runtime.evaluate", {"result": {"value": "two"}}),
        ])
        try:
            conn = adapter.CDPConnection(server.ws_url)
            results = []
            errors = []

            def worker(expr):
                try:
                    results.append(conn.call("Runtime.evaluate", {"expression": expr})["result"]["value"])
                except Exception as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=worker, args=("1",)), threading.Thread(target=worker, args=("2",))]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=2)
            conn.close()
            self.assertFalse(errors)
            self.assertEqual(sorted(results), ["one", "two"])
            self.assertEqual([msg["id"] for msg in server.received], [1, 2])
        finally:
            server.close()


    def test_high_level_actions_hold_transaction_for_constituent_commands(self):
        for action in ("navigate", "click", "input_text"):
            conn = object.__new__(adapter.CDPConnection)
            conn._io_lock = threading.RLock()
            conn._timeout = 1.0
            conn._page_enabled = False
            state = {"active": 0, "calls": []}

            @adapter.contextlib.contextmanager
            def tx():
                state["active"] += 1
                try:
                    yield
                finally:
                    state["active"] -= 1

            def call(method, params=None, timeout=None, session_id=None):
                self.assertGreater(state["active"], 0, f"{action} call {method} escaped transaction")
                state["calls"].append(method)
                if method == "Page.navigate":
                    return {"frameId": "F1", "loaderId": "L1"}
                if method == "Runtime.evaluate":
                    return {"result": {"value": True}}
                return {}

            def evaluate(expression, return_by_value=True, timeout=None):
                self.assertGreater(state["active"], 0, f"{action} evaluate escaped transaction")
                state["calls"].append("Runtime.evaluate")
                if action == "click":
                    return {"x": 10, "y": 20}
                return True

            def wait_for_event(event_name, predicate=None, timeout=None, **kwargs):
                self.assertGreater(state["active"], 0, f"{action} wait escaped transaction")
                state["calls"].append(event_name)
                return {}

            conn.transaction = tx
            conn.call = call
            conn.evaluate = evaluate
            conn.wait_for_event = wait_for_event
            conn.event_cursor = lambda: 0

            if action == "navigate":
                conn.navigate("https://example.com")
                self.assertIn("Page.lifecycleEvent", state["calls"])
            elif action == "click":
                conn.click("#button")
                self.assertEqual(state["calls"].count("Input.dispatchMouseEvent"), 3)
            else:
                conn.input_text("#q", "hello", clear=False, press_enter=False)
                self.assertIn("Input.insertText", state["calls"])


class TestBrowserPassThroughMethods(unittest.TestCase):
    def test_browser_exposes_connection_actions_explicitly(self):
        expected = [
            "call",
            "wait_for_event",
            "event_cursor",
            "recent_events",
            "attach_session",
            "attach_to_target",
            "navigate",
            "wait_for_ready_state",
            "evaluate",
            "wait_for_selector",
            "click",
            "input_text",
            "press",
            "extract_text",
            "extract_html",
            "screenshot",
        ]
        for name in expected:
            self.assertIn(name, adapter.ChromeCDPAdapter.__dict__, name)
        for name in ("events", "dropped_event_count"):
            self.assertIsInstance(adapter.ChromeCDPAdapter.__dict__[name], property, name)

    def test_browser_pass_through_parameter_signatures_track_connection_methods(self):
        expected = [
            "call",
            "wait_for_event",
            "event_cursor",
            "recent_events",
            "attach_session",
            "attach_to_target",
            "navigate",
            "wait_for_ready_state",
            "evaluate",
            "wait_for_selector",
            "click",
            "input_text",
            "press",
            "extract_text",
            "extract_html",
            "screenshot",
        ]
        for name in expected:
            adapter_sig = inspect.signature(getattr(adapter.ChromeCDPAdapter, name))
            connection_sig = inspect.signature(getattr(adapter.CDPConnection, name))
            adapter_params = list(adapter_sig.parameters.values())
            connection_params = list(connection_sig.parameters.values())
            self.assertEqual([param.name for param in adapter_params], [param.name for param in connection_params], name)
            self.assertEqual([param.kind for param in adapter_params], [param.kind for param in connection_params], name)
            self.assertEqual([param.default for param in adapter_params], [param.default for param in connection_params], name)

    def test_direct_action_lazily_connects_once_and_reuses_connection(self):
        browser = adapter.ChromeCDPAdapter(timeout=1.0)
        fake = mock.Mock()
        fake.navigate.return_value = {"frameId": "F"}
        fake.extract_text.return_value = "text"

        def connect_once():
            browser._conn = fake
            return fake

        with mock.patch.object(browser, "connect", side_effect=connect_once) as connect:
            self.assertEqual(browser.navigate("https://example.com"), {"frameId": "F"})
            self.assertEqual(browser.extract_text(), "text")
            connect.assert_called_once_with()
        fake.navigate.assert_called_once_with("https://example.com", wait=True, timeout=None, wait_until="load")
        fake.extract_text.assert_called_once_with(selector=None)

    def test_browser_pass_through_methods_delegate_to_current_connection(self):
        browser = adapter.ChromeCDPAdapter(timeout=1.0)
        fake = mock.Mock()
        fake.events = ("queued",)
        fake.dropped_event_count = 2
        fake.call.return_value = {"result": "call"}
        fake.wait_for_event.return_value = {"event": "ok"}
        fake.event_cursor.return_value = 42
        fake.recent_events.return_value = ("recent",)
        fake.attach_session.return_value = "session"
        fake.attach_to_target.return_value = "session-id"
        fake.navigate.return_value = {"frameId": "F"}
        fake.wait_for_ready_state.return_value = "complete"
        fake.evaluate.return_value = "value"
        fake.wait_for_selector.return_value = True
        fake.extract_text.return_value = "text"
        fake.extract_html.return_value = "<html></html>"
        fake.screenshot.return_value = b"png"
        browser._conn = fake

        self.assertEqual(browser.events, ("queued",))
        self.assertEqual(browser.dropped_event_count, 2)
        self.assertEqual(
            browser.call("Runtime.evaluate", {"expression": "1"}, timeout=3, session_id="S"),
            {"result": "call"},
        )
        fake.call.assert_called_once_with(
            "Runtime.evaluate",
            params={"expression": "1"},
            timeout=3,
            session_id="S",
        )

        predicate = lambda params: True
        self.assertEqual(
            browser.wait_for_event(
                "Runtime.consoleAPICalled",
                predicate=predicate,
                timeout=4,
                session_id=adapter.ANY_SESSION,
                after_sequence=9,
            ),
            {"event": "ok"},
        )
        fake.wait_for_event.assert_called_once_with(
            "Runtime.consoleAPICalled",
            predicate=predicate,
            timeout=4,
            session_id=adapter.ANY_SESSION,
            after_sequence=9,
        )

        self.assertEqual(browser.event_cursor(), 42)
        self.assertEqual(browser.recent_events(), ("recent",))
        self.assertEqual(browser.attach_session("TARGET"), "session")
        self.assertEqual(browser.attach_to_target("TARGET"), "session-id")
        self.assertEqual(browser.navigate("https://example.com", wait=False, timeout=1, wait_until="commit"), {"frameId": "F"})
        fake.navigate.assert_called_once_with("https://example.com", wait=False, timeout=1, wait_until="commit")
        self.assertEqual(browser.wait_for_ready_state(("complete",), timeout=2), "complete")
        self.assertEqual(browser.evaluate("document.title", return_by_value=False, timeout=2), "value")
        self.assertTrue(browser.wait_for_selector("main", timeout=2))
        browser.click("button")
        fake.click.assert_called_once_with("button")
        browser.input_text("input", "hello", clear=False, press_enter=True)
        fake.input_text.assert_called_once_with("input", "hello", clear=False, press_enter=True)
        browser.press("Enter")
        fake.press.assert_called_once_with("Enter")
        self.assertEqual(browser.extract_text("main"), "text")
        self.assertEqual(browser.extract_html("main"), "<html></html>")
        self.assertEqual(browser.screenshot("page.png", format="jpeg"), b"png")



class TestV020LaunchAndOwnership(unittest.TestCase):
    def test_launch_chrome_port_zero_reads_and_verifies_devtools_active_port(self):
        disco = FakeDiscoveryServer(
            targets=[],
            version_ws_url="ws://127.0.0.1:0/devtools/browser/BOUND",
        )
        # Recreate with its actual port in the browser URL.
        disco.close()
        disco = FakeDiscoveryServer(
            targets=[],
            version_ws_url=None,
        )
        created = []

        def fake_popen(cmd, **kwargs):
            user_dir = next(arg.split("=", 1)[1] for arg in cmd if arg.startswith("--user-data-dir="))
            marker = pathlib.Path(user_dir) / "DevToolsActivePort"
            marker.write_text(f"{disco.port}\n/devtools/browser/FAKE\n")
            proc = mock.Mock()
            proc.poll.return_value = None
            proc.returncode = None
            created.append(proc)
            return proc

        try:
            with mock.patch("subprocess.Popen", side_effect=fake_popen):
                launch = adapter.launch_chrome(executable="/bin/echo", port=0, ready_timeout=1.0)
            self.assertEqual(launch.port, disco.port)
            self.assertEqual(urllib.parse.urlparse(launch.browser_ws_url).path, "/devtools/browser/FAKE")
            self.assertTrue(launch.owns_user_data_dir)
            adapter.terminate_chrome(launch)
        finally:
            disco.close()

    def test_devtools_active_port_partial_write_is_retried(self):
        disco = FakeDiscoveryServer(targets=[], version_ws_url=None)

        def fake_popen(cmd, **kwargs):
            user_dir = next(arg.split("=", 1)[1] for arg in cmd if arg.startswith("--user-data-dir="))
            marker = pathlib.Path(user_dir) / "DevToolsActivePort"
            marker.write_text(str(disco.port))

            def complete_marker():
                time.sleep(0.1)
                marker.write_text(f"{disco.port}\n/devtools/browser/FAKE\n")

            threading.Thread(target=complete_marker, daemon=True).start()
            proc = mock.Mock()
            proc.poll.return_value = None
            proc.returncode = None
            return proc

        try:
            with mock.patch("subprocess.Popen", side_effect=fake_popen):
                launch = adapter.launch_chrome(executable="/bin/echo", port=0, ready_timeout=2.0)
            self.assertEqual(launch.port, disco.port)
            adapter.terminate_chrome(launch)
        finally:
            disco.close()


    def test_launch_chrome_popen_failure_cleans_temp_profile_and_stderr_file(self):
        touched = {}

        def fake_popen(cmd, **kwargs):
            user_dir = next(arg.split("=", 1)[1] for arg in cmd if arg.startswith("--user-data-dir="))
            stderr_handle = kwargs["stderr"]
            touched["user_dir"] = user_dir
            touched["stderr_path"] = stderr_handle.name
            pathlib.Path(stderr_handle.name).write_text("startup failed")
            raise OSError("cannot spawn chrome")

        with mock.patch("subprocess.Popen", side_effect=fake_popen):
            with self.assertRaisesRegex(OSError, "cannot spawn chrome"):
                adapter.launch_chrome(executable="/bin/echo", ready_timeout=0)
        self.assertFalse(os.path.exists(touched["user_dir"]))
        self.assertFalse(os.path.exists(touched["stderr_path"]))

    def test_adapter_replacement_failure_leaves_old_connection_and_close_closes_all(self):
        made = []

        class FakeConnection:
            def __init__(self, ws_url, timeout=10.0):
                if ws_url == "bad":
                    raise adapter.CDPConnectionError("boom")
                self.ws_url = ws_url
                self.closed = False
                made.append(self)

            def close(self):
                self.closed = True

        browser = adapter.ChromeCDPAdapter(timeout=1.0)
        with mock.patch.object(adapter, "CDPConnection", FakeConnection):
            first = browser.connect("one")
            with self.assertRaises(adapter.CDPConnectionError):
                browser.connect("bad")
            self.assertIs(browser.connection, first)
            second = browser.connect("two")
            self.assertTrue(first.closed)
            extra = browser.open_connection("three")
            browser.close()
            self.assertTrue(second.closed)
            self.assertTrue(extra.closed)
            self.assertTrue(all(conn.closed for conn in made))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main()
