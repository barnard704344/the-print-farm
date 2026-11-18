"""Bambu LAN printers as Obico agents, sharing the farm's MQTT/camera clients.

Protocol references: Obico's /ws/dev/ agent connection, /api/v1/octo/printer/
and /api/v1/octo/pic/ (also used by moonraker-obico). No OctoPrint is needed.
"""

from collections import deque
from copy import deepcopy
import hashlib
import json
import logging
import os
from pathlib import Path
import tempfile
import threading
import time
from urllib.parse import urlsplit, urlunsplit

import requests
import websocket

from .bambu_client import PrintStatus
from .obico_client import ObicoClient

logger = logging.getLogger(__name__)
MASK = "********"
ACTIVE = {PrintStatus.RUNNING, PrintStatus.PAUSED, PrintStatus.PAUSE_FILAMENT}
PAUSED = {PrintStatus.PAUSED, PrintStatus.PAUSE_FILAMENT}


def same_job_id(first, second):
    """Bambu sends partial task identity; filling a missing half is not a new job."""
    if not first or not second:
        return True
    a = (first.split(":", 1) + [""])[:2]
    b = (second.split(":", 1) + [""])[:2]
    return all(not x or not y or x == y for x, y in zip(a, b))


def server_url(value):
    """Accept a server base URL, never credentials or tokens embedded in a URL."""
    value = str(value or "").strip().rstrip("/")
    p = urlsplit(value)
    if (p.scheme not in {"http", "https"} or not p.hostname or p.username
            or p.password or p.query or p.fragment):
        raise ValueError("Obico server must be an http(s) URL without credentials, query or fragment")
    try:
        p.port
    except ValueError:
        raise ValueError("Invalid Obico server port") from None
    return value


def websocket_url(value):
    p = urlsplit(server_url(value))
    return urlunsplit(("wss" if p.scheme == "https" else "ws", p.netloc,
                       p.path + "/ws/dev/", "", ""))


def linked_printer(config, session=None):
    """Validate the agent token against the requested printer without connecting WS.

    A connection test must not kick an existing agent off its live websocket.
    """
    s = session or requests.Session()
    try:
        r = s.get(server_url(config["server"]) + "/api/v1/octo/printer/",
                  headers={"Authorization": "Token " + config["auth_token"]},
                  timeout=10, allow_redirects=False)
        if r.status_code != 200:
            raise ValueError(f"Obico agent token check returned HTTP {r.status_code}")
        printer = r.json()["printer"]
        if int(printer["id"]) != int(config["printer_id"]):
            raise ValueError("Agent token belongs to a different Obico printer ID")
        return printer
    except (requests.RequestException, KeyError, TypeError):
        # Request exception strings may contain credentials, URLs or headers.
        raise ValueError("Could not validate the Obico agent connection") from None
    finally:
        if session is None:
            s.close()


class PrintLedger:
    """Keep the same Obico print identity across agent restarts and reconnects."""

    def __init__(self, path):
        self.path = Path(path)
        try:
            self.data = json.loads(self.path.read_text())
            if not isinstance(self.data, dict):
                self.data = {}
        except (OSError, ValueError):
            self.data = {}

    def save(self, data):
        if data == self.data:
            return
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", dir=self.path.parent, delete=False) as f:
            try:
                os.chmod(f.name, 0o600)
                json.dump(data, f)
                f.flush()
                os.fsync(f.fileno())
                os.replace(f.name, self.path)
            finally:
                if os.path.exists(f.name):
                    os.unlink(f.name)
        self.data = deepcopy(data)

    def messages(self, state, now):
        """Return ordered messages plus ledger changes, committed only after send."""
        previous = self.data
        data = dict(previous)
        filename = state.subtask_name or state.gcode_file or "Bambu print"
        same_job = (same_job_id(state.job_id, previous.get("job_id"))
                    and (previous.get("filename") == filename
                         or bool(state.job_id and previous.get("job_id"))))
        messages = []
        event = None
        if state.status in ACTIVE:
            if not previous.get("print_ts") or not same_job:
                data = {"print_ts": int(now), "filename": filename,
                        "job_id": state.job_id}
                event = "PrintStarted"
            elif state.status in PAUSED and previous.get("status") == PrintStatus.RUNNING.value:
                event = "PrintPaused"
            elif state.status == PrintStatus.RUNNING and previous.get("status") in {p.value for p in PAUSED}:
                event = "PrintResumed"
            data["status"] = state.status.value
            if state.job_id:
                data["job_id"] = state.job_id
            messages.append(status_message(state, now, data["print_ts"], data["filename"], event))
        else:
            if previous.get("print_ts") and state.status in {PrintStatus.FINISH, PrintStatus.FAILED}:
                event = "PrintDone" if state.status == PrintStatus.FINISH else "PrintFailed"
                messages.append(status_message(state, now, previous["print_ts"], previous["filename"], event))
            data = {}
            messages.append(status_message(state, now, -1, filename))
        return messages, data


def status_message(s, now, print_ts, filename, event=None):
    paused = s.status in PAUSED
    running = s.status == PrintStatus.RUNNING
    status = {
        "_ts": now,
        "state": {"text": "Printing" if running else "Paused" if paused else "Operational",
                  "flags": {"operational": True, "printing": running, "paused": paused,
                            "ready": not (running or paused), "error": s.status == PrintStatus.FAILED,
                            "closedOrError": False, "cancelling": False, "pausing": False}},
        "job": {"file": {"name": filename, "path": filename, "display": filename}},
        "progress": {"completion": s.mc_percent, "printTime": None,
                     "printTimeLeft": max(0, s.mc_remaining_time) * 60},
        "temperatures": {
            "tool0": {"actual": s.nozzle_temper, "target": s.nozzle_target_temper, "offset": 0},
            "bed": {"actual": s.bed_temper, "target": s.bed_target_temper, "offset": 0}},
        "currentLayerHeight": s.layer_num,
        "file_metadata": {"obico": {"totalLayerCount": s.total_layers}},
    }
    msg = {"current_print_ts": print_ts, "status": status}
    if event:
        msg["event"] = {"event_type": event}
    return msg


class BambuObicoBridge:
    def __init__(self, name, config, farm, cameras, state_dir):
        self.name, self.config = name, dict(config)
        self.farm, self.cameras = farm, cameras
        identity = config["server"] + "\0" + str(config["printer_id"])
        self.ledger = PrintLedger(Path(state_dir) / (hashlib.sha256(identity.encode()).hexdigest() + ".json"))
        self.stop_event = threading.Event()
        self.ws = None
        self.thread = None
        self.session = requests.Session()
        self.reader = ObicoClient(config["server"], config["username"], config["password"], config["printer_id"])
        self.remote = {}
        self.seen_refs = deque(maxlen=100)
        self.last_command = None
        self.last_frame_at = 0
        self.last_snapshot_at = None
        self.error = ""

    def start(self):
        self.thread = threading.Thread(target=self.run, daemon=True, name="obico-" + self.name)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        ws = self.ws
        if ws:
            ws.close()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=15)
        return not self.thread or not self.thread.is_alive()

    def publish(self, **fields):
        p = self.farm.get_printer(self.name)
        if p:
            current = dict(p.state.obico)
            current.update(fields, bridge=True,
                           remote_control=self.config.get("allow_remote_control", False),
                           last_snapshot_at=self.last_snapshot_at)
            p.set_obico_status(current)

    @staticmethod
    def fresh(printer):
        return printer.is_connected() and printer.last_report_age is not None and printer.last_report_age < 90

    def send(self, message):
        if not self.stop_event.is_set():
            self.ws.send(json.dumps(message))

    def command(self, command):
        """Only explicit pause/resume/cancel, for this live printer and active job."""
        if self.stop_event.is_set() or not self.config.get("allow_remote_control", False):
            return "Remote control is disabled in The Print Farm"
        p = self.farm.get_printer(self.name)
        if not p or not self.fresh(p) or not self.ledger.data.get("print_ts"):
            return "Printer is offline or has no active print"
        s = p.state
        if ((s.subtask_name or s.gcode_file or "Bambu print") != self.ledger.data.get("filename")
                and not (s.job_id and self.ledger.data.get("job_id"))):
            return "Print changed; command ignored"
        if not same_job_id(s.job_id, self.ledger.data.get("job_id")):
            return "Print changed; command ignored"
        allowed = {"pause": ({PrintStatus.RUNNING}, p.pause_print),
                   "resume": (PAUSED, p.resume_print), "cancel": (ACTIVE, p.stop_print)}
        if command not in allowed:
            return "Unsupported printer command"
        states, action = allowed[command]
        if s.status not in states:
            return "Command does not match the current printer state"
        key = (command, self.ledger.data["print_ts"])
        if self.last_command and self.last_command[0] == key and time.monotonic() - self.last_command[1] < 10:
            return "Duplicate command ignored"
        self.last_command = (key, time.monotonic())
        return None if action() else "Printer rejected command"

    def receive(self, message):
        if not isinstance(message, dict):
            return
        remote = message.get("remote_status")
        if isinstance(remote, dict):
            self.remote.update(remote)
            self.publish(watching=bool(self.remote.get("should_watch")))
        for c in message.get("commands", []):
            if isinstance(c, dict):
                error = self.command(c.get("cmd"))
                self.publish(command_error=error or "")
        passthru = message.get("passthru")
        if not isinstance(passthru, dict):
            return
        ref = passthru.get("ref")
        if ref is not None and ref in self.seen_refs:
            return
        if ref is not None:
            self.seen_refs.append(ref)
        if (passthru.get("target") == "printer" and not passthru.get("args")
                and not passthru.get("kwargs")):
            error = self.command(passthru.get("func"))
        else:
            error = "Unsupported request: only pause, resume and cancel are available"
        if ref is not None:
            self.send({"passthru": {"ref": ref, **({"error": error} if error else {"ret": None})}})

    def post_snapshot(self, printer):
        try:
            self._post_snapshot(printer)
        except Exception as exc:
            self.publish(camera_error="Camera upload failed (" + type(exc).__name__ + ")")

    def _post_snapshot(self, printer):
        if self.stop_event.is_set() or not self.fresh(printer) or printer.state.status not in ACTIVE:
            return
        self.cameras.start_camera(self.name, printer.host, printer.access_code,
                                  printer.camera_port, printer.tls_fingerprints.get("camera", ""))
        self.cameras.touch(self.name)
        frame, captured = self.cameras.get_frame_with_time(self.name)
        if not frame or captured <= self.last_frame_at or time.monotonic() - captured > 15:
            self.publish(camera_error="Waiting for a fresh camera frame")
            return
        r = self.session.post(self.config["server"] + "/api/v1/octo/pic/",
                              headers={"Authorization": "Token " + self.config["auth_token"]},
                              files={"pic": ("snapshot.jpg", frame, "image/jpeg")},
                              data={"is_primary_camera": "true", "is_nozzle_camera": "false",
                                    "camera_name": self.name, "viewing_boost": "false"},
                              timeout=10, allow_redirects=False)
        if r.status_code != 200:
            self.publish(camera_error=f"Snapshot upload returned HTTP {r.status_code}")
            return
        self.last_frame_at = captured
        self.last_snapshot_at = time.time()
        self.publish(camera_error="")

    def run(self):
        backoff = 2
        try:
            while not self.stop_event.is_set():
                try:
                    linked_printer(self.config, self.session)
                    self.ws = websocket.create_connection(
                        websocket_url(self.config["server"]), timeout=10,
                        header={"authorization": "bearer " + self.config["auth_token"]},
                        enable_multithread=True)
                    self.ws.settimeout(1)
                    version = "1.0" + ("+" + self.config["registration_key"] if self.config.get("registration_key") else "")
                    self.send({"settings": {"agent": {"name": "the_print_farm", "version": version},
                                             "webcams": [{"name": self.name, "is_primary_camera": True,
                                                          "stream_mode": "snapshot", "streamRatio": "16:9",
                                                          "rotation": 0, "rotate90": False,
                                                          "flipV": False, "flipH": False,
                                                          "target_fps": 0.1}]}})
                    self.publish(connected=True, error="", state="Connected")
                    connected_at = time.monotonic()
                    last_status = last_pic = last_poll = last_ping = 0
                    while not self.stop_event.is_set():
                        p = self.farm.get_printer(self.name)
                        if p is None:
                            return
                        now = time.monotonic()
                        if now - connected_at >= 30:
                            backoff = 2
                        if now - last_status >= 5:
                            if (self.fresh(p) and p.state.status != PrintStatus.UNKNOWN
                                    and (p.state.status not in ACTIVE or p.state.subtask_name or p.state.gcode_file)):
                                messages, ledger = self.ledger.messages(p.state, time.time())
                                for msg in messages:
                                    self.send(msg)
                                self.ledger.save(ledger)
                                self.publish(printer_connected=True)
                            else:
                                self.send({})  # unknown, do not end a print
                                self.publish(printer_connected=False)
                            last_status = now
                        if now - last_pic >= 10:
                            self.post_snapshot(p)
                            last_pic = now
                        if now - last_poll >= 30:
                            result = self.reader.fetch_status()
                            # Separate account/API failures from the live agent socket.
                            result["results_connected"] = result.pop("connected", False)
                            if not result["results_connected"]:
                                result["normalized_p"] = None
                            self.publish(**result, connected=True)
                            last_poll = now
                        if now - last_ping >= 25:
                            self.ws.ping()
                            last_ping = now
                        try:
                            opcode, payload = self.ws.recv_data(control_frame=True)
                            if opcode == websocket.ABNF.OPCODE_CLOSE:
                                if len(payload) >= 2 and int.from_bytes(payload[:2], "big") == 4321:
                                    self.publish(connected=False, error="Agent token is already in use; use a separate token per printer")
                                    return
                                raise websocket.WebSocketConnectionClosedException()
                            if opcode == websocket.ABNF.OPCODE_TEXT:
                                self.receive(json.loads(payload))
                        except websocket.WebSocketTimeoutException:
                            pass
                except Exception as exc:
                    # Never log remote bodies, headers, credentials or exception URLs.
                    self.publish(connected=False, error="Obico connection failed (" + type(exc).__name__ + ")")
                    logger.warning("[%s] Obico bridge disconnected (%s)", self.name, type(exc).__name__)
                finally:
                    if self.ws:
                        self.ws.close()
                        self.ws = None
                if self.stop_event.wait(backoff):
                    break
                backoff = min(backoff * 2, 60)
        finally:
            self.session.close()
            if self.reader._session:
                self.reader._session.close()
            self.publish(connected=False)


class BambuObicoManager:
    def __init__(self, farm, cameras, state_dir):
        self.farm, self.cameras, self.state_dir = farm, cameras, state_dir
        self.bridges = {}
        self.lock = threading.Lock()

    def configure(self, name, config):
        if config:
            from .obico_settings import prepare_obico_config
            config = prepare_obico_config(config, {}, "bambulab")
        with self.lock:
            old = self.bridges.get(name)
            if old and not old.stop():
                raise RuntimeError("Previous Obico connection is still stopping; retry shortly")
            self.bridges.pop(name, None)
            printer = self.farm.get_printer(name)
            if printer:
                printer.set_obico_status({})
            if config:
                bridge = BambuObicoBridge(name, config, self.farm, self.cameras, self.state_dir)
                self.bridges[name] = bridge
                bridge.start()

    def stop_all(self):
        with self.lock:
            for bridge in self.bridges.values():
                bridge.stop()
            self.bridges.clear()
