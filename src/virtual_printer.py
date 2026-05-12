"""
Virtual Printer Server — emulates a BambuLab P1S on the local network.

One VirtualPrinterServer per physical printer.  Allows OrcaSlicer to:
  - Discover the printer via SSDP (UDP broadcast on port 2021)
  - Connect via MQTT and sync live AMS state / printer status
  - Upload print files via implicit FTPS, which are queued into the farm

The virtual printer mirrors the real printer's live state from the existing
BambuClient connection — it does NOT replace or bypass it.

Network requirements
--------------------
Orca hardcodes port 8883 for MQTT and port 990 for FTP.  With multiple
printers on one host we need each virtual printer bound to a distinct IP.
Set ``virtual_ip`` in the printer config section to enable a virtual printer;
the server adds/removes the IP alias on the NIC automatically.

  printers:
    - name: P1S-ICT-AMS
      virtual_ip: 10.72.28.230        # <- add this line
      virtual_nic: eth0               # <- NIC to add the alias on (default eth0)

In OrcaSlicer, add the printer by IP (virtual_ip), access code and serial
exactly as you would a real printer.  The MQTT sync button will then read
live AMS slot colours/materials from the actual printer.

If virtual_ip is absent, the virtual printer is disabled for that entry.
"""

import hashlib
import json
import logging
import os
import socket
import ssl
import struct
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Standard Bambu LAN ports — must match what Orca expects
MQTT_PORT = 8883
FTP_PORT = 990
SSDP_PORT = 2021
SSDP_ADDR = "239.255.255.250"

# How often to push state to connected Orca clients (seconds)
STATE_PUSH_INTERVAL = 2.0

# SSDP broadcast interval
SSDP_INTERVAL = 30.0


# ---------------------------------------------------------------------------
# Utility: MQTT 3.1.1 packet encode/decode helpers
# ---------------------------------------------------------------------------

def _encode_remaining_length(n: int) -> bytes:
    result = b""
    while True:
        byte = n % 128
        n //= 128
        if n > 0:
            byte |= 0x80
        result += bytes([byte])
        if n == 0:
            break
    return result


def _read_byte(sock) -> Optional[int]:
    b = sock.recv(1)
    return b[0] if b else None


def _read_remaining_length(sock) -> Optional[int]:
    multiplier = 1
    value = 0
    for _ in range(4):
        b = _read_byte(sock)
        if b is None:
            return None
        value += (b & 0x7F) * multiplier
        multiplier *= 128
        if (b & 0x80) == 0:
            return value
    return None


def _read_bytes(sock, n: int) -> Optional[bytes]:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def _read_utf8(data: bytes, offset: int):
    length = (data[offset] << 8) | data[offset + 1]
    s = data[offset + 2: offset + 2 + length].decode("utf-8", errors="replace")
    return s, offset + 2 + length


def _encode_utf8(s: str) -> bytes:
    encoded = s.encode("utf-8")
    return struct.pack("!H", len(encoded)) + encoded


def _mqtt_connack(session_present: bool = False, return_code: int = 0) -> bytes:
    payload = bytes([int(session_present), return_code])
    return bytes([0x20, len(payload)]) + payload


def _mqtt_suback(packet_id: int, return_codes: list) -> bytes:
    header = struct.pack("!H", packet_id) + bytes(return_codes)
    return bytes([0x90]) + _encode_remaining_length(len(header)) + header


def _mqtt_publish(topic: str, payload: bytes, qos: int = 0) -> bytes:
    topic_bytes = _encode_utf8(topic)
    remaining = topic_bytes + payload
    return bytes([0x30 | (qos << 1)]) + _encode_remaining_length(len(remaining)) + remaining


def _mqtt_pingresp() -> bytes:
    return bytes([0xD0, 0x00])


# ---------------------------------------------------------------------------
# TLS cert helper (self-signed, for FTP implicit FTPS)
# ---------------------------------------------------------------------------

def _ensure_cert(cert_dir: str, name: str):
    """Generate a self-signed cert for the virtual printer FTP server if absent."""
    Path(cert_dir).mkdir(parents=True, exist_ok=True)
    safe = name.replace(" ", "_").replace("/", "_")
    certfile = os.path.join(cert_dir, f"{safe}.crt")
    keyfile = os.path.join(cert_dir, f"{safe}.key")
    if not os.path.exists(certfile) or not os.path.exists(keyfile):
        try:
            subprocess.run(
                [
                    "openssl", "req", "-newkey", "rsa:2048", "-nodes",
                    "-keyout", keyfile, "-x509", "-days", "3650",
                    "-out", certfile,
                    "-subj", f"/CN=BambuVirtual-{safe}",
                ],
                capture_output=True, check=True,
            )
            logger.info(f"[{name}] Generated self-signed cert: {certfile}")
        except Exception as e:
            logger.error(f"[{name}] Failed to generate cert: {e}")
            return None, None
    return certfile, keyfile


# ---------------------------------------------------------------------------
# Network interface management — macvlan + DHCP
# ---------------------------------------------------------------------------

def _iface_name(printer_name: str) -> str:
    """Derive a <=15-char interface name from printer name."""
    safe = "".join(c for c in printer_name if c.isalnum() or c in "-_")[:9]
    return f"vbbl-{safe}"


def _stable_mac(printer_name: str) -> str:
    """
    Derive a stable, locally-administered unicast MAC address from the printer
    name.  Same name → same MAC every time, so the DHCP server issues the same
    IP lease across restarts.
    """
    digest = hashlib.sha256(printer_name.encode()).digest()
    b = list(digest[:6])
    b[0] = (b[0] & 0xFE) | 0x02  # clear multicast, set locally-administered
    return ":".join(f"{x:02x}" for x in b)


def _setup_macvlan_dhcp(printer_name: str, nic: str) -> Optional[str]:
    """
    Create a macvlan sub-interface on *nic*, obtain a DHCP lease, and return
    the assigned IP address.  Returns None on failure.
    """
    iface = _iface_name(printer_name)

    # Tear down any leftover interface from a previous run
    subprocess.run(["ip", "link", "del", iface], capture_output=True)

    # Create macvlan with a deterministic MAC so DHCP gives the same IP each restart
    mac = _stable_mac(printer_name)
    r = subprocess.run(
        ["ip", "link", "add", iface, "link", nic, "type", "macvlan", "mode", "bridge"],
        capture_output=True,
    )
    if r.returncode != 0:
        logger.error(f"[{printer_name}] macvlan create failed: {r.stderr.decode().strip()}")
        return None

    # Set stable MAC before bringing the interface up
    subprocess.run(["ip", "link", "set", iface, "address", mac], capture_output=True)
    subprocess.run(["ip", "link", "set", iface, "up"], capture_output=True)

    # Get DHCP lease — use a no-op script so samba/other hooks don't interfere,
    # then apply the IP ourselves via Python (we have CAP_NET_ADMIN).
    lease_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "dhcp")
    os.makedirs(lease_dir, exist_ok=True)
    lease_file = os.path.join(lease_dir, f"{iface}.leases")
    pid_file = os.path.join(lease_dir, f"{iface}.pid")
    # No-op script: dhclient sends DISCOVER/REQUEST/ACK but doesn't configure the
    # interface itself — we read the lease file and apply the IP manually.
    noop_script = os.path.join(lease_dir, "dhclient-noop")
    if not os.path.exists(noop_script):
        with open(noop_script, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(noop_script, 0o755)
    r = subprocess.run(
        ["dhclient", "-1", "-v", "-lf", lease_file, "-pf", pid_file,
         "-sf", noop_script, iface],
        capture_output=True, timeout=30,
    )
    if r.returncode != 0:
        logger.error(f"[{printer_name}] dhclient failed: {r.stderr.decode().strip()}")
        subprocess.run(["ip", "link", "del", iface], capture_output=True)
        return None

    # Parse IP, netmask and router from lease file
    assigned_ip = None
    prefix_len = "24"
    router = None
    try:
        content = open(lease_file).read()
        for line in content.splitlines():
            line = line.strip().rstrip(";")
            if line.startswith("fixed-address"):
                assigned_ip = line.split()[1]
            elif line.startswith("option subnet-mask"):
                mask = line.split()[2]
                # Convert dotted mask to prefix length
                bits = sum(bin(int(x)).count("1") for x in mask.split("."))
                prefix_len = str(bits)
            elif line.startswith("option routers"):
                router = line.split()[2]
    except Exception:
        pass

    if not assigned_ip:
        logger.error(f"[{printer_name}] No IP in DHCP lease file")
        subprocess.run(["ip", "link", "del", iface], capture_output=True)
        return None

    # Apply IP to the interface ourselves (CAP_NET_ADMIN)
    subprocess.run(["ip", "addr", "add", f"{assigned_ip}/{prefix_len}", "dev", iface],
                   capture_output=True)
    if router:
        subprocess.run(["ip", "route", "add", "default", "via", router,
                        "dev", iface, "metric", "200"], capture_output=True)

    logger.info(f"[{printer_name}] macvlan {iface} assigned IP {assigned_ip}/{prefix_len}")
    return assigned_ip


def _teardown_macvlan(printer_name: str):
    iface = _iface_name(printer_name)
    lease_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "dhcp")
    lease_file = os.path.join(lease_dir, f"{iface}.leases")
    pid_file = os.path.join(lease_dir, f"{iface}.pid")
    noop_script = os.path.join(lease_dir, "dhclient-noop")
    subprocess.run(
        ["dhclient", "-r", "-lf", lease_file, "-pf", pid_file, "-sf", noop_script, iface],
        capture_output=True,
    )
    subprocess.run(["ip", "addr", "flush", "dev", iface], capture_output=True)
    subprocess.run(["ip", "link", "del", iface], capture_output=True)
    logger.info(f"[{printer_name}] Removed macvlan interface {iface}")


# ---------------------------------------------------------------------------
# State builder — convert BambuClient PrintState to Bambu MQTT push_status dict
# ---------------------------------------------------------------------------

def _build_state_payload(printer_name: str, serial: str, farm_manager) -> dict:
    """Build a full Bambu push_status payload from the real printer's live state."""
    printer = farm_manager.get_printer(printer_name)
    if printer is None:
        return {}

    s = printer.state

    payload = {
        "command": "push_status",
        "msg": 0,
        "sequence_id": str(int(time.time())),
        # Print state
        "gcode_state": s.gcode_state or "IDLE",
        "mc_percent": s.mc_percent,
        "mc_remaining_time": s.mc_remaining_time,
        "layer_num": s.layer_num,
        "total_layer_num": s.total_layers,
        "subtask_name": s.subtask_name,
        "gcode_file": s.gcode_file,
        # Temperatures
        "bed_temper": s.bed_temper,
        "bed_target_temper": s.bed_target_temper,
        "nozzle_temper": s.nozzle_temper,
        "nozzle_target_temper": s.nozzle_target_temper,
        "chamber_temper": s.chamber_temper,
        # Fans
        "cooling_fan_speed": int(s.cooling_fan_speed) if str(s.cooling_fan_speed).isdigit() else 0,
        "heatbreak_fan_speed": int(s.heatbreak_fan_speed) if str(s.heatbreak_fan_speed).isdigit() else 0,
        "big_fan1_speed": int(s.big_fan1_speed) if str(s.big_fan1_speed).isdigit() else 0,
        "big_fan2_speed": int(s.big_fan2_speed) if str(s.big_fan2_speed).isdigit() else 0,
        # Speed
        "spd_lvl": s.spd_lvl,
        "spd_mag": s.spd_mag,
        # Nozzle
        "nozzle_diameter": float(s.nozzle_diameter) if s.nozzle_diameter else 0.4,
        "nozzle_type": s.nozzle_type or "hardened_steel",
        # Network
        "wifi_signal": s.wifi_signal or "-60dBm",
        # Light
        "lights_report": [{"node": "chamber_light", "mode": "on" if s.chamber_light else "off"}],
        # Errors
        "print_error": s.print_error,
        "hms": s.hms or [],
        # AMS
        "ams_tray_now": s.ams_tray_now,
    }

    # AMS data — the key part Orca needs for slot sync
    if s.has_ams and s.ams_trays:
        # Group trays by unit
        units_map: dict = {}
        for tray in s.ams_trays:
            unit_id = tray.get("unit", 0)
            if unit_id not in units_map:
                units_map[unit_id] = []
            units_map[unit_id].append({
                "id": str(tray.get("id", 0) % 4),
                "remain": tray.get("remain", -1),
                "k": 0.02,
                "n": 1.0,
                "tag_uid": "0000000000000000",
                "tray_id_name": "",
                "tray_info_idx": "",
                "tray_type": tray.get("type", "PLA"),
                "tray_sub_brands": "",
                "tray_color": tray.get("color_raw", "FFFFFFFF"),
                "tray_weight": "1000",
                "tray_diameter": "1.75",
                "tray_temp": str(int(float(tray.get("nozzle_temp_min", 220)) if tray.get("nozzle_temp_min") else 220)),
                "tray_time": "0",
                "bed_temp_type": "0",
                "bed_temp": "0",
                "nozzle_temp_max": str(tray.get("nozzle_temp_max", "240")),
                "nozzle_temp_min": str(tray.get("nozzle_temp_min", "190")),
                "xcam_info": "000000000000000000000000",
                "tray_uuid": "00000000000000000000000000000000",
            })

        ams_list = []
        for unit_id, trays in sorted(units_map.items()):
            ams_info = s.ams_units[unit_id] if unit_id < len(s.ams_units) else {}
            ams_list.append({
                "id": str(unit_id),
                "humidity": str(ams_info.get("humidity", "4")),
                "temp": str(ams_info.get("temp", "0.0")),
                "tray": trays,
            })

        # ams_exist_bits: one bit per AMS unit that is present
        ams_exist_int = 0
        for uid in units_map:
            ams_exist_int |= (1 << uid)
        # tray_exist_bits: one bit per global tray slot that has filament loaded
        tray_exist_int = 0
        tray_is_bbl_int = 0
        for tray in s.ams_trays:
            gid = int(tray.get("id", 0))
            if tray.get("loaded"):
                tray_exist_int |= (1 << gid)
            if tray.get("is_bbl"):
                tray_is_bbl_int |= (1 << gid)

        payload["ams"] = {
            "ams": ams_list,
            "ams_exist_bits": format(ams_exist_int, "x"),
            "tray_exist_bits": format(tray_exist_int, "x"),
            "tray_is_bbl_bits": format(tray_is_bbl_int, "x"),
            "tray_now": str(s.ams_tray_now),
            "tray_pre": "255",
            "tray_tar": "255",
            "version": 3,
            "insert_flag": True,
            "power_on_flag": False,
        }

    # Virtual tray (external/bypass spool)
    if s.vt_tray:
        payload["vt_tray"] = s.vt_tray

    return payload


# ---------------------------------------------------------------------------
# Minimal MQTT broker — handles one Orca client connection at a time
# ---------------------------------------------------------------------------

class _MQTTClientHandler:
    """Handles a single Orca MQTT connection in its own thread."""

    def __init__(self, conn: ssl.SSLSocket, addr, server: "VirtualMQTTBroker"):
        self._conn = conn
        self._addr = addr
        self._server = server
        self._subscribed_topics: set = set()
        self._alive = True

    def run(self):
        logger.debug(f"[{self._server.name}] MQTT client connected from {self._addr}")
        try:
            self._conn.settimeout(120.0)
            while self._alive and not self._server.stopped:
                packet_type_byte = _read_byte(self._conn)
                if packet_type_byte is None:
                    break
                ptype = (packet_type_byte >> 4) & 0x0F
                remaining = _read_remaining_length(self._conn)
                if remaining is None:
                    break
                body = _read_bytes(self._conn, remaining) if remaining > 0 else b""
                if body is None:
                    break
                self._handle_packet(ptype, packet_type_byte, body)
        except (OSError, ssl.SSLError, ConnectionResetError):
            pass
        finally:
            logger.debug(f"[{self._server.name}] MQTT client disconnected {self._addr}")
            try:
                self._conn.close()
            except Exception:
                pass
            self._server.remove_client(self)

    def _handle_packet(self, ptype: int, raw_first: int, body: bytes):
        if ptype == 1:  # CONNECT
            self._handle_connect(body)
        elif ptype == 3:  # PUBLISH
            self._handle_publish(raw_first, body)
        elif ptype == 8:  # SUBSCRIBE
            self._handle_subscribe(body)
        elif ptype == 12:  # PINGREQ
            self._send_raw(_mqtt_pingresp())
        elif ptype == 14:  # DISCONNECT
            self._alive = False

    def _handle_connect(self, body: bytes):
        # Validate access code (password field in CONNECT)
        try:
            offset = 0
            _proto_name, offset = _read_utf8(body, offset)
            _proto_level = body[offset]; offset += 1
            connect_flags = body[offset]; offset += 1
            _keepalive = (body[offset] << 8) | body[offset + 1]; offset += 2
            _client_id, offset = _read_utf8(body, offset)
            username = ""
            password = ""
            if connect_flags & 0x80:  # username flag
                username, offset = _read_utf8(body, offset)
            if connect_flags & 0x40:  # password flag
                pw_len = (body[offset] << 8) | body[offset + 1]; offset += 2
                password = body[offset: offset + pw_len].decode("utf-8", errors="replace")
        except Exception:
            self._send_raw(_mqtt_connack(return_code=4))  # bad data
            return

        if username == "bblp" and password == self._server.access_code:
            self._send_raw(_mqtt_connack(return_code=0))
            self._server.add_client(self)
            logger.info(f"[{self._server.name}] Orca authenticated on virtual MQTT")
        else:
            self._send_raw(_mqtt_connack(return_code=5))  # bad credentials
            logger.warning(f"[{self._server.name}] Virtual MQTT auth failed (user={username!r})")
            self._alive = False

    def _handle_subscribe(self, body: bytes):
        packet_id = (body[0] << 8) | body[1]
        offset = 2
        return_codes = []
        while offset < len(body):
            topic, offset = _read_utf8(body, offset)
            _qos = body[offset]; offset += 1
            self._subscribed_topics.add(topic)
            return_codes.append(0)  # QoS 0 granted
        self._send_raw(_mqtt_suback(packet_id, return_codes))
        # Immediately push full state on subscribe
        self._push_state()

    def _handle_publish(self, raw_first: int, body: bytes):
        qos = (raw_first >> 1) & 0x03
        offset = 0
        topic, offset = _read_utf8(body, offset)
        if qos > 0:
            offset += 2  # skip packet id
        try:
            data = json.loads(body[offset:])
        except Exception:
            return
        # Handle pushall request from Orca
        pushing = data.get("pushing", {})
        if pushing.get("command") == "pushall":
            self._push_state()

    def _push_state(self):
        state = _build_state_payload(
            self._server.printer_name,
            self._server.serial,
            self._server.farm_manager,
        )
        if not state:
            return
        topic = f"device/{self._server.serial}/report"
        msg = json.dumps({"print": state})
        pkt = _mqtt_publish(topic, msg.encode())
        self._send_raw(pkt)

    def _send_raw(self, data: bytes):
        try:
            self._conn.sendall(data)
        except (OSError, ssl.SSLError):
            self._alive = False


class VirtualMQTTBroker:
    """Minimal TLS MQTT 3.1.1 broker bound to virtual_ip:8883."""

    def __init__(self, name: str, printer_name: str, virtual_ip: str,
                 serial: str, access_code: str, certfile: str, keyfile: str,
                 farm_manager):
        self.name = name
        self.printer_name = printer_name
        self.virtual_ip = virtual_ip
        self.serial = serial
        self.access_code = access_code
        self.farm_manager = farm_manager
        self.stopped = False
        self._clients: list = []
        self._clients_lock = threading.Lock()
        self._server_sock: Optional[socket.socket] = None
        self._certfile = certfile
        self._keyfile = keyfile

    def add_client(self, client: _MQTTClientHandler):
        with self._clients_lock:
            self._clients.append(client)

    def remove_client(self, client: _MQTTClientHandler):
        with self._clients_lock:
            self._clients = [c for c in self._clients if c is not client]

    def push_to_all(self):
        """Push current state to all connected Orca clients."""
        with self._clients_lock:
            clients = list(self._clients)
        for client in clients:
            client._push_state()

    def serve_forever(self):
        ssl_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        try:
            ssl_ctx.load_cert_chain(self._certfile, self._keyfile)
        except Exception as e:
            logger.error(f"[{self.name}] MQTT TLS cert load failed: {e}")
            return

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_sock = sock
        try:
            sock.bind((self.virtual_ip, MQTT_PORT))
            sock.listen(5)
            sock.settimeout(1.0)
            logger.info(f"[{self.name}] Virtual MQTT listening on {self.virtual_ip}:{MQTT_PORT}")
            while not self.stopped:
                try:
                    conn, addr = sock.accept()
                    try:
                        tls_conn = ssl_ctx.wrap_socket(conn, server_side=True)
                    except ssl.SSLError as e:
                        logger.debug(f"[{self.name}] TLS handshake failed from {addr}: {e}")
                        conn.close()
                        continue
                    handler = _MQTTClientHandler(tls_conn, addr, self)
                    t = threading.Thread(target=handler.run, daemon=True)
                    t.start()
                except socket.timeout:
                    continue
                except OSError:
                    break
        except OSError as e:
            logger.error(f"[{self.name}] Virtual MQTT bind failed on {self.virtual_ip}:{MQTT_PORT}: {e}")
        finally:
            sock.close()

    def stop(self):
        self.stopped = True
        if self._server_sock:
            try:
                self._server_sock.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Implicit FTPS server — receives print files from Orca
# ---------------------------------------------------------------------------

class _ImplicitFTPSHandler:
    """Handles a single implicit FTPS client (minimal FTP for Orca uploads)."""

    def __init__(self, conn: ssl.SSLSocket, addr, server: "VirtualFTPServer"):
        self._conn = conn
        self._addr = addr
        self._server = server
        self._authenticated = False
        self._username = ""
        self._cwd = "/"
        self._pasv_sock: Optional[socket.socket] = None
        self._pasv_port: int = 0

    def _send(self, line: str):
        try:
            self._conn.sendall((line + "\r\n").encode())
        except (OSError, ssl.SSLError):
            pass

    def _readline(self) -> Optional[str]:
        buf = b""
        try:
            while True:
                b = self._conn.recv(1)
                if not b:
                    return None
                buf += b
                if buf.endswith(b"\n"):
                    return buf.decode("utf-8", errors="replace").strip()
        except (OSError, ssl.SSLError):
            return None

    def run(self):
        self._send("220 BambuVirtual FTP ready")
        try:
            self._conn.settimeout(60.0)
            while True:
                line = self._readline()
                if line is None:
                    break
                parts = line.split(" ", 1)
                cmd = parts[0].upper()
                arg = parts[1] if len(parts) > 1 else ""

                if cmd == "USER":
                    self._username = arg
                    self._send("331 Password required")
                elif cmd == "PASS":
                    if arg == self._server.access_code:
                        self._authenticated = True
                        self._send("230 Logged in")
                    else:
                        self._send("530 Login failed")
                        break
                elif not self._authenticated:
                    self._send("530 Not logged in")
                elif cmd == "PWD":
                    self._send(f'257 "{self._cwd}"')
                elif cmd == "TYPE":
                    self._send("200 OK")
                elif cmd == "OPTS":
                    self._send("200 OK")
                elif cmd == "PBSZ":
                    self._send("200 PBSZ=0")
                elif cmd == "PROT":
                    self._send("200 PROT P")
                elif cmd == "SYST":
                    self._send("215 UNIX Type: L8")
                elif cmd == "FEAT":
                    self._send("211-Features:\r\n UTF8\r\n MLST\r\n211 END")
                elif cmd == "PASV":
                    self._handle_pasv()
                elif cmd == "STOR":
                    self._handle_stor(arg)
                elif cmd == "LIST" or cmd == "MLSD":
                    self._handle_list()
                elif cmd == "CWD":
                    self._cwd = arg or "/"
                    self._send("250 CWD OK")
                elif cmd == "QUIT":
                    self._send("221 Goodbye")
                    break
                elif cmd == "NOOP":
                    self._send("200 OK")
                else:
                    self._send(f"502 {cmd} not implemented")
        except (OSError, ssl.SSLError):
            pass
        finally:
            if self._pasv_sock:
                try:
                    self._pasv_sock.close()
                except Exception:
                    pass
            try:
                self._conn.close()
            except Exception:
                pass

    def _handle_pasv(self):
        """Open a passive data socket and tell client the address."""
        if self._pasv_sock:
            try:
                self._pasv_sock.close()
            except Exception:
                pass

        self._pasv_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._pasv_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._pasv_sock.bind((self._server.virtual_ip, 0))
        self._pasv_sock.listen(1)
        self._pasv_sock.settimeout(30.0)
        _, self._pasv_port = self._pasv_sock.getsockname()

        ip_parts = self._server.virtual_ip.replace(".", ",")
        p1, p2 = self._pasv_port >> 8, self._pasv_port & 0xFF
        self._send(f"227 Entering Passive Mode ({ip_parts},{p1},{p2})")

    def _accept_data_conn(self) -> Optional[ssl.SSLSocket]:
        if not self._pasv_sock:
            return None
        try:
            data_conn, _ = self._pasv_sock.accept()
            # Wrap data channel in TLS (PROT P — protected)
            ssl_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
            ssl_ctx.load_cert_chain(self._server.certfile, self._server.keyfile)
            return ssl_ctx.wrap_socket(data_conn, server_side=True)
        except Exception as e:
            logger.debug(f"[{self._server.name}] Data conn accept failed: {e}")
            return None

    def _handle_stor(self, filename: str):
        self._send("150 Ready to receive data")
        data_conn = self._accept_data_conn()
        if not data_conn:
            self._send("425 Can't open data connection")
            return
        try:
            buf = b""
            while True:
                chunk = data_conn.recv(65536)
                if not chunk:
                    break
                buf += chunk
            try:
                data_conn.unwrap()
            except Exception:
                pass
        except (OSError, ssl.SSLError) as e:
            self._send(f"426 Connection closed: {e}")
            return
        finally:
            try:
                data_conn.close()
            except Exception:
                pass

        if not buf:
            self._send("550 Empty file")
            return

        # Save file and queue the job
        basename = os.path.basename(filename) or filename
        try:
            self._server.receive_file(basename, buf)
            self._send("226 Transfer complete")
        except Exception as e:
            logger.error(f"[{self._server.name}] FTP STOR failed: {e}")
            self._send(f"550 {e}")

    def _handle_list(self):
        self._send("150 Here comes the directory listing")
        data_conn = self._accept_data_conn()
        if data_conn:
            try:
                data_conn.sendall(b"")
            except Exception:
                pass
            finally:
                try:
                    data_conn.unwrap()
                except Exception:
                    pass
                try:
                    data_conn.close()
                except Exception:
                    pass
        self._send("226 Directory listing OK")


class VirtualFTPServer:
    """Implicit FTPS server on virtual_ip:990 — accepts uploads from Orca."""

    def __init__(self, name: str, printer_name: str, virtual_ip: str,
                 access_code: str, certfile: str, keyfile: str,
                 uploads_dir: str, job_queue):
        self.name = name
        self.printer_name = printer_name
        self.virtual_ip = virtual_ip
        self.access_code = access_code
        self.certfile = certfile
        self.keyfile = keyfile
        self.uploads_dir = uploads_dir
        self.job_queue = job_queue
        self.stopped = False
        self._ssl_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        self._server_sock: Optional[socket.socket] = None

    def receive_file(self, filename: str, data: bytes):
        """Save uploaded file and add it to the job queue."""
        import hashlib
        h = hashlib.md5(data).hexdigest()
        dest = os.path.join(self.uploads_dir, f"{h}_{filename}")
        with open(dest, "wb") as f:
            f.write(data)
        logger.info(f"[{self.name}] Received upload: {filename} ({len(data)} bytes) → {dest}")
        # Queue the job (unassigned — staff assigns via web UI)
        try:
            self.job_queue.add_job(
                file_path=dest,
                original_filename=filename,
                uploaded_by=f"orca_virtual/{self.printer_name}",
            )
            logger.info(f"[{self.name}] Queued job from Orca upload: {filename}")
        except Exception as e:
            logger.error(f"[{self.name}] Failed to queue job: {e}")

    def serve_forever(self):
        try:
            self._ssl_ctx.load_cert_chain(self.certfile, self.keyfile)
        except Exception as e:
            logger.error(f"[{self.name}] FTP TLS cert load failed: {e}")
            return

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_sock = sock
        try:
            sock.bind((self.virtual_ip, FTP_PORT))
            sock.listen(5)
            sock.settimeout(1.0)
            logger.info(f"[{self.name}] Virtual FTP listening on {self.virtual_ip}:{FTP_PORT}")
            while not self.stopped:
                try:
                    conn, addr = sock.accept()
                    # Wrap immediately (implicit TLS)
                    try:
                        tls_conn = self._ssl_ctx.wrap_socket(conn, server_side=True)
                    except ssl.SSLError as e:
                        logger.debug(f"[{self.name}] FTP TLS handshake failed: {e}")
                        conn.close()
                        continue
                    handler = _ImplicitFTPSHandler(tls_conn, addr, self)
                    t = threading.Thread(target=handler.run, daemon=True)
                    t.start()
                except socket.timeout:
                    continue
                except OSError:
                    break
        except OSError as e:
            logger.error(f"[{self.name}] Virtual FTP bind failed on {self.virtual_ip}:{FTP_PORT}: {e}")
        finally:
            sock.close()

    def stop(self):
        self.stopped = True
        if self._server_sock:
            try:
                self._server_sock.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# SSDP broadcaster — makes Orca auto-discover the virtual printer
# ---------------------------------------------------------------------------

def _ssdp_build_payload(name: str, serial: str, virtual_ip: str, model: str) -> bytes:
    return json.dumps({
        "dev_name": name,
        "dev_id": serial,
        "dev_ip": virtual_ip,
        "dev_type": model,
        "dev_signal": "-50dBm",
        "dev_connection_type": "lan",
    }).encode()


def _ssdp_broadcast_loop(name: str, virtual_ip: str, serial: str,
                          model: str, stopped_event: threading.Event):
    """Broadcast Bambu-style SSDP discovery packets so Orca sees the virtual printer."""
    payload = _ssdp_build_payload(name, serial, virtual_ip, model)

    while not stopped_event.wait(SSDP_INTERVAL):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                                socket.inet_aton(virtual_ip))
                sock.sendto(payload, (SSDP_ADDR, SSDP_PORT))
        except Exception as e:
            logger.debug(f"[{name}] SSDP broadcast error: {e}")

    # Final broadcast on stop to let Orca refresh
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.sendto(payload, (SSDP_ADDR, SSDP_PORT))
    except Exception:
        pass


def _ssdp_msearch_listener(name: str, virtual_ip: str, serial: str,
                            model: str, stopped_event: threading.Event):
    """
    Listen for Bambu-style SSDP M-SEARCH queries on the multicast group and
    reply unicast so OrcaSlicer's auto-connect flow can find the printer
    immediately without waiting for the next passive broadcast.
    """
    payload = _ssdp_build_payload(name, serial, virtual_ip, model)
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        sock.bind(("", SSDP_PORT))
        mreq = socket.inet_aton(SSDP_ADDR) + socket.inet_aton(virtual_ip)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        sock.settimeout(1.0)
    except Exception as e:
        logger.warning(f"[{name}] SSDP listener setup failed: {e}")
        return

    while not stopped_event.is_set():
        try:
            data, addr = sock.recvfrom(4096)
        except socket.timeout:
            continue
        except Exception as e:
            logger.debug(f"[{name}] SSDP listener recv error: {e}")
            continue
        try:
            msg = data.decode(errors="ignore")
            if "M-SEARCH" in msg:
                # Reply unicast to the requester
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as reply_sock:
                    reply_sock.sendto(payload, addr)
                logger.debug(f"[{name}] Replied to SSDP M-SEARCH from {addr}")
        except Exception as e:
            logger.debug(f"[{name}] SSDP M-SEARCH handle error: {e}")

    try:
        sock.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# VirtualPrinterServer — one per physical printer
# ---------------------------------------------------------------------------

class VirtualPrinterServer:
    """
    Emulates one BambuLab P1S on the LAN.

    Creates a macvlan sub-interface, obtains a DHCP lease, then binds
    MQTT (8883) and FTP (990) on that IP.  The real printer connection is
    untouched — state is read from it.
    """

    def __init__(self, printer_cfg: dict, farm_manager, job_queue,
                 uploads_dir: str, cert_dir: str = "config/certs"):
        self.printer_name = printer_cfg["name"]
        self.virtual_nic = printer_cfg.get("virtual_nic", "eth0")
        self.serial = printer_cfg.get("serial", "")
        self.access_code = printer_cfg.get("access_code", "")
        self.model = printer_cfg.get("model", "3DPrinter-P1S-v1")
        self.virtual_ip: Optional[str] = None  # assigned after DHCP
        self._farm = farm_manager
        self._jq = job_queue
        self._uploads = uploads_dir
        self._cert_dir = cert_dir
        self._stopped = threading.Event()
        self._threads: list = []
        self._mqtt: Optional[VirtualMQTTBroker] = None
        self._ftp: Optional[VirtualFTPServer] = None

    def start(self):
        certfile, keyfile = _ensure_cert(self._cert_dir, self.printer_name)
        if not certfile:
            logger.error(f"[{self.printer_name}] Cannot start virtual printer: no TLS cert")
            return

        # Create macvlan interface and get DHCP IP
        ip = _setup_macvlan_dhcp(self.printer_name, self.virtual_nic)
        if not ip:
            logger.error(f"[{self.printer_name}] Cannot start virtual printer: DHCP failed")
            return
        self.virtual_ip = ip

        # MQTT broker
        self._mqtt = VirtualMQTTBroker(
            name=self.printer_name,
            printer_name=self.printer_name,
            virtual_ip=self.virtual_ip,
            serial=self.serial,
            access_code=self.access_code,
            certfile=certfile,
            keyfile=keyfile,
            farm_manager=self._farm,
        )
        t_mqtt = threading.Thread(
            target=self._mqtt.serve_forever,
            name=f"vp-mqtt-{self.printer_name}",
            daemon=True,
        )
        self._threads.append(t_mqtt)
        t_mqtt.start()

        # FTP server
        self._ftp = VirtualFTPServer(
            name=self.printer_name,
            printer_name=self.printer_name,
            virtual_ip=self.virtual_ip,
            access_code=self.access_code,
            certfile=certfile,
            keyfile=keyfile,
            uploads_dir=self._uploads,
            job_queue=self._jq,
        )
        t_ftp = threading.Thread(
            target=self._ftp.serve_forever,
            name=f"vp-ftp-{self.printer_name}",
            daemon=True,
        )
        self._threads.append(t_ftp)
        t_ftp.start()

        # SSDP broadcaster
        t_ssdp = threading.Thread(
            target=_ssdp_broadcast_loop,
            args=(self.printer_name, self.virtual_ip, self.serial,
                  self.model, self._stopped),
            name=f"vp-ssdp-{self.printer_name}",
            daemon=True,
        )
        self._threads.append(t_ssdp)
        t_ssdp.start()

        # SSDP M-SEARCH responder (handles OrcaSlicer auto-connect queries)
        t_msearch = threading.Thread(
            target=_ssdp_msearch_listener,
            args=(self.printer_name, self.virtual_ip, self.serial,
                  self.model, self._stopped),
            name=f"vp-ssdp-rx-{self.printer_name}",
            daemon=True,
        )
        self._threads.append(t_msearch)
        t_msearch.start()

        # State push loop
        t_push = threading.Thread(
            target=self._state_push_loop,
            name=f"vp-push-{self.printer_name}",
            daemon=True,
        )
        self._threads.append(t_push)
        t_push.start()

        logger.info(
            f"[{self.printer_name}] Virtual printer started — "
            f"Orca: add by IP {self.virtual_ip}"
        )

    def _state_push_loop(self):
        while not self._stopped.wait(STATE_PUSH_INTERVAL):
            if self._mqtt:
                self._mqtt.push_to_all()

    def stop(self):
        self._stopped.set()
        if self._mqtt:
            self._mqtt.stop()
        if self._ftp:
            self._ftp.stop()
        _teardown_macvlan(self.printer_name)
        logger.info(f"[{self.printer_name}] Virtual printer stopped")


# ---------------------------------------------------------------------------
# VirtualPrinterManager — started by main.py
# ---------------------------------------------------------------------------

class VirtualPrinterManager:
    """Starts a VirtualPrinterServer for every BambuLab printer."""

    def __init__(self):
        self._servers: list = []

    def start_all(self, printer_configs: list, farm_manager, job_queue,
                  uploads_dir: str, cert_dir: str = "config/certs"):
        for cfg in printer_configs:
            if cfg.get("type", "bambulab").lower() != "bambulab":
                continue
            if cfg.get("virtual_printer") is False:
                continue  # Opt-out via virtual_printer: false
            srv = VirtualPrinterServer(cfg, farm_manager, job_queue,
                                       uploads_dir, cert_dir)
            srv.start()
            self._servers.append(srv)
        if not self._servers:
            logger.info("Virtual printers: no BambuLab printers configured")

    def stop_all(self):
        for srv in self._servers:
            srv.stop()
        self._servers.clear()
