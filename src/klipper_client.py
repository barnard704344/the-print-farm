"""
Klipper/Moonraker Client for the Print Farm.

Connects to a Klipper printer via the Moonraker HTTP API to monitor
print status and send commands (pause, resume, stop, start print, upload).

Moonraker API reference:
- GET  /printer/info                         — Printer info & state
- GET  /printer/objects/query?...            — Query printer objects
- POST /printer/print/start?filename=...     — Start a print
- POST /printer/print/pause                  — Pause current print
- POST /printer/print/resume                 — Resume paused print
- POST /printer/print/cancel                 — Cancel/stop current print
- POST /server/files/upload                  — Upload file (multipart)
- POST /printer/gcode/script?script=...      — Send raw G-code
- GET  /server/files/list                    — List uploaded files
"""

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import requests

from .bambu_client import PrintState, PrintStatus

logger = logging.getLogger(__name__)

# Happy Hare MMU object names
_MMU_OBJECTS = {
    "mmu": None,
    "mmu_machine": None,
    "mmu_encoder mmu_encoder": None,
}

# Moonraker object prefixes for fans and LEDs
_FAN_PREFIXES = ("fan_generic", "heater_fan", "controller_fan")
_LED_PREFIXES = ("neopixel", "led", "dotstar")
_OUTPUT_PIN_PREFIX = "output_pin"

# Default polling interval for status updates (seconds)
POLL_INTERVAL = 2.0


def _hex_color(val: str) -> str:
    """Normalise a bare hex color (e.g. 'ff0000') to '#ff0000'."""
    if not val:
        return ""
    val = val.strip().lstrip("#")
    if len(val) == 6:
        return "#" + val
    return ""


def _number(value, default=0.0) -> float:
    """Return a float for optional Moonraker numeric fields."""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _is_extruder_object(name: str) -> bool:
    suffix = name[len("extruder"):]
    return name == "extruder" or (name.startswith("extruder") and suffix.isdigit())


def _extruder_sort_key(name: str) -> int:
    if name == "extruder":
        return 0
    suffix = name[len("extruder"):]
    return int(suffix) if suffix.isdigit() else 999


class KlipperClient:
    """
    HTTP client for a single Klipper printer via Moonraker API.

    Provides the same public interface as BambuClient so FarmManager
    can treat both printer types uniformly.
    """

    def __init__(self, name: str, host: str, port: int = 7125,
                 api_key: str = "", camera_url: str = "",
                 obico_config: dict = None):
        self.name = name
        self.host = host
        self.port = port
        self.api_key = api_key
        self.camera_url = camera_url
        self.printer_type = "klipper"

        # Not used by Klipper but kept for interface compatibility
        self.ams_serial = ""
        self.camera_port = 0
        self.ftp_port = 0

        self._base_url = f"http://{host}:{port}"
        self._session = requests.Session()
        if api_key:
            self._session.headers["X-Api-Key"] = api_key

        self._connected = threading.Event()
        self._state = PrintState()
        self._state_lock = threading.Lock()
        self._on_state_change: Optional[Callable[[str, PrintState], None]] = None
        self._stop_event = threading.Event()
        self._poll_thread: Optional[threading.Thread] = None

        # Detected capabilities (set during connect)
        self._has_mmu = False
        self._extruder_objects = ["extruder"]
        self._fan_objects = []   # e.g. ["fan_generic exhaust_fan", "heater_fan hotend_fan"]
        self._led_objects = []   # e.g. ["neopixel chamber_light", "led sb_leds"]
        self._output_pins = []   # e.g. ["output_pin caselight"]

        # Layer count cache: {printer_filename: int}
        # Stores total layer count from Moonraker file metadata as a fallback
        # when the slicer does not emit SET_PRINT_STATS_INFO commands.
        self._layer_cache: dict = {}

        # Obico integration
        self._obico = None
        if obico_config and obico_config.get("server"):
            from .obico_client import ObicoClient
            self._obico = ObicoClient(
                server_url=obico_config["server"],
                username=obico_config.get("username", ""),
                password=obico_config.get("password", ""),
                printer_id=int(obico_config.get("printer_id", 0)),
            )

    @property
    def state(self) -> PrintState:
        with self._state_lock:
            return self._state

    def on_state_change(self, callback: Callable[[str, PrintState], None]):
        self._on_state_change = callback

    def connect(self, timeout: float = 10.0) -> bool:
        """Test connection to Moonraker and start status polling."""
        logger.info(f"[{self.name}] Connecting to Moonraker at {self._base_url}...")
        try:
            resp = self._session.get(
                f"{self._base_url}/printer/info",
                timeout=timeout,
            )
            if resp.status_code != 200:
                logger.error(f"[{self.name}] Moonraker returned {resp.status_code}")
                return False

            info = resp.json().get("result", {})
            klipper_state = info.get("state", "")
            logger.info(f"[{self.name}] Klipper state: {klipper_state}, "
                        f"version: {info.get('software_version', '?')}")

            self._connected.set()
            self._stop_event.clear()
            self._has_mmu = False
            self._extruder_objects = ["extruder"]
            self._fan_objects = []
            self._led_objects = []
            self._output_pins = []

            # Detect Happy Hare MMU
            try:
                obj_resp = self._session.get(
                    f"{self._base_url}/printer/objects/list", timeout=5)
                if obj_resp.status_code == 200:
                    objects = obj_resp.json().get("result", {}).get("objects", [])
                    if "mmu" in objects:
                        self._has_mmu = True
                        logger.info(f"[{self.name}] Happy Hare MMU detected")

                    self._extruder_objects = sorted(
                        [obj_name for obj_name in objects if _is_extruder_object(obj_name)],
                        key=_extruder_sort_key,
                    ) or ["extruder"]

                    # Auto-discover fan objects
                    for obj_name in objects:
                        for prefix in _FAN_PREFIXES:
                            if obj_name == prefix or obj_name.startswith(prefix + " "):
                                self._fan_objects.append(obj_name)
                        for prefix in _LED_PREFIXES:
                            if obj_name == prefix or obj_name.startswith(prefix + " "):
                                self._led_objects.append(obj_name)
                        if obj_name.startswith(_OUTPUT_PIN_PREFIX + " "):
                            self._output_pins.append(obj_name)

                    if len(self._extruder_objects) > 1:
                        logger.info(f"[{self.name}] Extruders detected: {self._extruder_objects}")
                    if self._fan_objects:
                        logger.info(f"[{self.name}] Fans detected: {self._fan_objects}")
                    if self._led_objects:
                        logger.info(f"[{self.name}] LEDs detected: {self._led_objects}")
                    if self._output_pins:
                        logger.info(f"[{self.name}] Output pins detected: {self._output_pins}")
            except Exception:
                pass

            # Detect Klipper Adaptive Flow (dashboard on port 7127)
            self._adaptive_flow_url = None
            try:
                af_url = f"http://{self.host}:7127"
                af_resp = self._session.get(af_url, timeout=3)
                if af_resp.status_code == 200:
                    self._adaptive_flow_url = af_url
                    logger.info(f"[{self.name}] Klipper Adaptive Flow detected at {af_url}")
            except Exception:
                pass

            # Start background polling thread
            if not self._poll_thread or not self._poll_thread.is_alive():
                self._poll_thread = threading.Thread(
                    target=self._poll_loop, daemon=True, name=f"klipper-poll-{self.name}"
                )
                self._poll_thread.start()

            # Do an initial status fetch
            self._fetch_status()

            logger.info(f"[{self.name}] Connected successfully")
            return True
        except requests.ConnectionError as e:
            logger.error(f"[{self.name}] Connection failed: {e}")
            return False
        except Exception as e:
            logger.error(f"[{self.name}] Connection error: {e}")
            return False

    def disconnect(self):
        self._stop_event.set()
        self._connected.clear()
        if self._poll_thread and self._poll_thread.is_alive():
            self._poll_thread.join(timeout=5)
        logger.info(f"[{self.name}] Disconnected")

    def is_connected(self) -> bool:
        return self._connected.is_set()

    def refresh_status(self) -> bool:
        """Fetch printer status immediately and update the cached state."""
        try:
            self._fetch_status()
            self._connected.set()
            return True
        except Exception as e:
            logger.error(f"[{self.name}] Status refresh failed: {e}")
            self._handle_connection_loss()
            return False

    # ── Commands ──────────────────────────────────────────────

    def pause_print(self) -> bool:
        return self._post("/printer/print/pause")

    def resume_print(self) -> bool:
        return self._post("/printer/print/resume")

    def stop_print(self) -> bool:
        return self._post("/printer/print/cancel")

    def start_print(self, filename: str, **kwargs) -> bool:
        """Start printing a file already uploaded to Klipper's gcodes directory."""
        # Moonraker expects the filename relative to the gcodes root
        return self._post(f"/printer/print/start?filename={requests.utils.quote(filename)}")

    def upload_file(self, local_path: str, remote_filename: str) -> bool:
        """Upload a G-code file to Klipper via Moonraker's file upload API."""
        logger.info(f"[{self.name}] Uploading {remote_filename} via Moonraker...")
        try:
            with open(local_path, "rb") as f:
                resp = self._session.post(
                    f"{self._base_url}/server/files/upload",
                    files={"file": (remote_filename, f, "application/octet-stream")},
                    data={"root": "gcodes"},
                    timeout=120,
                )
            if resp.status_code == 201:
                logger.info(f"[{self.name}] Upload complete: {remote_filename}")
                return True
            else:
                logger.error(f"[{self.name}] Upload failed: {resp.status_code} {resp.text[:200]}")
                return False
        except Exception as e:
            logger.error(f"[{self.name}] Upload error: {e}")
            return False

    def set_bed_temperature(self, temp: int) -> bool:
        temp = max(0, min(temp, 120))
        return self._gcode(f"M140 S{temp}")

    def set_nozzle_temperature(self, temp: int, heater: str = "") -> bool:
        temp = max(0, min(temp, 300))
        heater = (heater or "extruder").strip()
        allowed = set(getattr(self, "_extruder_objects", ["extruder"]))
        if heater not in allowed:
            logger.warning(f"[{self.name}] Cannot set unknown heater: {heater}")
            return False
        return self._gcode(f"SET_HEATER_TEMPERATURE HEATER={heater} TARGET={temp}")

    def set_chamber_light(self, on: bool) -> bool:
        """Toggle chamber light. Tries discovered LEDs/pins first, falls back to macro."""
        if self._led_objects:
            # Toggle first neopixel/led/dotstar
            obj = self._led_objects[0]
            if on:
                return self._gcode(f"SET_LED LED={obj.split(' ', 1)[1]} RED=1 GREEN=1 BLUE=1")
            else:
                return self._gcode(f"SET_LED LED={obj.split(' ', 1)[1]} RED=0 GREEN=0 BLUE=0")
        if self._output_pins:
            pin = self._output_pins[0].split(" ", 1)[1]
            return self._gcode(f"SET_PIN PIN={pin} VALUE={'1' if on else '0'}")
        macro = "CHAMBER_LIGHT_ON" if on else "CHAMBER_LIGHT_OFF"
        return self._gcode(macro)

    def set_led(self, led_object: str, on: bool) -> bool:
        """Toggle a specific LED or output pin by its Klipper object name."""
        parts = led_object.split(" ", 1)
        obj_type = parts[0]
        obj_name = parts[1] if len(parts) > 1 else parts[0]
        if obj_type == "output_pin":
            return self._gcode(f"SET_PIN PIN={obj_name} VALUE={'1' if on else '0'}")
        # neopixel, led, dotstar all use SET_LED
        if on:
            return self._gcode(f"SET_LED LED={obj_name} RED=1 GREEN=1 BLUE=1")
        else:
            return self._gcode(f"SET_LED LED={obj_name} RED=0 GREEN=0 BLUE=0")

    def set_fan_speed(self, fan_object: str, speed: float) -> bool:
        """Set speed of a fan_generic object. speed is 0.0-1.0."""
        parts = fan_object.split(" ", 1)
        if parts[0] != "fan_generic" or len(parts) < 2:
            logger.warning(f"[{self.name}] Cannot control fan: {fan_object}")
            return False
        fan_name = parts[1]
        speed = max(0.0, min(1.0, speed))
        return self._gcode(f"SET_FAN_SPEED FAN={fan_name} SPEED={speed:.2f}")

    def send_gcode(self, gcode: str) -> bool:
        """Send raw G-code to the printer."""
        return self._gcode(gcode)

    def push_status_request(self) -> bool:
        """Force a status refresh (fetch immediately)."""
        try:
            self._fetch_status()
            return True
        except Exception:
            return False

    def home_axes(self, axes: str = "XYZ") -> bool:
        """Home specified axes."""
        return self._gcode(f"G28 {' '.join(axes)}")

    def emergency_stop(self) -> bool:
        """Send emergency stop to Klipper."""
        return self._post("/printer/emergency_stop")

    # ── Internal ──────────────────────────────────────────────

    def _post(self, path: str) -> bool:
        """Send a POST request to Moonraker."""
        if not self._connected.is_set():
            logger.error(f"[{self.name}] Cannot send command: not connected")
            return False
        try:
            resp = self._session.post(f"{self._base_url}{path}", timeout=10)
            if resp.status_code == 200:
                logger.debug(f"[{self.name}] POST {path} OK")
                return True
            else:
                logger.error(f"[{self.name}] POST {path} failed: {resp.status_code}")
                return False
        except Exception as e:
            logger.error(f"[{self.name}] POST {path} error: {e}")
            self._handle_connection_loss()
            return False

    def _gcode(self, script: str) -> bool:
        """Send a G-code script via Moonraker."""
        if not self._connected.is_set():
            return False
        try:
            resp = self._session.post(
                f"{self._base_url}/printer/gcode/script",
                params={"script": script},
                timeout=10,
            )
            if resp.status_code == 200:
                logger.debug(f"[{self.name}] Gcode: {script[:50]}")
                return True
            else:
                logger.error(f"[{self.name}] Gcode failed ({resp.status_code}): {script[:50]}")
                return False
        except Exception as e:
            logger.error(f"[{self.name}] Gcode error: {e}")
            self._handle_connection_loss()
            return False

    def _handle_connection_loss(self):
        """Mark as disconnected when we can't reach Moonraker."""
        if self._connected.is_set():
            logger.warning(f"[{self.name}] Lost connection to Moonraker")
            self._connected.clear()

    def _poll_loop(self):
        """Background thread: poll Moonraker for status updates."""
        consecutive_failures = 0
        while not self._stop_event.is_set():
            try:
                self._fetch_status()
                if not self._connected.is_set():
                    self._connected.set()
                    logger.info(f"[{self.name}] Reconnected to Moonraker")
                consecutive_failures = 0
            except Exception as e:
                consecutive_failures += 1
                if consecutive_failures == 3:
                    logger.warning(f"[{self.name}] Lost connection: {e}")
                    self._handle_connection_loss()

            self._stop_event.wait(POLL_INTERVAL)

    def _fetch_status(self):
        """Query Moonraker for current printer state and update self._state."""
        params = {
            "heater_bed": "temperature,target",
            "print_stats": "state,filename,total_duration,print_duration,filament_used,message,info",
            "display_status": "progress,message",
            "virtual_sdcard": "progress,file_position,file_path",
            "fan": "speed",
            "gcode_move": "speed_factor",
        }

        for extruder_obj in getattr(self, "_extruder_objects", ["extruder"]):
            params[extruder_obj] = "temperature,target,pressure_advance"

        # Include Happy Hare MMU objects if detected
        if self._has_mmu:
            for obj in _MMU_OBJECTS:
                params[obj] = ""

        # Include discovered fan objects
        for fan_obj in self._fan_objects:
            params[fan_obj] = "speed"

        # Include discovered LED objects
        for led_obj in self._led_objects:
            params[led_obj] = "color_data"

        # Include discovered output pins (often used for lights)
        for pin_obj in self._output_pins:
            params[pin_obj] = "value"

        resp = self._session.get(
            f"{self._base_url}/printer/objects/query",
            params=params,
            timeout=5,
        )

        if resp.status_code != 200:
            raise ConnectionError(f"Status query returned {resp.status_code}")

        result = resp.json().get("result", {}).get("status", {})
        prev_status = self._state.status

        # Determine layer info before acquiring the lock so the metadata
        # HTTP request (fallback) does not hold the lock.
        ps_pre = result.get("print_stats", {})
        ps_info_pre = ps_pre.get("info") or {}
        _info_layer_num = ps_info_pre.get("current_layer") or 0
        _info_total_layers = ps_info_pre.get("total_layer") or 0

        # Fallback: if the slicer did not emit SET_PRINT_STATS_INFO, retrieve
        # the layer count from Moonraker's file-metadata analysis instead.
        _ps_filename = ps_pre.get("filename", "")
        _meta_total_layers = 0
        if _info_total_layers == 0 and _ps_filename:
            _meta_total_layers = self._get_cached_layer_count(_ps_filename)

        with self._state_lock:
            # Print stats
            ps = result.get("print_stats", {})
            klipper_state = ps.get("state", "standby")
            self._state.status = self._map_status(klipper_state)
            self._state.gcode_state = klipper_state
            raw_filename = ps.get("filename", "")
            # Strip UUID prefix (32 hex chars + underscore) added during upload
            if len(raw_filename) > 33 and raw_filename[32] == "_" and all(c in "0123456789abcdef" for c in raw_filename[:32]):
                raw_filename = raw_filename[33:]
            self._state.subtask_name = raw_filename
            self._state.gcode_file = raw_filename

            # Layer count: prefer SET_PRINT_STATS_INFO values; fall back to
            # file-metadata layer_count when the slicer doesn't emit that command.
            total_layers = _info_total_layers or _meta_total_layers
            layer_num = _info_layer_num

            # Progress
            ds = result.get("display_status", {})
            vs = result.get("virtual_sdcard", {})
            progress = _number(ds.get("progress", vs.get("progress", 0)), 0.0)
            self._state.mc_percent = int(progress * 100)

            # Estimate current layer from progress when the slicer does not
            # report it via SET_PRINT_STATS_INFO but we know the total layers.
            if layer_num == 0 and total_layers > 0 and progress > 0:
                layer_num = int(progress * total_layers)

            self._state.layer_num = layer_num
            self._state.total_layers = total_layers

            # Estimate remaining time from total_duration and progress
            total_dur = _number(ps.get("total_duration", 0), 0.0)
            if progress > 0.01 and total_dur > 0:
                estimated_total = total_dur / progress
                self._state.mc_remaining_time = int((estimated_total - total_dur) / 60)
            else:
                self._state.mc_remaining_time = 0

            # Temperatures
            bed = result.get("heater_bed", {})
            self._state.bed_temper = _number(bed.get("temperature"), 0.0)
            self._state.bed_target_temper = _number(bed.get("target"), 0.0)

            ext = result.get("extruder", {})
            self._state.nozzle_temper = _number(ext.get("temperature"), 0.0)
            self._state.nozzle_target_temper = _number(ext.get("target"), 0.0)

            tools = []
            for idx, extruder_obj in enumerate(getattr(self, "_extruder_objects", ["extruder"])):
                tool_data = result.get(extruder_obj, {})
                tools.append({
                    "object": extruder_obj,
                    "name": f"T{idx}",
                    "temperature": _number(tool_data.get("temperature"), 0.0),
                    "target": _number(tool_data.get("target"), 0.0),
                    "pressure_advance": _number(tool_data.get("pressure_advance"), 0.0),
                })
            self._state.klipper_tools = tools

            # Klipper doesn't typically have a chamber temp sensor by default
            # but if configured, it would be a custom heater — leave as 0
            self._state.chamber_temper = 0.0

            # Fan speed (0-1 float → percentage string)
            fan = result.get("fan", {})
            fan_speed = _number(fan.get("speed"), 0.0)
            self._state.cooling_fan_speed = str(int(fan_speed * 255))

            # Speed factor
            gm = result.get("gcode_move", {})
            spd_factor = _number(gm.get("speed_factor"), 1.0)
            self._state.spd_mag = int(spd_factor * 100)

            # Discovered fans (fan_generic, heater_fan, controller_fan)
            fans = []
            for fan_obj in self._fan_objects:
                fan_data = result.get(fan_obj, {})
                speed = _number(fan_data.get("speed"), 0.0)
                # Extract display name: "fan_generic exhaust_fan" → "exhaust_fan"
                parts = fan_obj.split(" ", 1)
                display_name = parts[1] if len(parts) > 1 else parts[0]
                fan_type = parts[0]  # fan_generic, heater_fan, controller_fan
                fans.append({
                    "object": fan_obj,
                    "name": display_name,
                    "type": fan_type,
                    "speed": round(speed, 4),  # 0.0–1.0
                    "controllable": fan_type == "fan_generic",
                })
            self._state.klipper_fans = fans

            # Discovered LEDs (neopixel, led, dotstar)
            leds = []
            for led_obj in self._led_objects:
                led_data = result.get(led_obj, {})
                color_data = led_data.get("color_data", [])
                # Consider LED "on" if any channel in any pixel is > 0
                is_on = any(
                    any(_number(v, 0.0) > 0 for v in pixel)
                    for pixel in color_data
                ) if color_data else False
                parts = led_obj.split(" ", 1)
                display_name = parts[1] if len(parts) > 1 else parts[0]
                leds.append({
                    "object": led_obj,
                    "name": display_name,
                    "type": parts[0],
                    "on": is_on,
                    "color_data": color_data[:1] if color_data else [],  # first pixel only for display
                })
            # Discovered output pins (often used for lights)
            for pin_obj in self._output_pins:
                pin_data = result.get(pin_obj, {})
                value = _number(pin_data.get("value"), 0.0)
                parts = pin_obj.split(" ", 1)
                display_name = parts[1] if len(parts) > 1 else parts[0]
                leds.append({
                    "object": pin_obj,
                    "name": display_name,
                    "type": "output_pin",
                    "on": value > 0,
                    "value": round(value, 4),
                })
            self._state.klipper_leds = leds

            # Set chamber_light based on any detected LED/pin being on
            if leds:
                self._state.chamber_light = any(l["on"] for l in leds)

            # Klipper doesn't have AMS
            self._state.has_ams = False
            self._state.ams_trays = []

            # Happy Hare MMU
            mmu_data = result.get("mmu", {})
            if mmu_data and mmu_data.get("enabled"):
                self._state.has_mmu = True
                mmu_machine = result.get("mmu_machine", {})
                mmu_encoder = result.get("mmu_encoder mmu_encoder", {})
                num_gates = mmu_data.get("num_gates", 0)

                # Build gate/tray info similar to AMS trays
                gates = []
                for i in range(num_gates):
                    gate_status_val = (mmu_data.get("gate_status", []) or [])[i] if i < len(mmu_data.get("gate_status", [])) else 0
                    # gate_status: 0=empty, 1=unknown, 2=loaded
                    gates.append({
                        "gate": i,
                        "status": gate_status_val,
                        "material": (mmu_data.get("gate_material", []) or [""])[i] if i < len(mmu_data.get("gate_material", [])) else "",
                        "color": _hex_color((mmu_data.get("gate_color", []) or [""])[i] if i < len(mmu_data.get("gate_color", [])) else ""),
                        "filament_name": (mmu_data.get("gate_filament_name", []) or [""])[i] if i < len(mmu_data.get("gate_filament_name", [])) else "",
                        "spool_id": (mmu_data.get("gate_spool_id", []) or [-1])[i] if i < len(mmu_data.get("gate_spool_id", [])) else -1,
                        "temperature": (mmu_data.get("gate_temperature", []) or [0])[i] if i < len(mmu_data.get("gate_temperature", [])) else 0,
                    })

                unit_info = {}
                if mmu_machine:
                    unit_info = mmu_machine.get("unit_0", {})

                self._state.mmu = {
                    "enabled": True,
                    "print_state": mmu_data.get("print_state", ""),
                    "tool": mmu_data.get("tool", -1),
                    "gate": mmu_data.get("gate", -1),
                    "filament": mmu_data.get("filament", ""),
                    "filament_pos": mmu_data.get("filament_pos", 0),
                    "filament_position": mmu_data.get("filament_position", 0.0),
                    "filament_direction": mmu_data.get("filament_direction", 0),
                    "num_gates": num_gates,
                    "num_toolchanges": mmu_data.get("num_toolchanges", 0),
                    "last_toolchange": mmu_data.get("last_toolchange", "Unknown"),
                    "is_homed": mmu_data.get("is_homed", False),
                    "is_locked": mmu_data.get("is_locked", False),
                    "is_paused": mmu_data.get("is_paused", False),
                    "is_in_print": mmu_data.get("is_in_print", False),
                    "runout": mmu_data.get("runout", False),
                    "action": mmu_data.get("action", "Idle"),
                    "servo": mmu_data.get("servo", ""),
                    "sync_drive": mmu_data.get("sync_drive", False),
                    "has_bypass": mmu_data.get("has_bypass", False),
                    "clog_detection": mmu_data.get("clog_detection_enabled", 0),
                    "endless_spool": mmu_data.get("endless_spool_enabled", 0),
                    "active_filament": mmu_data.get("active_filament", {}),
                    "ttg_map": mmu_data.get("ttg_map", []),
                    "endless_spool_groups": mmu_data.get("endless_spool_groups", []),
                    "gates": gates,
                    "unit_name": unit_info.get("name", ""),
                    "unit_vendor": unit_info.get("vendor", ""),
                    "sensors": mmu_data.get("sensors", {}),
                    "bowden_progress": mmu_data.get("bowden_progress", -1),
                    "extruder_filament_remaining": mmu_data.get("extruder_filament_remaining", 0),
                    "flowguard": mmu_data.get("flowguard", {}),
                    "encoder": {
                        "enabled": mmu_encoder.get("enabled", False) if mmu_encoder else False,
                        "flow_rate": mmu_encoder.get("flow_rate", 0) if mmu_encoder else 0,
                        "encoder_pos": mmu_encoder.get("encoder_pos", 0.0) if mmu_encoder else 0.0,
                        "detection_length": mmu_encoder.get("detection_length", 0) if mmu_encoder else 0,
                        "headroom": mmu_encoder.get("headroom", 0) if mmu_encoder else 0,
                        "desired_headroom": mmu_encoder.get("desired_headroom", 0) if mmu_encoder else 0,
                        "detection_mode": mmu_encoder.get("detection_mode", 0) if mmu_encoder else 0,
                    },
                }
            else:
                self._state.has_mmu = False
                self._state.mmu = {}

            self._state.raw_data = result

        # Fetch Obico failure detection data (outside the lock, separate request)
        if self._obico:
            try:
                obico_data = self._obico.fetch_status()
                with self._state_lock:
                    self._state.obico = obico_data
            except Exception as e:
                logger.debug(f"[{self.name}] Obico poll error: {e}")

        # Keep Adaptive Flow URL on state so frontend can link to it
        if self._adaptive_flow_url:
            with self._state_lock:
                self._state.adaptive_flow = {"url": self._adaptive_flow_url}

        new_status = self._state.status
        if new_status != prev_status:
            logger.info(f"[{self.name}] Status: {prev_status.value} -> {new_status.value}")
            if self._on_state_change:
                self._on_state_change(self.name, self._state)

    def _get_cached_layer_count(self, filename: str) -> int:
        """Return total layer count for *filename* from Moonraker file metadata.

        Positive results are cached so the metadata endpoint is only queried
        once per file.  Zero / failure results are *not* cached so that we
        retry on the next poll cycle (e.g. while Moonraker is still analysing
        the file right after upload).
        """
        if filename in self._layer_cache:
            return self._layer_cache[filename]
        try:
            meta_resp = self._session.get(
                f"{self._base_url}/server/files/metadata",
                params={"filename": filename},
                timeout=5,
            )
            if meta_resp.status_code == 200:
                meta = meta_resp.json().get("result", {})
                count = meta.get("layer_count") or 0
                if count > 0:
                    self._layer_cache[filename] = count
                    logger.debug(
                        f"[{self.name}] File metadata layer count for "
                        f"'{filename}': {count}"
                    )
                return count
            else:
                logger.debug(
                    f"[{self.name}] File metadata request for '{filename}' "
                    f"returned HTTP {meta_resp.status_code}"
                )
        except Exception as e:
            logger.debug(f"[{self.name}] Could not fetch file metadata for '{filename}': {e}")
        return 0

    @staticmethod
    def _map_status(klipper_state: str) -> PrintStatus:
        """Map Klipper print_stats state to our PrintStatus enum."""
        mapping = {
            "standby": PrintStatus.IDLE,
            "printing": PrintStatus.RUNNING,
            "paused": PrintStatus.PAUSED,
            "complete": PrintStatus.FINISH,
            "cancelled": PrintStatus.IDLE,
            "error": PrintStatus.FAILED,
        }
        return mapping.get(klipper_state, PrintStatus.UNKNOWN)
