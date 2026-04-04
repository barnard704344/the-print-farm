"""
Farm Manager — coordinates multiple printers (BambuLab + Klipper).

Maintains connections to all printers, tracks their states,
and provides a unified interface for the web UI and job queue.
"""

import logging
import threading
import time
from typing import Dict, Optional, Union

from .bambu_client import BambuClient, PrintState, PrintStatus
from .klipper_client import KlipperClient

logger = logging.getLogger(__name__)

# Union type for all supported printer clients
PrinterClient = Union[BambuClient, KlipperClient]


def create_printer_client(cfg: dict) -> PrinterClient:
    """Factory: create the right client based on printer type in config."""
    printer_type = cfg.get("type", "bambulab").lower()
    name = cfg["name"]

    if printer_type == "klipper":
        return KlipperClient(
            name=name,
            host=cfg["host"],
            port=cfg.get("moonraker_port", 7125),
            api_key=cfg.get("api_key", ""),
            camera_url=cfg.get("camera_url", ""),
            obico_config=cfg.get("obico"),
        )
    else:
        # Default: BambuLab
        return BambuClient(
            name=name,
            host=cfg["host"],
            access_code=cfg["access_code"],
            serial=cfg["serial"],
            port=cfg.get("mqtt_port", 8883),
            ftp_port=cfg.get("ftp_port", 990),
            camera_port=cfg.get("camera_port", 6000),
            ams_serial=cfg.get("ams_serial", ""),
        )


class FarmManager:
    """Manages multiple printers (BambuLab and Klipper) as a farm."""

    def __init__(self, printer_configs: list = None):
        self._printers: Dict[str, PrinterClient] = {}
        self._printer_types: Dict[str, str] = {}
        self._lock = threading.Lock()

        for cfg in (printer_configs or []):
            name = cfg["name"]
            printer_type = cfg.get("type", "bambulab").lower()
            client = create_printer_client(cfg)
            self._printers[name] = client
            self._printer_types[name] = printer_type

    def connect_all(self, timeout: float = 10.0) -> Dict[str, bool]:
        """Connect to all printers. Returns {name: success}."""
        results = {}
        threads = []

        def _connect(name, client):
            results[name] = client.connect(timeout=timeout)

        for name, client in self._printers.items():
            t = threading.Thread(target=_connect, args=(name, client))
            threads.append(t)
            t.start()

        for t in threads:
            t.join(timeout=timeout + 5)

        connected = sum(1 for v in results.values() if v)
        logger.info(f"Farm: {connected}/{len(results)} printers connected")
        return results

    def disconnect_all(self):
        for name, client in self._printers.items():
            client.disconnect()

    def get_printer(self, name: str) -> Optional[PrinterClient]:
        return self._printers.get(name)

    def get_all_printers(self) -> Dict[str, PrinterClient]:
        return dict(self._printers)

    def get_printer_type(self, name: str) -> str:
        """Return 'bambulab' or 'klipper' for a given printer name."""
        return self._printer_types.get(name, "bambulab")

    def get_all_states(self) -> Dict[str, dict]:
        """Get serializable state for all printers (both Bambu and Klipper)."""
        states = {}
        for name, client in self._printers.items():
            s = client.state
            printer_type = self._printer_types.get(name, "bambulab")
            state = {
                "name": name,
                "host": client.host,
                "type": printer_type,
                "connected": client.is_connected(),
                "status": s.status.value,
                "gcode_state": s.gcode_state,
                "mc_percent": s.mc_percent,
                "mc_remaining_time": s.mc_remaining_time,
                "layer_num": s.layer_num,
                "total_layers": s.total_layers,
                "subtask_name": s.subtask_name,
                "bed_temper": s.bed_temper,
                "bed_target_temper": s.bed_target_temper,
                "nozzle_temper": s.nozzle_temper,
                "nozzle_target_temper": s.nozzle_target_temper,
                "chamber_temper": s.chamber_temper,
                "cooling_fan_speed": s.cooling_fan_speed,
                "heatbreak_fan_speed": s.heatbreak_fan_speed,
                "big_fan1_speed": s.big_fan1_speed,
                "big_fan2_speed": s.big_fan2_speed,
                "spd_lvl": s.spd_lvl,
                "spd_mag": s.spd_mag,
                "nozzle_diameter": s.nozzle_diameter,
                "nozzle_type": s.nozzle_type,
                "wifi_signal": s.wifi_signal,
                "chamber_light": s.chamber_light,
                "print_error": s.print_error,
                "hms": s.hms or [],
                "has_ams": s.has_ams,
                "ams_serial": getattr(client, 'ams_serial', ''),
                "ams_trays": s.ams_trays,
                "ams_tray_now": s.ams_tray_now,
                "ams_humidity": s.ams_humidity,
                "vt_tray": s.vt_tray,
                "has_mmu": s.has_mmu,
                "mmu": s.mmu,
                "obico": s.obico,
                "adaptive_flow": s.adaptive_flow,
            }
            states[name] = state
        return states

    def get_idle_printers(self) -> list:
        """Return names of printers that are idle and connected."""
        idle = []
        for name, client in self._printers.items():
            if client.is_connected() and client.state.status in (
                PrintStatus.IDLE, PrintStatus.FINISH
            ):
                idle.append(name)
        return idle

    def get_farm_summary(self) -> dict:
        """High-level farm stats."""
        total = len(self._printers)
        connected = sum(1 for c in self._printers.values() if c.is_connected())
        printing = sum(
            1 for c in self._printers.values()
            if c.is_connected() and c.state.status == PrintStatus.RUNNING
        )
        idle = len(self.get_idle_printers())
        errored = sum(
            1 for c in self._printers.values()
            if c.is_connected() and c.state.status in (PrintStatus.FAILED, PrintStatus.PAUSE_FILAMENT)
        )
        return {
            "total": total,
            "connected": connected,
            "printing": printing,
            "idle": idle,
            "errored": errored,
        }
