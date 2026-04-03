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

            # Detect Happy Hare MMU
            try:
                obj_resp = self._session.get(
                    f"{self._base_url}/printer/objects/list", timeout=5)
                if obj_resp.status_code == 200:
                    objects = obj_resp.json().get("result", {}).get("objects", [])
                    if "mmu" in objects:
                        self._has_mmu = True
                        logger.info(f"[{self.name}] Happy Hare MMU detected")
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

    def set_nozzle_temperature(self, temp: int) -> bool:
        temp = max(0, min(temp, 300))
        return self._gcode(f"M104 S{temp}")

    def set_chamber_light(self, on: bool) -> bool:
        """Klipper doesn't have a standard chamber light command.
        This sends a custom macro if configured, otherwise no-op."""
        macro = "CHAMBER_LIGHT_ON" if on else "CHAMBER_LIGHT_OFF"
        return self._gcode(macro)

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
            "extruder": "temperature,target,pressure_advance",
            "print_stats": "state,filename,total_duration,print_duration,filament_used,message",
            "display_status": "progress,message",
            "virtual_sdcard": "progress,file_position,file_path",
            "fan": "speed",
            "gcode_move": "speed_factor",
        }

        # Include Happy Hare MMU objects if detected
        if self._has_mmu:
            for obj in _MMU_OBJECTS:
                params[obj] = ""

        resp = self._session.get(
            f"{self._base_url}/printer/objects/query",
            params=params,
            timeout=5,
        )

        if resp.status_code != 200:
            raise ConnectionError(f"Status query returned {resp.status_code}")

        result = resp.json().get("result", {}).get("status", {})
        prev_status = self._state.status

        with self._state_lock:
            # Print stats
            ps = result.get("print_stats", {})
            klipper_state = ps.get("state", "standby")
            self._state.status = self._map_status(klipper_state)
            self._state.gcode_state = klipper_state
            self._state.subtask_name = ps.get("filename", "")
            self._state.gcode_file = ps.get("filename", "")

            # Progress
            ds = result.get("display_status", {})
            vs = result.get("virtual_sdcard", {})
            progress = ds.get("progress", vs.get("progress", 0))
            self._state.mc_percent = int(progress * 100)

            # Estimate remaining time from total_duration and progress
            total_dur = ps.get("total_duration", 0)
            if progress > 0.01 and total_dur > 0:
                estimated_total = total_dur / progress
                self._state.mc_remaining_time = int((estimated_total - total_dur) / 60)
            else:
                self._state.mc_remaining_time = 0

            # Temperatures
            bed = result.get("heater_bed", {})
            self._state.bed_temper = bed.get("temperature", 0.0)
            self._state.bed_target_temper = bed.get("target", 0.0)

            ext = result.get("extruder", {})
            self._state.nozzle_temper = ext.get("temperature", 0.0)
            self._state.nozzle_target_temper = ext.get("target", 0.0)

            # Klipper doesn't typically have a chamber temp sensor by default
            # but if configured, it would be a custom heater — leave as 0
            self._state.chamber_temper = 0.0

            # Fan speed (0-1 float → percentage string)
            fan = result.get("fan", {})
            fan_speed = fan.get("speed", 0)
            self._state.cooling_fan_speed = str(int(fan_speed * 255))

            # Speed factor
            gm = result.get("gcode_move", {})
            spd_factor = gm.get("speed_factor", 1.0)
            self._state.spd_mag = int(spd_factor * 100)

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
                    "num_gates": num_gates,
                    "num_toolchanges": mmu_data.get("num_toolchanges", 0),
                    "is_homed": mmu_data.get("is_homed", False),
                    "is_paused": mmu_data.get("is_paused", False),
                    "runout": mmu_data.get("runout", False),
                    "active_filament": mmu_data.get("active_filament", {}),
                    "ttg_map": mmu_data.get("ttg_map", []),
                    "endless_spool_groups": mmu_data.get("endless_spool_groups", []),
                    "gates": gates,
                    "unit_name": unit_info.get("name", ""),
                    "unit_vendor": unit_info.get("vendor", ""),
                    "encoder": {
                        "enabled": mmu_encoder.get("enabled", False),
                        "flow_rate": mmu_encoder.get("flow_rate", 0),
                    } if mmu_encoder else {},
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
