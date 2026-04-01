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


class BambuCamera:
    """Receives JPEG frames from a P1S camera over TLS."""

    def __init__(self, host: str, access_code: str, port: int = 6000):
        self.host = host
        self.access_code = access_code
        self.port = port

        self._latest_frame: Optional[bytes] = None
        self._frame_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._connected = False

    @property
    def latest_frame(self) -> Optional[bytes]:
        with self._frame_lock:
            return self._latest_frame

    @property
    def is_streaming(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and self._connected

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

            img = None
            payload_size = 0

            while not self._stop_event.is_set():
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


class CameraManager:
    """Manages camera streams for all printers in the farm."""

    def __init__(self):
        self._cameras = {}
        self._lock = threading.Lock()

    def start_camera(self, name: str, host: str, access_code: str, port: int = 6000):
        """Start a camera stream for a printer."""
        with self._lock:
            if name in self._cameras:
                self._cameras[name].stop()
            cam = BambuCamera(host=host, access_code=access_code, port=port)
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

    def is_streaming(self, name: str) -> bool:
        cam = self._cameras.get(name)
        return cam.is_streaming if cam else False

    def get_status(self) -> dict:
        """Get streaming status for all cameras."""
        return {name: cam.is_streaming for name, cam in self._cameras.items()}
