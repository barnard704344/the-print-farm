"""
Farm Manager — coordinates multiple printers (BambuLab + Klipper).

Maintains connections to all printers, tracks their states,
and provides a unified interface for the web UI and job queue.
"""

import copy
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Dict, Optional, Union

from .bambu_client import BambuClient, PrintStatus
from .klipper_client import KlipperClient

logger = logging.getLogger(__name__)

# Union type for all supported printer clients
PrinterClient = Union[BambuClient, KlipperClient]

# A print moving from one of these states into FINISH/FAILED leaves something
# on the build plate that a person must remove before the next job.
_PRINTING_STATES = (PrintStatus.RUNNING, PrintStatus.PAUSED, PrintStatus.PAUSE_FILAMENT)
_ENDED_STATES = (PrintStatus.FINISH, PrintStatus.FAILED)
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
            tls_fingerprints=cfg.get("tls_fingerprints"),
        )


class FarmManager:
    """Manages multiple printers (BambuLab and Klipper) as a farm."""

    def __init__(self, printer_configs: list = None):
        self._printers: Dict[str, PrinterClient] = {}
        self._printer_types: Dict[str, str] = {}
        self._lock = threading.Lock()
        self._failure_timeout_seconds = 300
        self._failure_first_seen: Dict[str, float] = {}
        # Persisted MMU gate configs: {printer_name: {gate_index: {material, color, spool_id}}}
        self._gate_configs: Dict[str, Dict[int, dict]] = {}
        self._gate_config_saver = None  # callable set by load_gate_configs()
        # Persisted AMS tray spool assignments: {printer_name: {tray_id: spool_id}}
        self._ams_tray_configs: Dict[str, Dict[int, int]] = {}
        self._ams_tray_saver = None   # callable(printer, tray_id, spool_id)
        self._ams_tray_deleter = None  # callable(printer, tray_id)
        # Build plate confirmation: {printer_name: state dict}
        self._plate_states: Dict[str, dict] = {}
        self._plate_required: Dict[str, bool] = {}
        self._plate_last_status: Dict[str, PrintStatus] = {}
        self._plate_saver = None  # callable(printer, state)
        self._plate_lock = threading.Lock()
        for cfg in (printer_configs or []):
            name = cfg["name"]
            printer_type = cfg.get("type", "bambulab").lower()
            client = create_printer_client(cfg)
            self._printers[name] = client
            self._printer_types[name] = printer_type
            self._plate_required[name] = bool(cfg.get("require_plate_clear", True))

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

    def _effective_status(self, name: str, client: PrinterClient) -> PrintStatus:
        """Return farm status, auto-clearing stale failures after the configured delay."""
        reported = client.state.status
        if reported != PrintStatus.FAILED:
            self._failure_first_seen.pop(name, None)
            return reported
        now = time.monotonic()
        first_seen = self._failure_first_seen.setdefault(name, now)
        if (
            self._failure_timeout_seconds > 0
            and now - first_seen >= self._failure_timeout_seconds
        ):
            return PrintStatus.IDLE
        return reported

    def get_effective_status(self, name: str) -> Optional[PrintStatus]:
        client = self._printers.get(name)
        return self._effective_status(name, client) if client else None

    def set_failure_timeout(self, seconds: float) -> None:
        """Set how long FAILED remains blocking; zero disables automatic clearing."""
        self._failure_timeout_seconds = max(0.0, float(seconds))

    def get_failure_timeout(self) -> float:
        return self._failure_timeout_seconds

    def reset_failure_timer(self, name: str) -> None:
        """Start a fresh cooldown for a newly detected failure."""
        self._failure_first_seen[name] = time.monotonic()

    def load_gate_configs(self, job_queue) -> None:
        """Load persisted MMU gate configs from the database into memory."""
        self._gate_config_saver = job_queue.save_gate_config
        for name in self._printers:
            configs = job_queue.get_gate_configs(name)
            if configs:
                self._gate_configs[name] = {
                    c["gate"]: {
                        "material": c["material"],
                        "color": c["color"],
                        "spool_id": c["spool_id"],
                    }
                    for c in configs
                }

    def load_ams_tray_configs(self, job_queue) -> None:
        """Load persisted AMS tray spool assignments from the database."""
        self._ams_tray_saver = job_queue.save_ams_tray_config
        self._ams_tray_deleter = job_queue.delete_ams_tray_config
        for name in self._printers:
            configs = job_queue.get_ams_tray_configs(name)
            if configs:
                self._ams_tray_configs[name] = configs

    def save_ams_tray_config(self, printer_name: str, tray_id: int,
                             spool_id: int) -> None:
        """Persist an AMS tray spool assignment in memory and to the database."""
        if printer_name not in self._ams_tray_configs:
            self._ams_tray_configs[printer_name] = {}
        self._ams_tray_configs[printer_name][tray_id] = spool_id
        if self._ams_tray_saver:
            self._ams_tray_saver(printer_name, tray_id, spool_id)

    def delete_ams_tray_config(self, printer_name: str, tray_id: int) -> None:
        """Remove an AMS tray spool assignment."""
        if printer_name in self._ams_tray_configs:
            self._ams_tray_configs[printer_name].pop(tray_id, None)
        if self._ams_tray_deleter:
            self._ams_tray_deleter(printer_name, tray_id)

    def get_ams_tray_spool_id(self, printer_name: str, tray_id: int) -> int:
        """Return the assigned spool_id for an AMS tray, or -1 if unassigned."""
        return self._ams_tray_configs.get(printer_name, {}).get(tray_id, -1)

    def save_gate_config(self, printer_name: str, gate: int,
                         material: str = '', color: str = '',
                         spool_id: int = -1) -> None:
        """Persist an MMU gate assignment in memory and to the database."""
        if printer_name not in self._gate_configs:
            self._gate_configs[printer_name] = {}
        self._gate_configs[printer_name][gate] = {
            "material": material,
            "color": color,
            "spool_id": spool_id,
        }
        if self._gate_config_saver:
            self._gate_config_saver(printer_name, gate, material, color, spool_id)

    def get_gate_config(self, printer_name: str, gate: int) -> dict:
        """Return persisted gate config for a specific gate, or empty dict."""
        return self._gate_configs.get(printer_name, {}).get(gate, {})

    def get_all_states(self) -> Dict[str, dict]:
        """Get serializable state for all printers (both Bambu and Klipper)."""
        states = {}
        for name, client in self._printers.items():
            s = client.state
            status = self._effective_status(name, client)
            reported_status = s.status
            auto_cleared_failure = (
                reported_status == PrintStatus.FAILED and status == PrintStatus.IDLE
            )
            printer_type = self._printer_types.get(name, "bambulab")
            state = {
                "name": name,
                "host": client.host,
                "type": printer_type,
                "connected": client.is_connected(),
                "status": status.value,
                "display_status": "READY" if auto_cleared_failure else status.value,
                "reported_status": reported_status.value,
                "failure_auto_cleared": auto_cleared_failure,
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
                "ams_units": s.ams_units,
                "vt_tray": s.vt_tray,
                "has_mmu": s.has_mmu,
                "mmu": s.mmu,  # will be overlaid below if gate configs are persisted
                "klipper_tools": s.klipper_tools,
                "klipper_fans": s.klipper_fans,
                "klipper_leds": s.klipper_leds,
                "obico": s.obico,
            }
            state.update(self.get_plate_state(name))

            # Overlay persisted gate configs onto MMU gates when HH has cleared them.
            # HH resets gate_spool_id (and sometimes material/color) after a print
            # finishes, but the filament is still physically loaded. We keep our own
            # persistent copy so the card always shows the correct assignment.
            printer_gate_cfgs = self._gate_configs.get(name)
            if printer_gate_cfgs and state["has_mmu"] and state["mmu"] and state["mmu"].get("gates"):
                mmu_copy = copy.deepcopy(state["mmu"])
                for gate in mmu_copy.get("gates", []):
                    gate_idx = gate.get("gate", -1)
                    # Only overlay for gates that are not empty (status != 0)
                    if gate_idx < 0 or gate.get("status", 0) == 0:
                        continue
                    persisted = printer_gate_cfgs.get(gate_idx)
                    if not persisted:
                        continue
                    if gate.get("spool_id", -1) <= 0 and persisted.get("spool_id", -1) > 0:
                        gate["spool_id"] = persisted["spool_id"]
                    if not gate.get("material") and persisted.get("material"):
                        gate["material"] = persisted["material"]
                    if not gate.get("color") and persisted.get("color"):
                        gate["color"] = persisted["color"]
                state["mmu"] = mmu_copy

            # Overlay persisted AMS tray spool assignments
            ams_tray_cfgs = self._ams_tray_configs.get(name)
            if ams_tray_cfgs and state["has_ams"] and state["ams_trays"]:
                trays_copy = copy.deepcopy(state["ams_trays"])
                for tray in trays_copy:
                    tray["spool_id"] = ams_tray_cfgs.get(tray["id"], -1)
                state["ams_trays"] = trays_copy
            elif state["has_ams"] and state["ams_trays"]:
                for tray in state["ams_trays"]:
                    tray.setdefault("spool_id", -1)

            states[name] = state
        return states

    def get_idle_printers(self) -> list:
        """Return names of printers that are idle, connected, and ready for a job.

        Printers whose build plate has not been confirmed clear are excluded.
        """
        idle = []
        for name, client in self._printers.items():
            if (
                client.is_connected()
                and self._effective_status(name, client) in (PrintStatus.IDLE, PrintStatus.FINISH)
                and not self.plate_needs_clearing(name)
            ):
                idle.append(name)
        return idle

    # ── Build plate confirmation ──────────────────────────

    def load_plate_states(self, job_queue) -> None:
        """Load persisted plate states and persist future changes via job_queue."""
        self._plate_saver = job_queue.save_plate_state
        stored = job_queue.get_plate_states()
        with self._plate_lock:
            for name in self._printers:
                if name in stored:
                    self._plate_states[name] = stored[name]

    def _persist_plate(self, name: str, state: dict) -> None:
        if self._plate_saver:
            try:
                self._plate_saver(name, state)
            except Exception:
                logger.exception("Could not persist build plate state for %s", name)

    def set_plate_clear_required(self, name: str, required: bool) -> None:
        self._plate_required[name] = bool(required)

    def plate_needs_clearing(self, name: str) -> bool:
        if not self._plate_required.get(name, True):
            return False
        with self._plate_lock:
            return bool(self._plate_states.get(name, {}).get("needs_clear"))

    def get_plate_state(self, name: str) -> dict:
        with self._plate_lock:
            state = dict(self._plate_states.get(name, {}))
        required = self._plate_required.get(name, True)
        return {
            "plate_clear_required": required,
            "plate_needs_clear": bool(required and state.get("needs_clear")),
            "plate_finished_status": state.get("finished_status", ""),
            "plate_finished_at": state.get("finished_at"),
            "plate_cleared_by": state.get("cleared_by", ""),
            "plate_cleared_at": state.get("cleared_at"),
        }

    def mark_plate_needs_clearing(self, name: str, finished_status: str) -> None:
        state = {
            "needs_clear": True,
            "finished_status": finished_status,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "cleared_by": "",
            "cleared_at": None,
        }
        with self._plate_lock:
            self._plate_states[name] = state
        self._persist_plate(name, state)
        logger.info("[%s] Build plate needs clearing (print %s)", name, finished_status)

    def confirm_plate_clear(self, name: str, cleared_by: str) -> bool:
        """Record that someone has cleared the plate. Returns False if unknown printer."""
        if name not in self._printers:
            return False
        with self._plate_lock:
            state = dict(self._plate_states.get(name, {}))
            state.update({
                "needs_clear": False,
                "cleared_by": cleared_by or "",
                "cleared_at": datetime.now(timezone.utc).isoformat(),
            })
            self._plate_states[name] = state
        self._persist_plate(name, state)
        logger.info("[%s] Build plate confirmed clear by %s", name, cleared_by or "unknown")
        return True

    def update_plate_states(self) -> None:
        """Watch printer status changes and flag plates that need clearing.

        Called periodically by the main loop. Covers jobs sent by the farm and
        prints started directly on the printer.
        """
        for name, client in self._printers.items():
            if not client.is_connected():
                continue
            status = client.state.status
            previous = self._plate_last_status.get(name)
            self._plate_last_status[name] = status
            with self._plate_lock:
                known = name in self._plate_states
                needs_clear = bool(self._plate_states.get(name, {}).get("needs_clear"))

            if previous in _PRINTING_STATES and status in _ENDED_STATES:
                self.mark_plate_needs_clearing(name, status.value)
            elif not known and status in _ENDED_STATES:
                # No plate record yet (e.g. right after this feature was
                # installed, possibly after an initial UNKNOWN report) and the
                # printer is sitting on a finished print: ask for confirmation.
                self.mark_plate_needs_clearing(name, status.value)
            elif status == PrintStatus.RUNNING and needs_clear and previous != PrintStatus.RUNNING:
                # Someone started a print at the printer itself, so they have
                # dealt with the plate.
                self.confirm_plate_clear(name, "printer (print started)")

    def get_farm_summary(self) -> dict:
        """High-level farm stats."""
        total = len(self._printers)
        connected = sum(1 for c in self._printers.values() if c.is_connected())
        printing = sum(
            1 for name, c in self._printers.items()
            if c.is_connected() and self._effective_status(name, c) == PrintStatus.RUNNING
        )
        idle = len(self.get_idle_printers())
        errored = sum(
            1 for name, c in self._printers.items()
            if c.is_connected() and self._effective_status(name, c) in (PrintStatus.FAILED, PrintStatus.PAUSE_FILAMENT)
        )
        return {
            "total": total,
            "connected": connected,
            "printing": printing,
            "idle": idle,
            "errored": errored,
        }
