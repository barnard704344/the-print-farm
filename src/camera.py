"""
BambuLab P1S Camera Stream Client.

Connects to the P1S camera on port 6000 over TLS and receives JPEG frames.
Provides frames to the web UI as an MJPEG stream.

Protocol (from HA bambulab integration):
  - Connect TLS to port 6000
  - Send 80-byte auth: [0x40,0,0,0] [0x00,0x30,0,0] [0,0,0,0] [0,0,0,0]
    [username 32B] [access_code 32B]
  - Receive 16-byte header per frame:
      bytes 0:3 = payload size (little-endian)
      bytes 4:15 = fixed
  - Receive payload_size bytes of JPEG data (starts with FF D8, ends with FF D9)
  - After full frame, SSLWantReadError until next frame (~1-2 sec)
"""

import logging
import socket
import ssl
import struct
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

JPEG_START = b'\xff\xd8\xff\xe0'
JPEG_END = b'\xff\xd9'
STALE_FRAME_SECONDS = 15


class BambuCamera:
    """Receives JPEG frames from a P1S camera over TLS."""

    def __init__(self, host: str, access_code: str, port: int = 6000):
        self.host = host
        self.access_code = access_code
        self.port = port

        self._latest_frame: Optional[bytes] = None
        self._last_frame_at = 0.0
        self._frame_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._connected = False

    @property
    def latest_frame(self) -> Optional[bytes]:
        with self._frame_lock:
            return self._latest_frame

    @property
    def latest_frame_with_time(self) -> tuple[Optional[bytes], float]:
        with self._frame_lock:
            return self._latest_frame, self._last_frame_at

    @property
    def last_frame_age(self) -> Optional[float]:
        with self._frame_lock:
            if not self._last_frame_at:
                return None
            return max(0.0, time.monotonic() - self._last_frame_at)

    @property
    def is_streaming(self) -> bool:
        age = self.last_frame_age
        fresh = age is not None and age <= STALE_FRAME_SECONDS
        return self._thread is not None and self._thread.is_alive() and self._connected and fresh

    @property
    def is_stale(self) -> bool:
        age = self.last_frame_age
        return self._connected and (age is None or age > STALE_FRAME_SECONDS)

    def start(self):
        """Start the camera receiver thread."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._receive_loop, daemon=True,
            name=f"camera-{self.host}"
        )
        self._thread.start()
        logger.info(f"Camera stream started for {self.host}")

    def stop(self):
        """Stop the camera receiver thread."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._connected = False
        logger.info(f"Camera stream stopped for {self.host}")

    def _build_auth(self) -> bytes:
        """Build the 80-byte auth payload."""
        auth = bytearray()
        auth += struct.pack("<I", 0x40)      # 0x40 0x00 0x00 0x00
        auth += struct.pack("<I", 0x3000)    # 0x00 0x30 0x00 0x00
        auth += struct.pack("<I", 0)         # padding
        auth += struct.pack("<I", 0)         # padding

        # Username: 'bblp' padded to 32 bytes
        username = b'bblp'
        auth += username + b'\x00' * (32 - len(username))

        # Access code padded to 32 bytes
        code = self.access_code.encode('ascii')
        auth += code + b'\x00' * (32 - len(code))

        return bytes(auth)

    def _receive_loop(self):
        """Main loop: connect, auth, receive JPEG frames. Retries forever."""
        while not self._stop_event.is_set():
            try:
                self._stream_frames()
            except Exception as e:
                logger.warning(f"Camera {self.host} stream error: {e}")
                self._connected = False
                if not self._stop_event.is_set():
                    self._stop_event.wait(timeout=5)

    def _stream_frames(self):
        """Connect and stream frames until error or stop."""
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        with socket.create_connection((self.host, self.port), timeout=5) as sock:
            ssl_sock = ctx.wrap_socket(sock, server_hostname=self.host)
            ssl_sock.write(self._build_auth())
            ssl_sock.setblocking(False)

            logger.info(f"Camera connected to {self.host}:{self.port}")
            self._connected = True
            stream_started = time.monotonic()

            img = None
            payload_size = 0

            while not self._stop_event.is_set():
                if time.monotonic() - stream_started > STALE_FRAME_SECONDS and self.is_stale:
                    raise RuntimeError("Camera stream stale")
                try:
                    data = ssl_sock.recv(4096)
                except ssl.SSLWantReadError:
                    if self._stop_event.wait(timeout=0.5):
                        break
                    continue

                if len(data) == 0:
                    logger.error(f"Camera {self.host}: received 0 bytes (auth rejected?)")
                    raise RuntimeError("No data from camera")

                if img is not None and len(data) > 0:
                    img += data
                    if len(img) > payload_size:
                        logger.warning(f"Camera {self.host}: frame overrun, resetting")
                        img = None
                    elif len(img) == payload_size:
                        # Full frame received
                        if img[:4] == JPEG_START and img[-2:] == JPEG_END:
                            with self._frame_lock:
                                self._latest_frame = bytes(img)
                                self._last_frame_at = time.monotonic()
                        else:
                            logger.warning(f"Camera {self.host}: invalid JPEG markers")
                        img = None

                elif len(data) == 16:
                    # Frame header: first 4 bytes = payload size (LE)
                    payload_size = int.from_bytes(data[0:4], byteorder='little')
                    img = bytearray()

                else:
                    logger.warning(f"Camera {self.host}: unexpected chunk size {len(data)}")
                    raise RuntimeError(f"Unexpected data: {len(data)} bytes")


class HttpCamera:
    """Fetches JPEG snapshots from an HTTP/MJPEG webcam URL (e.g. Klipper crowsnest)."""

    def __init__(self, url: str):
        self.url = url
        self._latest_frame: Optional[bytes] = None
        self._last_frame_at = 0.0
        self._frame_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._connected = False

    @property
    def latest_frame(self) -> Optional[bytes]:
        with self._frame_lock:
            return self._latest_frame

    @property
    def latest_frame_with_time(self) -> tuple[Optional[bytes], float]:
        with self._frame_lock:
            return self._latest_frame, self._last_frame_at

    @property
    def last_frame_age(self) -> Optional[float]:
        with self._frame_lock:
            if not self._last_frame_at:
                return None
            return max(0.0, time.monotonic() - self._last_frame_at)

    @property
    def is_streaming(self) -> bool:
        age = self.last_frame_age
        fresh = age is not None and age <= STALE_FRAME_SECONDS
        return self._thread is not None and self._thread.is_alive() and self._connected and fresh

    @property
    def is_stale(self) -> bool:
        age = self.last_frame_age
        return self._connected and (age is None or age > STALE_FRAME_SECONDS)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._fetch_loop, daemon=True,
            name=f"httpcam-{self.url[:40]}"
        )
        self._thread.start()
        logger.info(f"HTTP camera started for {self.url}")

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._connected = False
        logger.info(f"HTTP camera stopped for {self.url}")

    def _fetch_loop(self):
        import requests as _requests
        while not self._stop_event.is_set():
            try:
                resp = _requests.get(self.url, timeout=5, stream=True)
                content_type = resp.headers.get("Content-Type", "")

                if "multipart" in content_type:
                    self._read_mjpeg_stream(resp)
                else:
                    # Snapshot URL — read single frame with size limit
                    self._fetch_snapshot(resp)
                    self._stop_event.wait(timeout=1)
            except Exception as e:
                logger.warning(f"HTTP camera {self.url} error: {e}")
                self._connected = False
                if not self._stop_event.is_set():
                    self._stop_event.wait(timeout=5)

    def _read_mjpeg_stream(self, resp):
        """Read frames from an MJPEG multipart stream."""
        self._connected = True
        buf = b""
        max_buf = 10 * 1024 * 1024  # 10MB safety limit
        stream_started = time.monotonic()
        for chunk in resp.iter_content(chunk_size=4096):
            if self._stop_event.is_set():
                break
            if time.monotonic() - stream_started > STALE_FRAME_SECONDS and self.is_stale:
                raise RuntimeError("HTTP camera stream stale")
            buf += chunk
            if len(buf) > max_buf:
                logger.warning(f"HTTP camera {self.url}: buffer exceeded {max_buf} bytes, resetting")
                buf = b""
                continue
            # Look for JPEG start/end markers
            while True:
                start = buf.find(b'\xff\xd8')
                if start == -1:
                    buf = buf[-1:]  # keep last byte in case it's partial marker
                    break
                end = buf.find(b'\xff\xd9', start + 2)
                if end == -1:
                    break
                frame = buf[start:end + 2]
                with self._frame_lock:
                    self._latest_frame = frame
                    self._last_frame_at = time.monotonic()
                buf = buf[end + 2:]

    def _fetch_snapshot(self, resp):
        """Read a single snapshot frame from a non-streaming response."""
        try:
            # Read up to 10MB to prevent unbounded memory from an unexpected stream
            data = b""
            for chunk in resp.iter_content(chunk_size=4096):
                data += chunk
                if len(data) > 10 * 1024 * 1024:
                    logger.warning(f"HTTP camera {self.url}: snapshot too large, discarding")
                    resp.close()
                    return
            if resp.status_code == 200 and data:
                with self._frame_lock:
                    self._latest_frame = data
                    self._last_frame_at = time.monotonic()
                self._connected = True
        except Exception as e:
            logger.warning(f"HTTP camera snapshot error: {e}")
            self._connected = False


class CameraManager:
    """Manages camera streams for all printers in the farm."""

    def __init__(self):
        self._cameras = {}
        self._lock = threading.Lock()

    def start_camera(self, name: str, host: str, access_code: str, port: int = 6000):
        """Start a BambuLab camera stream for a printer."""
        with self._lock:
            existing = self._cameras.get(name)
            if existing and existing.is_streaming:
                return
            if existing:
                existing.stop()
            cam = BambuCamera(host=host, access_code=access_code, port=port)
            cam.start()
            self._cameras[name] = cam

    def start_http_camera(self, name: str, url: str):
        """Start an HTTP/MJPEG camera stream (e.g. Klipper webcam)."""
        with self._lock:
            existing = self._cameras.get(name)
            if existing and existing.is_streaming:
                return
            if existing:
                existing.stop()
            cam = HttpCamera(url=url)
            cam.start()
            self._cameras[name] = cam

    def stop_camera(self, name: str):
        """Stop a camera stream."""
        with self._lock:
            cam = self._cameras.pop(name, None)
            if cam:
                cam.stop()

    def stop_all(self):
        """Stop all camera streams."""
        with self._lock:
            for cam in self._cameras.values():
                cam.stop()
            self._cameras.clear()

    def get_frame(self, name: str) -> Optional[bytes]:
        """Get the latest JPEG frame for a printer."""
        cam = self._cameras.get(name)
        if cam:
            return cam.latest_frame
        return None

    def get_frame_after(self, name: str, after: float, timeout: float = 6.0) -> Optional[bytes]:
        """Wait for a JPEG frame captured after the given monotonic timestamp."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            cam = self._cameras.get(name)
            if cam:
                frame, frame_at = cam.latest_frame_with_time
                if frame and frame_at > after:
                    return frame
            time.sleep(0.2)
        return None

    def is_streaming(self, name: str) -> bool:
        cam = self._cameras.get(name)
        return cam.is_streaming if cam else False

    def get_status(self) -> dict:
        """Get simple streaming status for all cameras."""
        return {name: cam.is_streaming for name, cam in self._cameras.items()}

    def get_detailed_status(self) -> dict:
        """Get streaming, stale, and frame age status for all cameras."""
        return {
            name: {
                "streaming": cam.is_streaming,
                "stale": cam.is_stale,
                "last_frame_age": cam.last_frame_age,
            }
            for name, cam in self._cameras.items()
        }
