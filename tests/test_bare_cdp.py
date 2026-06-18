"""
Tests for bare_cdp.py — strict TDD, stdlib only.

Covers:
- Import audit (only stdlib modules loaded by production script)
- Low-level WebSocket handshake + frame masking
- CDP call: ignores events, returns matching response id
- input_text: focus/clear JS, Input.insertText, optional Enter
- extract_text: selector vs whole-page
- Endpoint discovery: /json/version and /json/list via stdlib HTTP server
"""

import base64
import hashlib
import json
import socket
import struct
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable, List, Optional, Tuple

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
        handlers: Optional[List[Tuple[str, Optional[dict]]]] = None,
        inject_events: Optional[List[dict]] = None,
    ):
        self.handlers = list(handlers or [])
        self.inject_events = list(inject_events or [])
        self._received: List[dict] = []
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
        for expected_method, result_body in self.handlers:
            frame_data = self._recv_frame(conn)
            opcode, payload = _decode_client_frame(frame_data)
            msg = json.loads(payload.decode())
            self._received.append(msg)

            # Optionally inject events before the real response
            for ev in self.inject_events:
                ev_frame = _encode_server_frame(json.dumps(ev).encode())
                conn.sendall(ev_frame)

            resp: dict = {"id": msg["id"]}
            if result_body is not None:
                resp["result"] = result_body
            resp_frame = _encode_server_frame(json.dumps(resp).encode())
            conn.sendall(resp_frame)

    def _recv_frame(self, conn: socket.socket, bufsize: int = 65536) -> bytes:
        buf = b""
        while True:
            chunk = conn.recv(bufsize)
            if not chunk:
                raise ConnectionError("server got EOF")
            buf += chunk
            # Minimal length check: header at least 6 bytes
            if len(buf) >= 6:
                return buf


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
            handlers=[("Runtime.evaluate", {"result": {"value": 7}})],
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
            # Call internal helper directly to avoid needing a real DOM
            conn._input_text_raw("#q", "hello world", clear=True, press_enter=False)
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
            conn._input_text_raw("#q", "hello", clear=True, press_enter=True)
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
            conn._input_text_raw("#field", dangerous_text, clear=True, press_enter=False)
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
            text = conn._extract_text_raw(selector="#main")
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
            text = conn._extract_text_raw(selector=None)
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
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main()
