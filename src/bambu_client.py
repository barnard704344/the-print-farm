"""
BambuLab P1S MQTT Client for LAN Mode.

Connects to a P1S via MQTT over TLS (port 8883) to monitor print status
and send commands (pause, resume, stop, start print).

Protocol reference (Bambu LAN mode):
- Topic: device/{serial}/report  (printer -> client)
- Topic: device/{serial}/request (client -> printer)
- Auth: username "bblp", password is the LAN access code
- TLS on port 8883 with self-signed cert
"""

import ftplib
import io
import json
import logging
import socket
import ssl
import threading
import time
import zipfile
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

import paho.mqtt.client as mqtt


class ImplicitFTPS(ftplib.FTP_TLS):
    """FTP_TLS subclass for implicit FTPS (port 990) where TLS wraps the
    connection from the very first byte, before the FTP welcome banner."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def connect(self, host='', port=0, timeout=-999, source_address=None):
        if host:
            self.host = host
        if port:
            self.port = port
        if timeout != -999:
            self.timeout = timeout
        if source_address is not None:
            self.source_address = source_address

        # Create a plain TCP socket, then wrap it in TLS immediately
        sock = socket.create_connection(
            (self.host, self.port), self.timeout, self.source_address
        )
        self.af = sock.family
        self.sock = self.context.wrap_socket(sock, server_hostname=self.host)
        self.file = self.sock.makefile('r', encoding=self.encoding)
        self.welcome = self.getresp()
        return self.welcome

    def storbinary(self, cmd, fp, blocksize=8192, callback=None, rest=None):
        """Override to handle BambuLab printers not completing TLS shutdown
        on the data channel after transfer."""
        self.voidcmd('TYPE I')
        with self.transfercmd(cmd, rest) as conn:
            while True:
                buf = fp.read(blocksize)
                if not buf:
                    break
                conn.sendall(buf)
                if callback:
                    callback(buf)
            if isinstance(conn, ssl.SSLSocket):
                try:
                    conn.unwrap()
                except (OSError, ssl.SSLError):
                    pass  # Bambu printers don't do clean TLS data shutdown
        return self.voidresp()

logger = logging.getLogger(__name__)


def read_3mf_first_extruder(path: str) -> Optional[int]:
    """Return the first_extruder index from a .3mf file's plate_1.json.

    Returns None if the file cannot be read or the field is absent.
    On a 4-slot AMS machine, first_extruder >= 4 means the external/bypass spool.
    """
    try:
        with zipfile.ZipFile(path, "r") as z:
            data = json.loads(z.read("Metadata/plate_1.json"))
            return int(data.get("first_extruder", 0))
    except Exception:
        return None


class PrintStatus(Enum):
    IDLE = "IDLE"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    PAUSE_FILAMENT = "PAUSE_FILAMENT"
    FINISH = "FINISH"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


@dataclass
class PrintState:
    """Current state of a P1S printer."""
    status: PrintStatus = PrintStatus.UNKNOWN
    gcode_state: str = ""
    mc_percent: int = 0
    mc_remaining_time: int = 0
    layer_num: int = 0
    total_layers: int = 0
    subtask_name: str = ""
    gcode_file: str = ""
    # Temperatures
    bed_temper: float = 0.0
    bed_target_temper: float = 0.0
    nozzle_temper: float = 0.0
    nozzle_target_temper: float = 0.0
    chamber_temper: float = 0.0
    # Fans
    cooling_fan_speed: str = "0"
    heatbreak_fan_speed: str = "0"
    big_fan1_speed: str = "0"
    big_fan2_speed: str = "0"
    # Speed
    spd_lvl: int = 1
    spd_mag: int = 100
    # Nozzle
    nozzle_diameter: str = ""
    nozzle_type: str = ""
    # Network
    wifi_signal: str = ""
    # Light
    chamber_light: bool = False
    # AMS
    ams: dict = field(default_factory=dict)  # full AMS payload
    ams_trays: list = field(default_factory=list)  # flattened tray list
    ams_tray_now: int = 255  # currently active tray (255=none, 254=external)
    ams_humidity: str = ""  # AMS humidity level
    ams_units: list = field(default_factory=list)  # per-unit info (id, humidity, temp)
    vt_tray: dict = field(default_factory=dict)  # external/virtual tray
    has_ams: bool = False
    # Happy Hare MMU (Klipper)
    has_mmu: bool = False
    mmu: dict = field(default_factory=dict)
    # Klipper fans and LEDs (auto-discovered)
    klipper_fans: list = field(default_factory=list)
    klipper_leds: list = field(default_factory=list)
    # Obico failure detection (Klipper)
    obico: dict = field(default_factory=dict)
    # Klipper Adaptive Flow
    adaptive_flow: dict = field(default_factory=dict)
    # Errors
    print_error: int = 0
    hms: list = field(default_factory=list)
    raw_data: dict = field(default_factory=dict)


class BambuClient:
    """
    MQTT client for a single BambuLab P1S in LAN mode.

    Subscribes to printer reports, tracks print state, and provides
    methods to send commands (pause, resume, stop, start print).
    """

    def __init__(self, name: str, host: str, access_code: str, serial: str,
                 port: int = 8883, ftp_port: int = 990, camera_port: int = 6000,
                 ams_serial: str = ""):
        self.name = name
        self.host = host
        self.access_code = access_code
        self.serial = serial
        self.ams_serial = ams_serial
        self.port = port
        self.ftp_port = ftp_port
        self.camera_port = camera_port

        self._client: Optional[mqtt.Client] = None
        self._connected = threading.Event()
        self._state = PrintState()
        self._state_lock = threading.Lock()
        self._on_state_change: Optional[Callable[[str, PrintState], None]] = None
        self._tray_overrides: dict = {}  # {tray_id: {type, color, nozzle_temp_min, nozzle_temp_max}}
        self._last_tray_exist_bits: int = 0
        self._last_tray_is_bbl_bits: int = 0

        self._report_topic = f"device/{self.serial}/report"
        self._request_topic = f"device/{self.serial}/request"

    @property
    def state(self) -> PrintState:
        with self._state_lock:
            return self._state

    def on_state_change(self, callback: Callable[[str, PrintState], None]):
        """Register callback(printer_name, state) for state changes."""
        self._on_state_change = callback

    def connect(self, timeout: float = 10.0) -> bool:
        """Connect to the P1S MQTT broker over TLS."""
        self._reconnect_backoff = 1
        self._stop_event = threading.Event()

        self._client = mqtt.Client(
            client_id=f"print_farm_{self.name}_{int(time.time())}",
            protocol=mqtt.MQTTv311,
        )
        self._client.username_pw_set("bblp", self.access_code)
        self._client.reconnect_delay_set(min_delay=10, max_delay=120)

        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
        self._client.tls_set_context(ssl_ctx)

        self._client.on_connect = self._handle_connect
        self._client.on_message = self._handle_message
        self._client.on_disconnect = self._handle_disconnect

        logger.info(f"[{self.name}] Connecting to {self.host}:{self.port}...")
        try:
            self._client.connect(self.host, self.port, keepalive=60)
            self._client.loop_start()
        except Exception as e:
            logger.error(f"[{self.name}] Failed to connect: {e}")
            return False

        if not self._connected.wait(timeout=timeout):
            logger.error(f"[{self.name}] Connection timed out")
            return False

        logger.info(f"[{self.name}] Connected successfully")
        return True

    def disconnect(self):
        if hasattr(self, '_stop_event'):
            self._stop_event.set()
        if self._client:
            self._client.loop_stop()
            self._client.disconnect()
            self._connected.clear()
            logger.info(f"[{self.name}] Disconnected")

    def is_connected(self) -> bool:
        return self._connected.is_set()

    # ── Commands ──────────────────────────────────────────────

    def pause_print(self) -> bool:
        return self._send_command({
            "print": {"command": "pause", "sequence_id": str(int(time.time()))}
        })

    def resume_print(self) -> bool:
        return self._send_command({
            "print": {"command": "resume", "sequence_id": str(int(time.time()))}
        })

    def stop_print(self) -> bool:
        return self._send_command({
            "print": {"command": "stop", "sequence_id": str(int(time.time()))}
        })

    def start_print(self, filename: str, plate_number: int = 1, use_ams: Optional[bool] = None) -> bool:
        """Start printing a file already uploaded to the printer's root.

        For .3mf files, uses project_file command.
        For raw .gcode, also wraps via project_file with ftp:// URL.

        use_ams: override whether to activate AMS routing. If None, defaults to
        True when the printer has an AMS. Pass False for external/bypass spool jobs.
        """
        subtask = filename.replace(".3mf", "").replace(".gcode", "")
        if use_ams is None:
            use_ams = self._state.has_ams

        cmd = {
            "print": {
                "command": "project_file",
                "sequence_id": "20000",
                "param": f"Metadata/plate_{plate_number}.gcode",
                "subtask_name": subtask,
                "url": f"ftp://{filename}",
                "file": filename,
                "md5": "",
                "bed_type": "auto",
                "timelapse": False,
                "bed_leveling": True,
                "auto_bed_leveling": 1,
                "flow_cali": True,
                "vibration_cali": True,
                "layer_inspect": False,
                "use_ams": use_ams,
                "profile_id": "0",
                "project_id": "0",
                "subtask_id": "0",
                "task_id": "0",
            }
        }

        # If AMS is present, create identity mapping for all trays
        # so gcode M620 Sx commands map directly to the correct AMS tray
        if use_ams:
            num_trays = len(self._state.ams_trays) if self._state.ams_trays else 4
            cmd["print"]["ams_mapping"] = list(range(num_trays))

        return self._send_command(cmd)

    def set_chamber_light(self, on: bool) -> bool:
        mode = "on" if on else "off"
        return self._send_command({
            "system": {
                "sequence_id": str(int(time.time())),
                "command": "ledctrl",
                "led_node": "chamber_light",
                "led_mode": mode,
                "led_on_time": 500,
                "led_off_time": 500,
                "loop_times": 0,
                "interval_time": 0,
            }
        })

    def set_bed_temperature(self, temp: int) -> bool:
        """Set the heated bed target temperature (0 to turn off)."""
        temp = max(0, min(temp, 120))
        return self._send_command({
            "print": {
                "command": "gcode_line",
                "sequence_id": str(int(time.time())),
                "param": f"M140 S{temp}\n",
            }
        })

    def set_nozzle_temperature(self, temp: int) -> bool:
        """Set the nozzle target temperature (0 to turn off)."""
        temp = max(0, min(temp, 300))
        return self._send_command({
            "print": {
                "command": "gcode_line",
                "sequence_id": str(int(time.time())),
                "param": f"M104 S{temp}\n",
            }
        })

    def unload_filament(self) -> bool:
        """Unload filament (retract and cool nozzle)."""
        return self._send_command({
            "print": {
                "command": "ams_change_filament",
                "sequence_id": str(int(time.time())),
                "target": 255,
                "curr_temp": 220,
                "tar_temp": 220,
            }
        })

    def load_filament(self) -> bool:
        """Load/feed filament at current nozzle temp."""
        return self._send_command({
            "print": {
                "command": "gcode_line",
                "sequence_id": str(int(time.time())),
                "param": "M620 S255\nM104 S220\nG28 X\nM109 S220\nG1 E30 F300\nM621 S255\n",
            }
        })

    def ams_load_tray(self, tray_id: int) -> bool:
        """Load filament from a specific AMS tray (0-15, or 254 for external, 255 to unload)."""
        return self._send_command({
            "print": {
                "command": "ams_change_filament",
                "sequence_id": str(int(time.time())),
                "target": tray_id,
                "curr_temp": 220,
                "tar_temp": 220,
            }
        })

    def set_tray_info(self, tray_id: int, tray_type: str, color: str,
                      nozzle_temp_min: int = 190, nozzle_temp_max: int = 230) -> bool:
        """Set filament type/color for an AMS tray. Also stores override locally."""
        ams_id = tray_id // 4
        slot = tray_id % 4
        # Normalize color to 8-char hex RRGGBBAA (strip # prefix)
        color_hex = color.lstrip("#")
        if len(color_hex) == 6:
            color_hex += "FF"

        # Store local override so dashboard shows it even before printer confirms
        self._tray_overrides[tray_id] = {
            "type": tray_type,
            "color": f"#{color_hex[:6]}",
            "color_raw": color_hex,
            "nozzle_temp_min": str(nozzle_temp_min),
            "nozzle_temp_max": str(nozzle_temp_max),
        }

        return self._send_command({
            "print": {
                "command": "ams_filament_setting",
                "sequence_id": str(int(time.time())),
                "ams_id": ams_id,
                "tray_id": slot,
                "tray_info_idx": "",
                "tray_color": color_hex.upper(),
                "nozzle_temp_min": nozzle_temp_min,
                "nozzle_temp_max": nozzle_temp_max,
                "tray_type": tray_type,
            }
        })

    def push_status_request(self) -> bool:
        return self._send_command({
            "pushing": {
                "command": "pushall",
                "sequence_id": str(int(time.time())),
            }
        })

    def refresh_status(self) -> bool:
        """Ask the printer to publish a fresh full status report."""
        return self.push_status_request()

    def upload_file(self, local_path: str, remote_filename: str) -> bool:
        """Upload a G-code file to the printer via implicit FTPS (port 990)."""
        logger.info(f"[{self.name}] Uploading {remote_filename} via FTPS...")
        try:
            ssl_ctx = ssl.create_default_context()
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE

            ftp = ImplicitFTPS(context=ssl_ctx)
            ftp.connect(self.host, self.ftp_port, timeout=30)
            ftp.login("bblp", self.access_code)
            ftp.prot_p()

            with open(local_path, "rb") as f:
                ftp.storbinary(f"STOR /{remote_filename}", f)
            ftp.quit()
            logger.info(f"[{self.name}] Upload complete: {remote_filename}")
            return True
        except Exception as e:
            logger.error(f"[{self.name}] Upload failed: {e}")
            return False

    # ── Internal ──────────────────────────────────────────────

    def _send_command(self, payload: dict) -> bool:
        if not self._client or not self._connected.is_set():
            logger.error(f"[{self.name}] Cannot send command: not connected")
            return False
        try:
            msg = json.dumps(payload)
            result = self._client.publish(self._request_topic, msg)
            if result.rc == mqtt.MQTT_ERR_SUCCESS:
                logger.debug(f"[{self.name}] Sent: {payload}")
                return True
            else:
                logger.error(f"[{self.name}] Publish failed: rc={result.rc}")
                return False
        except Exception as e:
            logger.error(f"[{self.name}] Error sending command: {e}")
            return False

    def _handle_connect(self, client, userdata, flags, rc, properties=None):
        if rc == 0:
            logger.info(f"[{self.name}] MQTT connected, subscribing...")
            client.subscribe(self._report_topic)
            self._connected.set()
            self._reconnect_backoff = 1  # Reset backoff on success
            self.push_status_request()
        else:
            logger.error(f"[{self.name}] MQTT connect failed: rc={rc}")

    def _handle_disconnect(self, client, userdata, flags=None, rc=None, properties=None):
        was_connected = self._connected.is_set()
        self._connected.clear()

        # Only log once, with useful context
        if hasattr(self, '_stop_event') and self._stop_event.is_set():
            logger.info(f"[{self.name}] Disconnected (shutdown)")
            return

        if rc and rc != 0:
            logger.warning(f"[{self.name}] Unexpected disconnect (rc={rc}), will retry...")
        elif was_connected:
            logger.warning(f"[{self.name}] Disconnected by broker — printer may only allow 1 MQTT session. "
                           f"Retry in {self._reconnect_backoff}s")
        else:
            logger.info(f"[{self.name}] Disconnected (rc={rc})")

        # paho auto-reconnect handles this with the delay we set above

    def _handle_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            logger.warning(f"[{self.name}] Bad MQTT message: {e}")
            return

        # Log non-print responses (command acknowledgements, errors)
        if "print" not in payload:
            logger.debug(f"[{self.name}] Non-print MQTT msg: {json.dumps(payload)[:500]}")
            return

        print_data = payload["print"]

        # Log command responses (result field = ack from printer)
        if "command" in print_data and "result" in print_data:
            cmd = print_data.get("command", "?")
            result = print_data.get("result", "?")
            reason = print_data.get("reason", "")
            logger.info(f"[{self.name}] Command response: {cmd} -> {result}"
                        f"{f' reason={reason}' if reason else ''}")

        self._update_state(print_data)

        # AMS data comes in the print payload
        if "ams" in print_data:
            self._update_ams(print_data["ams"])
        if "vt_tray" in print_data:
            with self._state_lock:
                self._state.vt_tray = print_data["vt_tray"]

    def _update_state(self, data: dict):
        prev_status = self._state.status

        with self._state_lock:
            self._state.raw_data = data

            gcode_state = data.get("gcode_state", "")
            if gcode_state:
                self._state.gcode_state = gcode_state
                self._state.status = self._parse_status(gcode_state, data)

            if "mc_percent" in data:
                self._state.mc_percent = data["mc_percent"]
            if "mc_remaining_time" in data:
                self._state.mc_remaining_time = data["mc_remaining_time"]
            if "layer_num" in data:
                self._state.layer_num = data["layer_num"]
            if "total_layer_num" in data:
                self._state.total_layers = data["total_layer_num"]
            if "subtask_name" in data:
                self._state.subtask_name = data["subtask_name"]
            if "gcode_file" in data:
                self._state.gcode_file = data["gcode_file"]
            # Temperatures
            if "bed_temper" in data:
                self._state.bed_temper = data["bed_temper"]
            if "bed_target_temper" in data:
                self._state.bed_target_temper = data["bed_target_temper"]
            if "nozzle_temper" in data:
                self._state.nozzle_temper = data["nozzle_temper"]
            if "nozzle_target_temper" in data:
                self._state.nozzle_target_temper = data["nozzle_target_temper"]
            if "chamber_temper" in data:
                self._state.chamber_temper = data["chamber_temper"]
            # Fans
            if "cooling_fan_speed" in data:
                self._state.cooling_fan_speed = str(data["cooling_fan_speed"])
            if "heatbreak_fan_speed" in data:
                self._state.heatbreak_fan_speed = str(data["heatbreak_fan_speed"])
            if "big_fan1_speed" in data:
                self._state.big_fan1_speed = str(data["big_fan1_speed"])
            if "big_fan2_speed" in data:
                self._state.big_fan2_speed = str(data["big_fan2_speed"])
            # Speed
            if "spd_lvl" in data:
                self._state.spd_lvl = data["spd_lvl"]
            if "spd_mag" in data:
                self._state.spd_mag = data["spd_mag"]
            # Nozzle
            if "nozzle_diameter" in data:
                self._state.nozzle_diameter = str(data["nozzle_diameter"])
            if "nozzle_type" in data:
                self._state.nozzle_type = str(data["nozzle_type"])
            # Network
            if "wifi_signal" in data:
                self._state.wifi_signal = str(data["wifi_signal"])
            # Errors
            if "print_error" in data:
                old_err = self._state.print_error
                new_err = data["print_error"]
                if new_err != old_err:
                    if new_err != 0:
                        logger.warning(f"[{self.name}] print_error: {new_err} (0x{new_err:08X})")
                    else:
                        logger.info(f"[{self.name}] print_error cleared (was {old_err})")
                self._state.print_error = new_err
            if "hms" in data:
                self._state.hms = data["hms"]
            # Light
            lights = data.get("lights_report", [])
            for light in lights:
                if light.get("node") == "chamber_light":
                    self._state.chamber_light = light.get("mode") == "on"

        new_status = self._state.status
        if new_status != prev_status:
            logger.info(f"[{self.name}] Status: {prev_status.value} -> {new_status.value}")
            if self._on_state_change:
                self._on_state_change(self.name, self._state)

    @staticmethod
    def _parse_hex_bits(value: str) -> int:
        """Parse a hex bitmask string (with or without 0x prefix) to int."""
        value = value.strip()
        if value.startswith("0x") or value.startswith("0X"):
            return int(value, 16)
        return int(value, 16)

    def _update_ams(self, ams_data: dict):
        """Parse AMS data from MQTT into structured state.

        Handles both full updates (with inner 'ams' list of units/trays) and
        partial updates (just tray_now, tray_exist_bits, etc.) without wiping
        existing tray data. Follows BamBuddy's merge-not-replace pattern.
        """
        try:
            self._update_ams_inner(ams_data)
        except Exception as e:
            logger.error(f"[{self.name}] Error handling AMS data: {e}")

    def _update_ams_inner(self, ams_data: dict):
        with self._state_lock:
            # Always update tray_now if present
            if "tray_now" in ams_data:
                self._state.ams_tray_now = int(ams_data["tray_now"])

            # Parse bitmask fields (hex strings, may or may not have 0x prefix)
            if ams_data.get("tray_exist_bits"):
                self._last_tray_exist_bits = self._parse_hex_bits(ams_data["tray_exist_bits"])
            if ams_data.get("tray_is_bbl_bits"):
                self._last_tray_is_bbl_bits = self._parse_hex_bits(ams_data["tray_is_bbl_bits"])

            # Check for inner ams list — partial updates (P1S) may only send
            # tray_now/tray_exist_bits without the full unit/tray data
            ams_units = ams_data.get("ams", [])
            if not ams_units:
                # Partial update — refresh active flag on existing trays, keep data
                if self._state.ams_trays:
                    for t in self._state.ams_trays:
                        t["active"] = (t["id"] == self._state.ams_tray_now)
                return

            # Full update — store raw payload and rebuild tray list
            self._state.ams = ams_data
            self._state.has_ams = True

            tray_exist = self._last_tray_exist_bits
            tray_is_bbl = self._last_tray_is_bbl_bits

            trays = []
            units_info = []
            for unit in ams_units:
                unit_id = int(unit.get("id", 0))
                humidity = unit.get("humidity", "")
                self._state.ams_humidity = humidity  # keep last for compat
                temp = unit.get("temp", "")
                units_info.append({
                    "id": unit_id,
                    "humidity": humidity,
                    "temp": temp,
                })
                for tray in unit.get("tray", []):
                    tray_id = int(tray.get("id", 0))
                    global_id = unit_id * 4 + tray_id
                    color_hex = tray.get("tray_color", "00000000")
                    # Check tray_exist_bits to see if filament is physically present
                    tray_exists = bool(tray_exist & (1 << global_id))
                    is_bbl = bool(tray_is_bbl & (1 << global_id))
                    tray_type = tray.get("tray_type", "")
                    # Apply local override if set (for manual filament config)
                    override = self._tray_overrides.get(global_id)
                    display_type = tray_type or (override["type"] if override else ("3rd Party" if tray_exists else ""))
                    display_color = (f"#{color_hex[:6]}" if color_hex != "00000000"
                                     else (override["color"] if override else ""))
                    trays.append({
                        "id": global_id,
                        "unit": unit_id,
                        "slot": tray_id,
                        "type": display_type,
                        "color": display_color,
                        "color_raw": color_hex if color_hex != "00000000" else (override["color_raw"] if override else "00000000"),
                        "remain": tray.get("remain", -1),
                        "nozzle_temp_min": override["nozzle_temp_min"] if override else tray.get("nozzle_temp_min", "0"),
                        "nozzle_temp_max": override["nozzle_temp_max"] if override else tray.get("nozzle_temp_max", "0"),
                        "loaded": tray_exists,
                        "is_bbl": is_bbl,
                        "active": global_id == self._state.ams_tray_now,
                    })
            self._state.ams_trays = trays
            self._state.ams_units = units_info

    def _parse_status(self, gcode_state: str, data: dict) -> PrintStatus:
        state_upper = gcode_state.upper()
        if state_upper == "RUNNING":
            return PrintStatus.RUNNING
        elif state_upper == "PAUSE":
            sub_stage = data.get("mc_print_sub_stage", 0)
            error_code = str(data.get("mc_print_error_code", "0"))
            if sub_stage == 1 or error_code in ("50348", "50349"):
                return PrintStatus.PAUSE_FILAMENT
            return PrintStatus.PAUSED
        elif state_upper == "FINISH":
            return PrintStatus.FINISH
        elif state_upper == "FAILED":
            return PrintStatus.FAILED
        elif state_upper == "IDLE":
            return PrintStatus.IDLE
        else:
            return PrintStatus.UNKNOWN
