"""Exercise real HTTP multipart and WebSocket framing against a local fake agent API."""

import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
import struct
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from PIL import Image

from src.bambu_client import BambuClient, PrintState, PrintStatus
from src.bambu_obico import BambuObicoBridge


class TransportTests(unittest.TestCase):
    def test_real_agent_auth_status_snapshot_and_pause_round_trip(self):
        messages, uploads, errors = [], [], []
        connected = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_):
                pass

            def reply(self, code, data):
                payload = json.dumps(data).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                if self.path == "/api/v1/octo/printer/":
                    if self.headers.get("Authorization") != "Token test-agent-token":
                        return self.reply(401, {})
                    return self.reply(200, {"printer": {"id": 9}})
                # Match Obico's case-sensitive bearer header and /ws/dev/ route.
                if self.path != "/ws/dev/" or self.headers.get("Authorization") != "bearer test-agent-token":
                    return self.reply(403, {})
                key = self.headers.get("Sec-WebSocket-Key", "")
                accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
                self.send_response(101)
                self.send_header("Upgrade", "websocket")
                self.send_header("Connection", "Upgrade")
                self.send_header("Sec-WebSocket-Accept", accept)
                self.end_headers()
                connected.set()
                self.connection.settimeout(5)
                try:
                    while True:
                        header = self.rfile.read(2)
                        if len(header) != 2:
                            break
                        opcode, length = header[0] & 15, header[1] & 127
                        if length == 126:
                            length = struct.unpack("!H", self.rfile.read(2))[0]
                        elif length == 127:
                            length = struct.unpack("!Q", self.rfile.read(8))[0]
                        if length > 1024 * 1024:
                            raise ValueError("oversized frame")
                        mask = self.rfile.read(4) if header[1] & 128 else b""
                        data = self.rfile.read(length)
                        if mask:
                            data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
                        if opcode == 8:
                            self.wfile.write(bytes([0x88, len(data)]) + data)
                            break
                        if opcode == 9:
                            self.wfile.write(bytes([0x8a, len(data)]) + data)
                        if opcode != 1:
                            continue
                        message = json.loads(data)
                        messages.append(message)
                        if message.get("event", {}).get("event_type") == "PrintStarted":
                            command = b'{"commands":[{"cmd":"pause","initiator":"system"}]}'
                            self.wfile.write(bytes([0x81, len(command)]) + command)
                except Exception as exc:
                    errors.append(type(exc).__name__)

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if self.path != "/api/v1/octo/pic/" or self.headers.get("Authorization") != "Token test-agent-token":
                    return self.reply(403, {})
                uploads.append((self.headers.get("Content-Type"), body))
                self.reply(200, {"result": "ok"})

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        printer = BambuClient("P1S-home", "192.0.2.1", "unused-lan-code", "unused-serial")
        printer._connected.set()
        printer._last_report_at = time.monotonic()
        printer._state = PrintState(status=PrintStatus.RUNNING, subtask_name="home-test.3mf", job_id="job-9", mc_percent=12)
        farm, cameras = MagicMock(), MagicMock()
        farm.get_printer.return_value = printer
        jpeg = BytesIO()
        Image.new("RGB", (32, 32), "white").save(jpeg, format="JPEG")
        cameras.get_frame_with_time.return_value = (jpeg.getvalue(), time.monotonic())
        bridge = BambuObicoBridge("P1S-home", {
            "server": f"http://127.0.0.1:{server.server_port}", "printer_id": 9,
            "username": "unused-account", "password": "unused-password",
            "auth_token": "test-agent-token", "allow_remote_control": True,
        }, farm, cameras, tmp.name)
        bridge.reader.fetch_status = MagicMock(return_value={"connected": True})
        self.addCleanup(bridge.stop)

        def pause():
            bridge.stop_event.set()
            return True

        printer.pause_print = MagicMock(side_effect=pause)
        with patch.dict("os.environ", {"NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"}):
            bridge.start()
            self.assertTrue(connected.wait(5), "Agent WebSocket did not authenticate")
            bridge.thread.join(timeout=8)
            self.assertFalse(bridge.thread.is_alive(), "Pause command did not round-trip")
        self.assertEqual(errors, [])
        settings = next(m["settings"] for m in messages if "settings" in m)
        self.assertEqual(settings["webcams"][0]["stream_mode"], "snapshot")
        # Older Obico versions call int(rotation); absent/None crashes their API.
        webcam = settings["webcams"][0]
        self.assertEqual(int(webcam["rotation"]), 0)
        self.assertIs(webcam["flipV"], False)
        self.assertIs(webcam["flipH"], False)
        self.assertTrue(any(m.get("status", {}).get("job", {}).get("file", {}).get("name") == "home-test.3mf" for m in messages))
        self.assertEqual(len(uploads), 1)
        content_type, body = uploads[0]
        self.assertIn("multipart/form-data", content_type)
        self.assertIn(b'name="pic"', body)
        self.assertIn(jpeg.getvalue(), body)
        self.assertIn(b'name="viewing_boost"\r\n\r\nfalse', body)
        printer.pause_print.assert_called_once()
        self.assertEqual(bridge.ledger.data["filename"], "home-test.3mf")


if __name__ == "__main__":
    unittest.main()
