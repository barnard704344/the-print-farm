"""
Flask Web Server + REST API for The Print Farm.

Provides a dashboard and API endpoints to monitor printers,
manage the job queue, and control individual printers.
"""

import logging
import os
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time
import uuid
from functools import wraps
from typing import Optional

import yaml
from flask import Flask, Response, jsonify, render_template, request, send_from_directory, session
from werkzeug.utils import secure_filename

from .discovery import discover_printers, scan_subnet, get_local_subnets, test_bambu_connection, test_klipper_connection, scan_moonraker_port
from .gcode_to_3mf import wrap_gcode_as_3mf, parse_gcode_filaments, parse_gcode_model_name
from .ldap_auth import authenticate_user, test_ad_connection, lookup_user
from .file_library import parse_gcode_metadata
from .api_v1 import create_api_v1
from .plate_detection import analyse_plate

logger = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = {"gcode", "3mf"}


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def create_app(farm_manager, job_queue, camera_manager=None, api_key=None, admin_password=None, config=None, file_library=None, spoolman_client=None, vp_manager=None):
    """Create the Flask app with references to farm manager, job queue, and camera manager."""
    app = Flask(
        __name__,
        template_folder=os.path.join(os.path.dirname(__file__), "..", "templates"),
        static_folder=os.path.join(os.path.dirname(__file__), "..", "static"),
    )
    app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024 * 1024  # 10GB max upload
    app.secret_key = secrets.token_hex(32)

    # Full config reference for AD settings management
    if config is None:
        config = {}
    app_config = config
    _vp_manager = vp_manager  # VirtualPrinterManager, may be None
    prefix = os.environ.get("APP_PREFIX", "/the-print-farm")

    def _get_ad_config():
        return app_config.get("active_directory", {})

    def _ad_enabled():
        return _get_ad_config().get("enabled", False)

    def _get_student_access_config():
        return app_config.get("student_access", {})

    def _bambu_uses_raised_bed(printer_name):
        if farm_manager.get_printer_type(printer_name) != "bambulab":
            return False
        cfg = next((p for p in app_config.get("printers", []) if p.get("name") == printer_name), {})
        text = " ".join(str(cfg.get(k, "")) for k in ("name", "model", "printer_model", "product")).upper()
        if re.search(r"\bA1(?:\b|[-_\s])", text):
            return False
        if re.search(r"\b(?:P1|X1)[A-Z0-9-]*\b", text):
            return True
        return False

    def _plate_detection_root():
        root = os.path.join(os.path.dirname(__file__), "..", "data", "plate_detection")
        os.makedirs(root, exist_ok=True)
        return root

    def _safe_printer_dir_name(printer_name):
        return re.sub(r"[^a-zA-Z0-9_.-]", "_", printer_name)

    def _plate_detection_dir(printer_name):
        path = os.path.join(_plate_detection_root(), _safe_printer_dir_name(printer_name))
        os.makedirs(path, exist_ok=True)
        return path

    def _get_plate_detection_config(printer_name):
        cfg = app_config.get("plate_detection", {}).get(printer_name, {})
        needs_raised_check = _bambu_uses_raised_bed(printer_name)
        return {
            "enabled": bool(cfg.get("enabled", False)),
            "threshold": float(cfg.get("threshold", 12.0)),
            "roi": {
                "x": float((cfg.get("roi") or {}).get("x", 0)),
                "y": float((cfg.get("roi") or {}).get("y", 0)),
                "w": float((cfg.get("roi") or {}).get("w", 100)),
                "h": float((cfg.get("roi") or {}).get("h", 100)),
            },
            "prepare_before_check": bool(cfg.get("prepare_before_check", needs_raised_check)),
            "raised_bed_required": needs_raised_check,
            "inspection_z": max(0.0, min(250.0, float(cfg.get("inspection_z", 0.0)))),
            "settle_seconds": max(0.0, min(10.0, float(cfg.get("settle_seconds", 2.0)))),
        }

    def _plate_reference_prefix(phase):
        if phase == "rest":
            return "rest_reference_"
        if phase == "inspection":
            return "inspection_reference_"
        return "reference_"

    def _list_plate_references(printer_name, phase=None):
        ref_dir = _plate_detection_dir(printer_name)
        names = []
        for f in os.listdir(ref_dir):
            if not f.lower().endswith((".jpg", ".jpeg")):
                continue
            if phase == "rest" and f.startswith(_plate_reference_prefix("rest")):
                names.append(f)
            elif phase == "inspection" and (
                f.startswith(_plate_reference_prefix("inspection")) or f.startswith(_plate_reference_prefix(None))
            ):
                names.append(f)
            elif phase is None and (
                f.startswith(_plate_reference_prefix("rest"))
                or f.startswith(_plate_reference_prefix("inspection"))
                or f.startswith(_plate_reference_prefix(None))
            ):
                names.append(f)
        return sorted(names)

    def _load_plate_reference_bytes(printer_name, phase=None):
        ref_dir = _plate_detection_dir(printer_name)
        refs = []
        for name in _list_plate_references(printer_name, phase):
            with open(os.path.join(ref_dir, name), "rb") as f:
                refs.append(f.read())
        return refs

    def _current_camera_frame(printer_name, wait_seconds=0.0, after=None):
        if not camera_manager:
            return None
        if wait_seconds:
            time.sleep(wait_seconds)
        if after is not None:
            frame = camera_manager.get_frame_after(printer_name, after, timeout=0.5)
        else:
            frame = camera_manager.get_frame(printer_name)
        if frame:
            return frame

        printer = farm_manager.get_printer(printer_name)
        if not printer:
            return None
        try:
            if farm_manager.get_printer_type(printer_name) == "klipper":
                camera_url = getattr(printer, "camera_url", "") or _detect_klipper_webcam(printer)
                if not camera_url:
                    return None
                camera_manager.start_http_camera(printer_name, camera_url)
            else:
                camera_manager.start_camera(
                    printer_name,
                    printer.host,
                    printer.access_code,
                    getattr(printer, "camera_port", 6000),
                )
            if after is not None:
                return camera_manager.get_frame_after(printer_name, after, timeout=6.0)
            for _ in range(10):
                time.sleep(0.5)
                frame = camera_manager.get_frame(printer_name)
                if frame:
                    return frame
        except Exception as e:
            logger.warning(f"Could not start camera for plate detection on {printer_name}: {e}")
        return None

    def _prepare_plate_detection_view(printer_name, cfg, home=False):
        if not cfg.get("prepare_before_check"):
            return {"ok": True, "skipped": True}
        if farm_manager.get_printer_type(printer_name) != "bambulab":
            return {"ok": True, "skipped": True}

        printer = farm_manager.get_printer(printer_name)
        if not printer:
            return {"ok": False, "message": f"Printer '{printer_name}' not found"}
        if not printer.is_connected():
            return {"ok": False, "message": f"Printer '{printer_name}' not connected"}
        if not hasattr(printer, "prepare_build_plate_inspection"):
            return {"ok": True, "skipped": True}

        move_started = time.monotonic()
        ok = printer.prepare_build_plate_inspection(cfg.get("inspection_z", 0.0), home=home)
        if not ok:
            return {"ok": False, "message": "Could not move Bambu build plate to inspection height"}
        time.sleep(cfg.get("settle_seconds", 2.0))
        return {"ok": True, "after": move_started}

    def _check_build_plate_clear(printer_name):
        cfg = _get_plate_detection_config(printer_name)
        if not cfg.get("enabled"):
            return {"ok": True, "skipped": True, "message": "Build plate detection disabled"}

        is_bambu = farm_manager.get_printer_type(printer_name) == "bambulab"
        should_prepare = is_bambu and cfg.get("prepare_before_check")

        frame = _current_camera_frame(printer_name)
        if not frame:
            return {"ok": False, "occupied": False, "message": "No camera snapshot available for build plate detection"}

        if should_prepare:
            rest_refs = _load_plate_reference_bytes(printer_name, "rest")
            if not rest_refs:
                return {
                    "ok": False,
                    "occupied": False,
                    "message": "Capture a Bambu resting-bed reference before enabling raised plate inspection",
                }
            rest_result = analyse_plate(frame, rest_refs, cfg["roi"], cfg["threshold"])
            if not rest_result.ok or rest_result.occupied:
                return {
                    "ok": rest_result.ok and not rest_result.occupied,
                    "occupied": rest_result.occupied,
                    "score": rest_result.score,
                    "threshold": rest_result.threshold,
                    "phase": "rest",
                    "message": rest_result.message,
                }

            prep = _prepare_plate_detection_view(printer_name, cfg)
            if not prep.get("ok"):
                return {"ok": False, "occupied": False, "message": prep.get("message", "Build plate inspection setup failed")}

            frame = _current_camera_frame(printer_name, wait_seconds=0.5, after=prep.get("after"))
            if not frame:
                return {"ok": False, "occupied": False, "message": "No camera snapshot available after moving bed"}
            refs = _load_plate_reference_bytes(printer_name, "inspection")
        else:
            refs = _load_plate_reference_bytes(printer_name, "inspection")

        result = analyse_plate(frame, refs, cfg["roi"], cfg["threshold"])
        return {
            "ok": result.ok and not result.occupied,
            "occupied": result.occupied,
            "score": result.score,
            "threshold": result.threshold,
            "phase": "inspection" if should_prepare else "single",
            "message": result.message,
        }

    def _test_current_plate_view(printer_name):
        cfg = _get_plate_detection_config(printer_name)
        if not cfg.get("enabled"):
            return {"ok": True, "skipped": True, "message": "Build plate detection disabled"}
        frame = _current_camera_frame(printer_name)
        if not frame:
            return {"ok": False, "occupied": False, "message": "No camera snapshot available for build plate detection"}

        checks = []
        phases = ["inspection"]
        if cfg.get("prepare_before_check"):
            phases = ["rest", "inspection"]

        for phase in phases:
            refs = _load_plate_reference_bytes(printer_name, phase)
            result = analyse_plate(frame, refs, cfg["roi"], cfg["threshold"])
            checks.append({
                "phase": phase,
                "ok": result.ok and not result.occupied,
                "occupied": result.occupied,
                "score": result.score,
                "threshold": result.threshold,
                "message": result.message,
            })

        ok_checks = [c for c in checks if c["ok"]]
        ok = bool(ok_checks)
        occupied = not ok
        if ok_checks:
            primary = min(ok_checks, key=lambda c: c["score"])
            message = f"Current camera view matches the {primary.get('phase')} empty reference"
        else:
            primary = max(checks, key=lambda c: c["score"]) if checks else {}
            message = primary.get("message", "Build plate check failed")
        return {
            "ok": ok,
            "occupied": occupied,
            "score": primary.get("score"),
            "threshold": primary.get("threshold", cfg["threshold"]),
            "phase": primary.get("phase"),
            "checks": checks,
            "motion": False,
            "message": message,
        }

    def _test_plate_reference_images(printer_name):
        cfg = _get_plate_detection_config(printer_name)
        phases = ["rest", "inspection"] if cfg.get("prepare_before_check") else ["inspection"]
        checks = []

        for phase in phases:
            refs = _load_plate_reference_bytes(printer_name, phase)
            if not refs:
                checks.append({
                    "phase": phase,
                    "ok": False,
                    "score": None,
                    "threshold": cfg["threshold"],
                    "count": 0,
                    "message": f"No {phase} reference image captured",
                })
                continue

            scores = []
            for idx, ref in enumerate(refs):
                others = refs[:idx] + refs[idx + 1:]
                compare_to = others or [ref]
                result = analyse_plate(ref, compare_to, cfg["roi"], cfg["threshold"])
                scores.append(result.score)

            score = max(scores) if scores else 0.0
            ok = score <= cfg["threshold"]
            checks.append({
                "phase": phase,
                "ok": ok,
                "score": score,
                "threshold": cfg["threshold"],
                "count": len(refs),
                "message": f"{phase.capitalize()} reference image ready" if ok else f"{phase.capitalize()} reference images differ too much",
            })

        ok = all(c["ok"] for c in checks)
        return {
            "ok": ok,
            "checks": checks,
            "motion": False,
            "message": "Reference images are ready" if ok else "Reference image check needs attention",
        }

    def _notify_plate_blocked(printer_name, job_id, message):
        from .notifications import NotificationManager
        NotificationManager(app_config).notify(
            "plate_blocked",
            f"Build plate check blocked job #{job_id}",
            f"Job #{job_id} was not sent to {printer_name}.\n{message}",
        )

    def _normalise_access_name(value):
        return re.sub(r"\s+", " ", str(value or "").strip()).lower()

    def _access_entries(values):
        if not isinstance(values, list):
            return set()
        return {_normalise_access_name(v) for v in values if _normalise_access_name(v)}

    def _current_access_names():
        names = {
            _normalise_access_name(session.get("username", "")),
            _normalise_access_name(session.get("display_name", "")),
        }
        return {n for n in names if n}

    def _current_user_in_entries(values):
        entries = _access_entries(values)
        return bool(entries and _current_access_names().intersection(entries))

    def _is_student_banned():
        if is_admin():
            return False
        return _current_user_in_entries(_get_student_access_config().get("banlist", []))

    def has_print_access():
        if is_admin() or _check_api_key():
            return True
        if not is_authenticated() or session.get("role") != "student":
            return False
        access = _get_student_access_config()
        if _current_user_in_entries(access.get("banlist", [])):
            return False
        return _current_user_in_entries(access.get("allowlist", []))

    def _print_access_denied_response():
        if not is_authenticated() and not _check_api_key():
            return jsonify({"error": "Login required"}), 401
        if _is_student_banned():
            return jsonify({"error": "Print access has been removed for this student"}), 403
        return jsonify({"error": "Student print access has not been approved"}), 403

    def _is_staff_only_printer(printer_name):
        """Check if a printer is restricted to staff only."""
        for p in app_config.get("printers", []):
            if p.get("name") == printer_name:
                return p.get("staff_only", False)
        return False

    def _save_config():
        """Write the current app_config to the YAML file."""
        config_path = os.environ.get("FARM_CONFIG", "config/config.yaml")
        with open(config_path, "w") as f:
            yaml.dump(app_config, f, default_flow_style=False, sort_keys=False)

    def _next_orca_port():
        """Return the next available OrcaSlicer port (starting at 5001)."""
        used = {p.get("orca_port") for p in app_config.get("printers", []) if p.get("orca_port")}
        port = 5001
        while port in used:
            port += 1
        return port

    def _local_ipv4_addresses():
        """Return IPv4 addresses currently bound on this host."""
        addresses = set()
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                ip = info[4][0]
                if ip and not ip.startswith("127."):
                    addresses.add(ip)
        except Exception:
            pass
        try:
            result = subprocess.run(
                ["ip", "-o", "-4", "addr", "show"],
                capture_output=True,
                text=True,
                timeout=2,
            )
            for match in re.finditer(r"\binet\s+([0-9.]+)/", result.stdout):
                ip = match.group(1)
                if ip and not ip.startswith("127."):
                    addresses.add(ip)
        except Exception:
            pass
        return addresses

    def _filter_discovery_results(printers):
        """Hide configured printers and this host's virtual Bambu endpoints."""
        configured_serials = {
            str(p.get("serial", "")).strip()
            for p in app_config.get("printers", [])
            if str(p.get("serial", "")).strip()
        }
        configured_hosts = {
            str(p.get("host", "")).strip()
            for p in app_config.get("printers", [])
            if str(p.get("host", "")).strip()
        }
        local_ips = _local_ipv4_addresses()
        filtered = []
        for p in printers:
            host = str(p.get("host", "")).strip()
            serial = str(p.get("serial", "")).strip()
            if host in local_ips:
                continue
            if host in configured_hosts:
                continue
            if serial and serial in configured_serials:
                continue
            filtered.append(p)
        return filtered

    def _sudo_cmd(args):
        """Run a command with sudo if available, otherwise directly."""
        sudo = shutil.which("sudo")
        if sudo:
            return subprocess.run([sudo, "-n"] + args, capture_output=True)
        return subprocess.run(args, capture_output=True)

    def _write_file_privileged(path, content):
        """Write a file using sudo tee if direct write fails."""
        try:
            with open(path, "w") as f:
                f.write(content)
        except PermissionError:
            sudo = shutil.which("sudo")
            if sudo:
                subprocess.run(
                    [sudo, "tee", path],
                    input=content.encode(), capture_output=True,
                )
            else:
                raise

    def _read_file_privileged(path):
        """Read a file using sudo cat if direct read fails."""
        try:
            with open(path) as f:
                return f.read()
        except PermissionError:
            sudo = shutil.which("sudo")
            if sudo:
                r = subprocess.run([sudo, "cat", path], capture_output=True)
                return r.stdout.decode()
            raise

    def _append_file_privileged(path, content):
        """Append to a file using sudo tee -a if direct write fails."""
        try:
            with open(path, "a") as f:
                f.write(content)
        except PermissionError:
            sudo = shutil.which("sudo")
            if sudo:
                subprocess.run(
                    [sudo, "tee", "-a", path],
                    input=content.encode(), capture_output=True,
                )
            else:
                raise

    def _remove_file_privileged(path):
        """Remove a file using sudo rm if direct remove fails."""
        try:
            os.remove(path)
        except PermissionError:
            sudo = shutil.which("sudo")
            if sudo:
                subprocess.run([sudo, "rm", path], capture_output=True)
            else:
                raise

    def _create_orca_vhost(printer_name, port):
        """Create an Apache VirtualHost for a per-printer OrcaSlicer port."""
        safe_name = re.sub(r'[^a-zA-Z0-9_-]', '-', printer_name).lower()
        conf_name = f"printer-{safe_name}"
        conf_path = f"/etc/apache2/sites-available/{conf_name}.conf"
        web_port = app_config.get("web", {}).get("port", 5000)
        vhost = (
            f"<VirtualHost *:{port}>\n"
            f"    # OrcaSlicer per-printer proxy: {printer_name}\n"
            f"    ProxyPass /api http://127.0.0.1:{web_port}/{printer_name}/api\n"
            f"    ProxyPassReverse /api http://127.0.0.1:{web_port}/{printer_name}/api\n"
            f"    Header always set Access-Control-Allow-Origin \"*\"\n"
            f"</VirtualHost>\n"
        )
        try:
            _write_file_privileged(conf_path, vhost)
            # Add Listen directive if not already present
            ports_conf = "/etc/apache2/ports.conf"
            ports_content = _read_file_privileged(ports_conf)
            if f"Listen {port}" not in ports_content:
                _append_file_privileged(ports_conf, f"\nListen {port}\n")
            _sudo_cmd(["a2ensite", conf_name])
            _sudo_cmd(["systemctl", "reload", "apache2"])
            logger.info(f"Created Apache vhost for {printer_name} on port {port}")
        except Exception as e:
            logger.error(f"Failed to create Apache vhost for {printer_name}: {e}")

    def _remove_orca_vhost(printer_name, port):
        """Remove the Apache VirtualHost for a printer."""
        safe_name = re.sub(r'[^a-zA-Z0-9_-]', '-', printer_name).lower()
        conf_name = f"printer-{safe_name}"
        try:
            _sudo_cmd(["a2dissite", conf_name])
            conf_path = f"/etc/apache2/sites-available/{conf_name}.conf"
            if os.path.exists(conf_path):
                _remove_file_privileged(conf_path)
            # Remove Listen directive
            if port:
                ports_conf = "/etc/apache2/ports.conf"
                ports_content = _read_file_privileged(ports_conf)
                new_content = "\n".join(
                    line for line in ports_content.splitlines()
                    if line.strip() != f"Listen {port}"
                ) + "\n"
                _write_file_privileged(ports_conf, new_content)
            _sudo_cmd(["systemctl", "reload", "apache2"])
            logger.info(f"Removed Apache vhost for {printer_name}")
        except Exception as e:
            logger.error(f"Failed to remove Apache vhost for {printer_name}: {e}")

    def is_admin():
        """True when user has staff role (AD) or legacy admin session."""
        return session.get("role") == "staff" or session.get("admin") is True

    def is_authenticated():
        """True when user is logged in with any role."""
        return session.get("role") in ("staff", "student") or session.get("admin") is True

    def admin_required(f):
        """Require staff / legacy admin role, or a valid API key."""
        @wraps(f)
        def decorated(*args, **kwargs):
            if not is_admin() and not _check_api_key():
                return jsonify({"ok": False, "error": "Admin login required", "message": "Admin login required"}), 403
            return f(*args, **kwargs)
        return decorated

    def _check_api_key():
        """Check if a valid API key was provided in the request header."""
        if not api_key:
            return False
        return request.headers.get("X-Api-Key", "") == api_key

    def login_required(f):
        """Require any authenticated user (student or staff), or a valid API key."""
        @wraps(f)
        def decorated(*args, **kwargs):
            if not is_authenticated() and not _check_api_key():
                return jsonify({"ok": False, "error": "Login required", "message": "Login required"}), 401
            return f(*args, **kwargs)
        return decorated

    def print_access_required(f):
        """Require staff/admin/API key or an approved student account."""
        @wraps(f)
        def decorated(*args, **kwargs):
            if not has_print_access():
                return _print_access_denied_response()
            return f(*args, **kwargs)
        return decorated

    def _is_job_owner(job):
        """True when the current user submitted this job."""
        uname = session.get("username", "")
        return uname and job.get("submitted_by") == uname

    def owner_or_admin_required(f):
        """Require admin OR ownership of the job (job_id must be a route param)."""
        @wraps(f)
        def decorated(*args, **kwargs):
            if is_admin() or _check_api_key():
                return f(*args, **kwargs)
            if not is_authenticated():
                return jsonify({"error": "Login required"}), 401
            if not has_print_access():
                return _print_access_denied_response()
            job_id = kwargs.get("job_id")
            if job_id:
                job = job_queue.get_job(job_id)
                if job and _is_job_owner(job):
                    return f(*args, **kwargs)
            return jsonify({"error": "Not authorised"}), 403
        return decorated

    def _has_active_job_on_printer(printer_name):
        """True when the current user has an active job on the given printer."""
        uname = session.get("username", "")
        if not uname:
            return False
        for job in job_queue.get_active_jobs():
            if job.get("printer_name") == printer_name and job.get("submitted_by") == uname:
                return True
        return False

    def printer_owner_or_admin_required(f):
        """Require admin OR having an active job on the printer (name must be a route param)."""
        @wraps(f)
        def decorated(*args, **kwargs):
            if is_admin() or _check_api_key():
                return f(*args, **kwargs)
            if not is_authenticated():
                return jsonify({"error": "Login required"}), 401
            name = kwargs.get("name")
            if name and _has_active_job_on_printer(name):
                return f(*args, **kwargs)
            return jsonify({"error": "Not authorised"}), 403
        return decorated

    @app.route(prefix + "/")
    @app.route(prefix)
    @app.route("/")
    def dashboard():
        return render_template("dashboard.html", prefix=prefix, api_key=api_key or "")

    @app.route(prefix + "/api/auth/status")
    @app.route("/api/auth/status")
    def auth_status():
        role = session.get("role")
        print_allowed = has_print_access()
        print_denied_reason = ""
        if is_authenticated() and not print_allowed and role == "student":
            print_denied_reason = (
                "Print access has been removed for this student"
                if _is_student_banned()
                else "Student print access has not been approved"
            )
        return jsonify({
            "admin": is_admin(),
            "authenticated": is_authenticated(),
            "role": role,
            "display_name": session.get("display_name", ""),
            "username": session.get("username", ""),
            "ad_enabled": _ad_enabled(),
            "has_local_users": bool(app_config.get("local_users")),
            "print_allowed": print_allowed,
            "print_denied_reason": print_denied_reason,
        })

    @app.route(prefix + "/api/auth/login", methods=["POST"])
    @app.route("/api/auth/login", methods=["POST"])
    def auth_login():
        data = request.get_json(silent=True) or {}
        username = data.get("username", "").strip()
        password = data.get("password", "")

        # Check local users first (works regardless of AD)
        local_users = app_config.get("local_users") or []
        for lu in local_users:
            if lu.get("username") == username and lu.get("password") == password:
                role = lu.get("role", "staff")
                session["role"] = role
                session["display_name"] = lu.get("display_name", username)
                session["username"] = username
                if role == "staff":
                    session["admin"] = True
                return jsonify({"ok": True, "role": role, "display_name": session["display_name"]})

        if _ad_enabled():
            # AD login
            if not username or not password:
                return jsonify({"ok": False, "error": "Username and password required"}), 400
            result = authenticate_user(username, password, _get_ad_config())
            if result["ok"]:
                session["role"] = result["role"]
                session["display_name"] = result.get("display_name", username)
                session["username"] = result.get("username", username)
                session.pop("admin", None)
                return jsonify({"ok": True, "role": result["role"], "display_name": result.get("display_name", username)})
            return jsonify({"ok": False, "error": result.get("error", "Authentication failed")}), 401
        else:
            # Legacy single-password login (no username needed)
            if admin_password and password == admin_password:
                session["admin"] = True
                session["role"] = "staff"
                return jsonify({"ok": True, "role": "staff"})
            return jsonify({"ok": False, "error": "Invalid credentials"}), 401

    @app.route(prefix + "/api/auth/logout", methods=["POST"])
    @app.route("/api/auth/logout", methods=["POST"])
    def auth_logout():
        session.pop("admin", None)
        session.pop("role", None)
        session.pop("display_name", None)
        session.pop("username", None)
        return jsonify({"ok": True})

    @app.route(prefix + "/api/auth/sso", methods=["POST"])
    @app.route("/api/auth/sso", methods=["POST"])
    def auth_sso():
        """SSO login — accepts username from Apache GSSAPI-verified PHP check."""
        if not _ad_enabled():
            return jsonify({"ok": False, "error": "AD not enabled"}), 400

        data = request.get_json(silent=True) or {}
        username = data.get("username", "").strip().lower()
        if not username:
            return jsonify({"ok": False, "error": "No username provided"}), 401

        # Verify user in AD and determine role (prevents spoofed usernames)
        result = lookup_user(username, _get_ad_config())
        if not result["ok"]:
            logger.warning(f"SSO lookup failed for {username}: {result.get('error')}")
            return jsonify({"ok": False, "error": result.get("error", "SSO lookup failed")}), 401

        session["role"] = result["role"]
        session["display_name"] = result.get("display_name", username)
        session["username"] = result.get("username", username)
        session.pop("admin", None)
        if result["role"] == "staff":
            session["admin"] = True

        logger.info(f"SSO auth: {username} -> role={result['role']}")
        return jsonify({"ok": True, "role": result["role"], "display_name": result.get("display_name", username)})

    @app.route(prefix + "/static/<path:filename>")
    def prefixed_static(filename):
        return send_from_directory(app.static_folder, filename)

    # ── Farm API ──────────────────────────────────────────

    def _get_printer_orca_port(printer_name):
        """Get the OrcaSlicer port for a printer from config."""
        for p in app_config.get("printers", []):
            if p.get("name") == printer_name:
                return p.get("orca_port")
        return None

    def _get_printer_config_fields(printer_name):
        """Return virtual_ip (live from vp_manager), serial, and access_code."""
        serial = ""
        access_code = ""
        for p in app_config.get("printers", []):
            if p.get("name") == printer_name:
                serial = p.get("serial", "")
                access_code = p.get("access_code", "")
                break
        # Live virtual IP from running server (DHCP-assigned)
        virtual_ip = None
        if _vp_manager:
            for srv in _vp_manager._servers:
                if srv.printer_name == printer_name:
                    virtual_ip = srv.virtual_ip
                    break
        return {"virtual_ip": virtual_ip, "serial": serial, "access_code": access_code}

    @app.route(prefix + "/api/farm/status")
    @app.route("/api/farm/status")
    def farm_status():
        """Full status of all printers + farm summary."""
        states = farm_manager.get_all_states()
        # Merge config fields into each printer state
        staff = is_admin()
        for name in states:
            states[name]["staff_only"] = _is_staff_only_printer(name)
            states[name]["orca_port"] = _get_printer_orca_port(name)
            cfg = _get_printer_config_fields(name)
            if staff:
                states[name].update(cfg)
            else:
                # Do not expose virtual_ip / serial / access_code to students
                states[name]["virtual_ip"] = None
                states[name]["serial"] = ""
                states[name]["access_code"] = ""
        return jsonify({
            "summary": farm_manager.get_farm_summary(),
            "printers": states,
        })

    @app.route(prefix + "/api/farm/summary")
    @app.route("/api/farm/summary")
    def farm_summary():
        return jsonify(farm_manager.get_farm_summary())

    # ── Printer API ───────────────────────────────────────

    @app.route(prefix + "/api/printer/<name>/status")
    @app.route("/api/printer/<name>/status")
    def printer_status(name):
        states = farm_manager.get_all_states()
        if name not in states:
            return jsonify({"error": "Printer not found"}), 404
        return jsonify(states[name])

    @app.route(prefix + "/api/printer/<name>/pause", methods=["POST"])
    @app.route("/api/printer/<name>/pause", methods=["POST"])
    @printer_owner_or_admin_required
    def printer_pause(name):
        client = farm_manager.get_printer(name)
        if not client:
            return jsonify({"error": "Printer not found"}), 404
        ok = client.pause_print()
        return jsonify({"ok": ok})

    @app.route(prefix + "/api/printer/<name>/resume", methods=["POST"])
    @app.route("/api/printer/<name>/resume", methods=["POST"])
    @printer_owner_or_admin_required
    def printer_resume(name):
        client = farm_manager.get_printer(name)
        if not client:
            return jsonify({"error": "Printer not found"}), 404
        ok = client.resume_print()
        return jsonify({"ok": ok})

    @app.route(prefix + "/api/printer/<name>/stop", methods=["POST"])
    @app.route("/api/printer/<name>/stop", methods=["POST"])
    @printer_owner_or_admin_required
    def printer_stop(name):
        client = farm_manager.get_printer(name)
        if not client:
            return jsonify({"error": "Printer not found"}), 404
        ok = client.stop_print()
        # Mark any active job on this printer as cancelled (user-initiated stop)
        if ok:
            for job in job_queue.get_active_jobs():
                if job.get("printer_name") == name:
                    job_queue.cancel_job(job["id"])
        return jsonify({"ok": ok})

    @app.route(prefix + "/api/printer/<name>/light", methods=["POST"])
    @app.route("/api/printer/<name>/light", methods=["POST"])
    @admin_required
    def printer_light(name):
        client = farm_manager.get_printer(name)
        if not client:
            return jsonify({"error": "Printer not found"}), 404
        current = client.state.chamber_light
        ok = client.set_chamber_light(not current)
        return jsonify({"ok": ok, "light": not current})

    @app.route(prefix + "/api/printer/<name>/led", methods=["POST"])
    @app.route("/api/printer/<name>/led", methods=["POST"])
    @admin_required
    def printer_led(name):
        """Toggle a specific LED or output pin on a Klipper printer."""
        client = farm_manager.get_printer(name)
        if not client:
            return jsonify({"error": "Printer not found"}), 404
        if farm_manager.get_printer_type(name) != "klipper":
            return jsonify({"ok": False, "message": "LED control only available for Klipper printers"}), 400
        data = request.get_json(silent=True) or {}
        led_object = data.get("object", "")
        on = data.get("on")
        if not led_object:
            return jsonify({"error": "Missing 'object' parameter"}), 400
        # Validate the object is a known LED/pin on this printer
        known = [l["object"] for l in client.state.klipper_leds]
        if led_object not in known:
            return jsonify({"error": "Unknown LED object"}), 400
        if on is None:
            # Toggle based on current state
            current = next((l for l in client.state.klipper_leds if l["object"] == led_object), {})
            on = not current.get("on", False)
        ok = client.set_led(led_object, bool(on))
        return jsonify({"ok": ok, "on": bool(on)})

    @app.route(prefix + "/api/printer/<name>/fan_speed", methods=["POST"])
    @app.route("/api/printer/<name>/fan_speed", methods=["POST"])
    @admin_required
    def printer_fan_speed(name):
        """Set speed of a fan_generic on a Klipper printer."""
        client = farm_manager.get_printer(name)
        if not client:
            return jsonify({"error": "Printer not found"}), 404
        if farm_manager.get_printer_type(name) != "klipper":
            return jsonify({"ok": False, "message": "Fan control only available for Klipper printers"}), 400
        data = request.get_json(silent=True) or {}
        fan_object = data.get("object", "")
        speed = data.get("speed")
        if not fan_object or speed is None:
            return jsonify({"error": "Missing 'object' and/or 'speed' parameter"}), 400
        # Validate the object is a known controllable fan
        known = [f["object"] for f in client.state.klipper_fans if f.get("controllable")]
        if fan_object not in known:
            return jsonify({"error": "Unknown or non-controllable fan object"}), 400
        speed = max(0.0, min(1.0, float(speed)))
        ok = client.set_fan_speed(fan_object, speed)
        return jsonify({"ok": ok, "speed": speed})

    @app.route(prefix + "/api/printer/<name>/emergency_stop", methods=["POST"])
    @app.route("/api/printer/<name>/emergency_stop", methods=["POST"])
    @admin_required
    def printer_emergency_stop(name):
        """Emergency stop — Klipper only."""
        client = farm_manager.get_printer(name)
        if not client:
            return jsonify({"error": "Printer not found"}), 404
        if farm_manager.get_printer_type(name) != "klipper":
            return jsonify({"ok": False, "message": "Emergency stop only available for Klipper printers"}), 400
        ok = client.emergency_stop()
        return jsonify({"ok": ok})

    @app.route(prefix + "/api/printer/<name>/bed_temp", methods=["POST"])
    @app.route("/api/printer/<name>/bed_temp", methods=["POST"])
    @admin_required
    def printer_bed_temp(name):
        client = farm_manager.get_printer(name)
        if not client:
            return jsonify({"error": "Printer not found"}), 404
        data = request.get_json(silent=True) or {}
        temp = int(data.get("temp", 0))
        ok = client.set_bed_temperature(temp)
        return jsonify({"ok": ok, "temp": temp})

    @app.route(prefix + "/api/printer/<name>/nozzle_temp", methods=["POST"])
    @app.route("/api/printer/<name>/nozzle_temp", methods=["POST"])
    @admin_required
    def printer_nozzle_temp(name):
        client = farm_manager.get_printer(name)
        if not client:
            return jsonify({"error": "Printer not found"}), 404
        data = request.get_json(silent=True) or {}
        temp = int(data.get("temp", 0))
        heater = str(data.get("heater", "") or "")
        if farm_manager.get_printer_type(name) == "klipper":
            ok = client.set_nozzle_temperature(temp, heater=heater)
        else:
            ok = client.set_nozzle_temperature(temp)
        return jsonify({"ok": ok, "temp": temp, "heater": heater})

    @app.route(prefix + "/api/printer/<name>/unload_filament", methods=["POST"])
    @app.route("/api/printer/<name>/unload_filament", methods=["POST"])
    @admin_required
    def printer_unload_filament(name):
        client = farm_manager.get_printer(name)
        if not client:
            return jsonify({"error": "Printer not found"}), 404
        ok = client.unload_filament()
        return jsonify({"ok": ok})

    @app.route(prefix + "/api/printer/<name>/load_filament", methods=["POST"])
    @app.route("/api/printer/<name>/load_filament", methods=["POST"])
    @admin_required
    def printer_load_filament(name):
        client = farm_manager.get_printer(name)
        if not client:
            return jsonify({"error": "Printer not found"}), 404
        ok = client.load_filament()
        return jsonify({"ok": ok})

    @app.route(prefix + "/api/printer/<name>/ams_load", methods=["POST"])
    @app.route("/api/printer/<name>/ams_load", methods=["POST"])
    @admin_required
    def printer_ams_load(name):
        """Load filament from a specific AMS tray."""
        client = farm_manager.get_printer(name)
        if not client:
            return jsonify({"error": "Printer not found"}), 404
        if farm_manager.get_printer_type(name) == "klipper":
            return jsonify({"ok": False, "message": "AMS not supported on Klipper printers"}), 400
        data = request.get_json(silent=True) or {}
        tray_id = data.get("tray_id")
        if tray_id is None:
            return jsonify({"ok": False, "message": "tray_id required"}), 400
        ok = client.ams_load_tray(int(tray_id))
        return jsonify({"ok": ok})

    @app.route(prefix + "/api/printer/<name>/tray_config", methods=["POST"])
    @app.route("/api/printer/<name>/tray_config", methods=["POST"])
    @admin_required
    def printer_tray_config(name):
        """Set filament type/color for an AMS tray."""
        client = farm_manager.get_printer(name)
        if not client:
            return jsonify({"error": "Printer not found"}), 404
        if farm_manager.get_printer_type(name) == "klipper":
            return jsonify({"ok": False, "message": "AMS not supported on Klipper printers"}), 400
        data = request.get_json(silent=True) or {}
        tray_id = data.get("tray_id")
        tray_type = data.get("type", "PLA")
        color = data.get("color", "#FFFFFF")
        nozzle_temp_min = int(data.get("nozzle_temp_min", 190))
        nozzle_temp_max = int(data.get("nozzle_temp_max", 230))
        if tray_id is None:
            return jsonify({"ok": False, "message": "tray_id required"}), 400
        ok = client.set_tray_info(int(tray_id), tray_type, color, nozzle_temp_min, nozzle_temp_max)
        return jsonify({"ok": ok})

    # ── Job Queue API ─────────────────────────────────────

    @app.route(prefix + "/api/jobs", methods=["GET"])
    @app.route("/api/jobs", methods=["GET"])
    def list_jobs():
        return jsonify({
            "jobs": job_queue.get_all_jobs(),
            "stats": job_queue.get_stats(),
        })

    @app.route(prefix + "/api/jobs/queued")
    @app.route("/api/jobs/queued")
    def queued_jobs():
        return jsonify(job_queue.get_queued_jobs())

    @app.route(prefix + "/api/jobs/active")
    @app.route("/api/jobs/active")
    def active_jobs():
        return jsonify(job_queue.get_active_jobs())

    @app.route(prefix + "/api/jobs/<int:job_id>")
    @app.route("/api/jobs/<int:job_id>")
    def get_job(job_id):
        job = job_queue.get_job(job_id)
        if not job:
            return jsonify({"error": "Job not found"}), 404
        return jsonify(job)

    @app.route(prefix + "/api/jobs/upload", methods=["POST"])
    @app.route("/api/jobs/upload", methods=["POST"])
    @login_required
    @print_access_required
    def upload_job():
        """Upload a G-code/3MF file and add it to the queue."""
        if "file" not in request.files:
            return jsonify({"error": "No file provided"}), 400

        file = request.files["file"]
        if not file.filename or not allowed_file(file.filename):
            return jsonify({"error": "Invalid file type. Allowed: .gcode, .3mf"}), 400

        original_name = secure_filename(file.filename)
        unique_name = f"{uuid.uuid4().hex}_{original_name}"
        file_path = os.path.join(job_queue.upload_dir, unique_name)
        file.save(file_path)

        # If filename looks like an OrcaSlicer temp name (e.g. 97188.0.gcode),
        # try to extract the real model name from gcode metadata
        if re.match(r"^\d+\.\d+\.gcode$", original_name) and original_name.endswith(".gcode"):
            model_name = parse_gcode_model_name(file_path)
            if model_name:
                original_name = model_name + ".gcode"

        copies = int(request.form.get("copies", 1))
        priority = int(request.form.get("priority", 0))
        notes = request.form.get("notes", "")
        printer = request.form.get("printer", "")
        if printer and not (is_admin() or _check_api_key()) and _is_staff_only_printer(printer):
            return jsonify({"error": f"Printer '{printer}' is restricted to staff"}), 403
        meta = parse_gcode_metadata(file_path)

        job_id = job_queue.add_job(
            filename=unique_name,
            original_name=original_name,
            file_path=file_path,
            copies=copies,
            priority=priority,
            notes=notes,
            submitted_by=session.get("username", ""),
            print_time_seconds=meta.get("print_time_seconds"),
        )

        # Notify
        from .notifications import NotificationManager
        NotificationManager(app_config).notify(
            "job_submitted",
            f"New Job — {original_name}",
            f"Job #{job_id} submitted by {session.get('username', 'unknown')}.\nFile: {original_name}",
        )

        # Add to file library for persistent storage
        # Save uploaded thumbnail if provided
        uploaded_thumb_path = None
        if "thumbnail" in request.files:
            thumb = request.files["thumbnail"]
            if thumb.filename:
                thumb_dir = os.path.join(job_queue.upload_dir, "thumbnails")
                os.makedirs(thumb_dir, exist_ok=True)
                uploaded_thumb_path = os.path.join(thumb_dir, f"{unique_name}.thumb.png")
                thumb.save(uploaded_thumb_path)

        if file_library:
            try:
                file_library.add_file(
                    original_name=original_name,
                    stored_name=unique_name,
                    file_path=file_path,
                    file_size=os.path.getsize(file_path),
                    uploaded_by=session.get("username", ""),
                    metadata=meta,
                    thumbnail_override=uploaded_thumb_path,
                )
            except Exception as e:
                logger.warning(f"Failed to add file to library: {e}")

        # If a specific printer was requested, assign and send immediately
        if printer:
            plate_check = _check_build_plate_clear(printer)
            if not plate_check.get("ok"):
                _notify_plate_blocked(printer, job_id, plate_check.get("message", "Build plate check failed"))
                return jsonify({
                    "error": plate_check.get("message", "Build plate check failed"),
                    "plate_detection": plate_check,
                }), 409
            ok = job_queue.assign_job(job_id, printer)
            if ok:
                t = threading.Thread(target=_send_job_to_printer, args=(job_id, printer), daemon=True)
                t.start()

        return jsonify({"ok": True, "job_id": job_id})

    def _send_job_to_printer(job_id, printer_name):
        """Background task: upload file to printer and start print."""
        try:
            job = job_queue.get_job(job_id)
            if not job:
                logger.error(f"Send job #{job_id}: job not found")
                return
            printer = farm_manager.get_printer(printer_name)
            if not printer:
                logger.error(f"Send job #{job_id}: printer '{printer_name}' not found")
                job_queue.mark_failed(job_id)
                return

            file_path = job["file_path"]
            remote_name = job["filename"]
            printer_type = farm_manager.get_printer_type(printer_name)

            # Use original name for Klipper so the printer shows
            # a clean filename instead of the UUID-prefixed one.
            if printer_type == "klipper" and job.get("original_name"):
                remote_name = job["original_name"]

            # BambuLab printers need .gcode wrapped in .3mf
            # Klipper printers take raw .gcode directly
            if printer_type == "bambulab" and remote_name.lower().endswith(".gcode"):
                threemf_path = file_path + ".3mf"
                try:
                    wrap_gcode_as_3mf(file_path, threemf_path)
                    file_path = threemf_path
                    remote_name = remote_name.rsplit(".", 1)[0] + ".3mf"
                    logger.info(f"Wrapped gcode as 3mf: {remote_name}")
                except Exception as e:
                    logger.error(f"Failed to wrap gcode as 3mf: {e}")
                    job_queue.mark_failed(job_id)
                    return
            elif printer_type == "klipper" and remote_name.lower().endswith(".3mf"):
                # Klipper can't print .3mf files — need the raw gcode
                logger.error(f"Cannot send .3mf to Klipper printer '{printer_name}'")
                job_queue.mark_failed(job_id)
                return

            from .bambu_client import build_3mf_ams_mapping, read_3mf_first_extruder, sanitize_3mf_external_spool
            num_ams = len(printer.state.ams_trays) if printer.state.ams_trays else 4
            first_ext = read_3mf_first_extruder(file_path) if file_path.lower().endswith(".3mf") else None
            ams_mapping = build_3mf_ams_mapping(file_path, num_ams) if file_path.lower().endswith(".3mf") else None
            use_ams = True if ams_mapping else (None if (first_ext is None or first_ext < num_ams) else False)
            upload_path = sanitize_3mf_external_spool(file_path) if ams_mapping else file_path

            # Upload the file to the printer
            ok = printer.upload_file(upload_path, remote_name)
            if ok:
                # Wait for file to be ready before starting print
                time.sleep(2 if printer_type == "bambulab" else 0.5)
                started = printer.start_print(remote_name, use_ams=use_ams, ams_mapping=ams_mapping)
                if started:
                    job_queue.mark_printing(job_id)
                    logger.info(f"Started printing job #{job_id} on {printer_name}")
                else:
                    job_queue.mark_failed(job_id)
                    logger.error(f"Failed to start job #{job_id} on {printer_name}")
            else:
                job_queue.mark_failed(job_id)
                logger.error(f"Failed to upload job #{job_id} to {printer_name}")
        except Exception as e:
            logger.error(f"Send job #{job_id} to {printer_name} failed: {e}")
            try:
                job_queue.mark_failed(job_id)
            except Exception:
                pass

    @app.route(prefix + "/api/jobs/<int:job_id>/filaments")
    @app.route("/api/jobs/<int:job_id>/filaments")
    def job_filaments(job_id):
        """Get filament requirements for a job (parsed from gcode)."""
        job = job_queue.get_job(job_id)
        if not job:
            return jsonify({"error": "Job not found"}), 404
        file_path = job["file_path"]
        if not file_path.lower().endswith(".gcode"):
            return jsonify({"filaments": [], "used_slots": [], "used_filaments": []})
        try:
            info = parse_gcode_filaments(file_path)
            return jsonify(info)
        except Exception as e:
            logger.error(f"Failed to parse filaments for job #{job_id}: {e}")
            return jsonify({"filaments": [], "used_slots": [], "used_filaments": []})

    @app.route(prefix + "/api/jobs/<int:job_id>/check_filament", methods=["POST"])
    @app.route("/api/jobs/<int:job_id>/check_filament", methods=["POST"])
    def check_filament(job_id):
        """Check if a printer's AMS/MMU has the filaments a job needs.

        Returns match status and details for each required filament.
        """
        data = request.get_json(silent=True) or {}
        printer_name = data.get("printer")
        if not printer_name:
            return jsonify({"error": "printer required"}), 400

        job = job_queue.get_job(job_id)
        if not job:
            return jsonify({"error": "Job not found"}), 404

        printer = farm_manager.get_printer(printer_name)
        if not printer:
            return jsonify({"error": "Printer not found"}), 404

        # Parse gcode filament requirements
        file_path = job["file_path"]
        required = []
        if file_path.lower().endswith(".gcode"):
            try:
                info = parse_gcode_filaments(file_path)
                required = info.get("used_filaments", [])
            except Exception:
                pass

        if not required:
            return jsonify({"ok": True, "match": True, "details": [], "message": "No filament requirements detected"})

        printer_type = farm_manager.get_printer_type(printer_name)

        # ── Klipper printer ───────────────────────────────────
        if printer_type == "klipper":
            state = printer.state
            if not getattr(state, "has_mmu", False):
                # No AMS, no MMU — skip the filament check entirely
                return jsonify({
                    "ok": True, "match": True, "details": [],
                    "message": "Klipper printer without MMU — filament check skipped",
                })

            # MMU printer: use the state overlay (which already merges persisted gate
            # configs on top of whatever Happy Hare is currently reporting) so that
            # gate assignments are visible even after HH clears them post-print.
            all_states = farm_manager.get_all_states()
            mmu = all_states.get(printer_name, {}).get("mmu") or {}
            gates = mmu.get("gates", [])

            # Build a set of materials available across all non-empty gates.
            # A gate is considered available if its live status != 0 (empty) OR if
            # we have a persisted material for it (filament is still physically loaded).
            available_materials = set()
            for g in gates:
                gate_idx = g.get("gate", -1)
                live_status = g.get("status", 0)  # 0=empty, 1=unknown, 2=loaded
                material = (g.get("material") or "").upper().strip()
                # Also check persisted config directly in case live gate list is stale
                persisted = farm_manager.get_gate_config(printer_name, gate_idx)
                persisted_material = (persisted.get("material") or "").upper().strip()
                if live_status != 0 or persisted_material:
                    if material:
                        available_materials.add(material)
                    if persisted_material:
                        available_materials.add(persisted_material)

            details = []
            all_match = True
            for fil in required:
                slot = fil["slot"]
                needed_type = (fil["type"] or "").upper()
                needed_color = fil["color"][:7] if fil["color"] else ""

                if not needed_type or needed_type in available_materials:
                    details.append({
                        "slot": slot, "needed_type": needed_type,
                        "needed_color": needed_color,
                        "match": True, "reason": "",
                    })
                else:
                    all_match = False
                    available_list = ", ".join(sorted(available_materials)) or "none"
                    details.append({
                        "slot": slot, "needed_type": needed_type,
                        "needed_color": needed_color,
                        "match": False,
                        "reason": f"Slot {slot + 1}: need {needed_type}, gates have {available_list}",
                    })

            message = "All filaments match" if all_match else "Filament mismatch detected"
            return jsonify({"ok": True, "match": all_match, "details": details, "message": message})

        # ── BambuLab AMS printer ──────────────────────────────
        state = printer.state
        ams_trays = state.ams_trays or []

        details = []
        all_match = True
        for fil in required:
            slot = fil["slot"]
            needed_type = fil["type"]
            needed_color = fil["color"][:7] if fil["color"] else ""  # Strip alpha

            # Find matching AMS tray
            tray = next((t for t in ams_trays if t["id"] == slot), None)
            if not tray or not tray.get("loaded"):
                details.append({
                    "slot": slot,
                    "needed_type": needed_type,
                    "needed_color": needed_color,
                    "ams_type": None,
                    "ams_color": None,
                    "match": False,
                    "reason": f"Tray {slot + 1} is empty",
                })
                all_match = False
            else:
                type_match = (tray["type"].upper() == needed_type.upper()) if tray["type"] else False
                tray_color = (tray["color"] or "")[:7]
                color_match = tray_color.upper() == needed_color.upper() if needed_color and tray_color else True
                match = type_match  # Type must match; color is informational
                if not match:
                    all_match = False
                details.append({
                    "slot": slot,
                    "needed_type": needed_type,
                    "needed_color": needed_color,
                    "ams_type": tray["type"],
                    "ams_color": tray_color,
                    "match": match,
                    "reason": "" if match else f"Tray {slot + 1}: need {needed_type}, have {tray['type'] or 'unknown'}",
                })

        message = "All filaments match" if all_match else "Filament mismatch detected"
        return jsonify({"ok": True, "match": all_match, "details": details, "message": message})

    @app.route(prefix + "/api/jobs/<int:job_id>/assign", methods=["POST"])
    @app.route("/api/jobs/<int:job_id>/assign", methods=["POST"])
    @owner_or_admin_required
    def assign_job(job_id):
        data = request.get_json(silent=True) or {}
        printer_name = data.get("printer")
        printers = data.get("printers", [])
        job = job_queue.get_job(job_id)

        if not job:
            return jsonify({"error": "Job not found"}), 404

        file_path = job.get("file_path", "")
        if not file_path or not os.path.exists(file_path):
            return jsonify({"error": "Job source file is missing; re-upload the file to print again"}), 400

        # Support single printer (backward compat) or list
        if printer_name and not printers:
            printers = [printer_name]
        if not printers:
            return jsonify({"error": "printer or printers required"}), 400

        # Validate all printers first
        for pname in printers:
            p = farm_manager.get_printer(pname)
            if not p:
                return jsonify({"error": f"Printer '{pname}' not found"}), 404
            if not p.is_connected():
                return jsonify({"error": f"Printer '{pname}' not connected"}), 400
            if not (is_admin() or _check_api_key()) and _is_staff_only_printer(pname):
                return jsonify({"error": f"Printer '{pname}' is restricted to staff"}), 403
            plate_check = _check_build_plate_clear(pname)
            if not plate_check.get("ok"):
                _notify_plate_blocked(pname, job_id, plate_check.get("message", "Build plate check failed"))
                return jsonify({
                    "error": plate_check.get("message", "Build plate check failed"),
                    "plate_detection": plate_check,
                }), 409

        results = []
        # First printer gets the original job
        first = printers[0]
        ok = job_queue.assign_job(job_id, first)
        if ok:
            t = threading.Thread(target=_send_job_to_printer, args=(job_id, first), daemon=True)
            t.start()
            results.append({"printer": first, "job_id": job_id, "ok": True})
        else:
            results.append({"printer": first, "job_id": job_id, "ok": False})

        # Additional printers get cloned jobs
        for pname in printers[1:]:
            clone_id = job_queue.clone_job_for_printer(job_id)
            if clone_id:
                ok2 = job_queue.assign_job(clone_id, pname)
                if ok2:
                    t = threading.Thread(target=_send_job_to_printer, args=(clone_id, pname), daemon=True)
                    t.start()
                    results.append({"printer": pname, "job_id": clone_id, "ok": True})
                else:
                    results.append({"printer": pname, "job_id": clone_id, "ok": False})
            else:
                results.append({"printer": pname, "job_id": None, "ok": False})

        return jsonify({"ok": all(r["ok"] for r in results), "results": results})

    @app.route(prefix + "/api/jobs/<int:job_id>/reprint", methods=["POST"])
    @app.route("/api/jobs/<int:job_id>/reprint", methods=["POST"])
    @owner_or_admin_required
    def reprint_job(job_id):
        """Create a new copy of an existing job, optionally sending to printers."""
        data = request.get_json(silent=True) or {}
        printer_name = data.get("printer")
        printers = data.get("printers", [])
        original_job = job_queue.get_job(job_id)

        if not original_job:
            return jsonify({"error": "Job not found"}), 404

        source_path = original_job.get("file_path", "")
        if not source_path or not os.path.exists(source_path):
            return jsonify({"error": "Job source file is missing; re-upload the file to print again"}), 400

        # Support single printer (backward compat) or list
        if printer_name and not printers:
            printers = [printer_name]

        # Validate all printers first when immediate send is requested
        for pname in printers:
            p = farm_manager.get_printer(pname)
            if not p:
                return jsonify({"error": f"Printer '{pname}' not found"}), 404
            if not p.is_connected():
                return jsonify({"error": f"Printer '{pname}' not connected"}), 400
            if not (is_admin() or _check_api_key()) and _is_staff_only_printer(pname):
                return jsonify({"error": f"Printer '{pname}' is restricted to staff"}), 403
            plate_check = _check_build_plate_clear(pname)
            if not plate_check.get("ok"):
                _notify_plate_blocked(pname, job_id, plate_check.get("message", "Build plate check failed"))
                return jsonify({
                    "error": plate_check.get("message", "Build plate check failed"),
                    "plate_detection": plate_check,
                }), 409

        new_id = job_queue.reprint_job(job_id)
        if new_id is None:
            return jsonify({"error": "Job not found"}), 404

        # No printers selected: keep previous behavior (new queued job only)
        if not printers:
            return jsonify({"ok": True, "job_id": new_id})

        results = []
        first = printers[0]
        ok = job_queue.assign_job(new_id, first)
        if ok:
            t = threading.Thread(target=_send_job_to_printer, args=(new_id, first), daemon=True)
            t.start()
            results.append({"printer": first, "job_id": new_id, "ok": True})
        else:
            results.append({"printer": first, "job_id": new_id, "ok": False})

        # Additional printers get cloned jobs so multiple copies can run in parallel
        for pname in printers[1:]:
            clone_id = job_queue.clone_job_for_printer(new_id)
            if clone_id:
                ok2 = job_queue.assign_job(clone_id, pname)
                if ok2:
                    t = threading.Thread(target=_send_job_to_printer, args=(clone_id, pname), daemon=True)
                    t.start()
                    results.append({"printer": pname, "job_id": clone_id, "ok": True})
                else:
                    results.append({"printer": pname, "job_id": clone_id, "ok": False})
            else:
                results.append({"printer": pname, "job_id": None, "ok": False})

        return jsonify({"ok": all(r["ok"] for r in results), "job_id": new_id, "results": results})

    @app.route(prefix + "/api/jobs/<int:job_id>/cancel", methods=["POST"])
    @app.route("/api/jobs/<int:job_id>/cancel", methods=["POST"])
    @owner_or_admin_required
    def cancel_job(job_id):
        job = job_queue.get_job(job_id)
        if job and job["status"] == "printing" and job.get("printer_name"):
            printer = farm_manager.get_printer(job["printer_name"])
            if printer:
                printer.stop_print()
        ok = job_queue.cancel_job(job_id)
        return jsonify({"ok": ok})

    @app.route(prefix + "/api/jobs/<int:job_id>/requeue", methods=["POST"])
    @app.route("/api/jobs/<int:job_id>/requeue", methods=["POST"])
    @admin_required
    def requeue_job(job_id):
        job = job_queue.get_job(job_id)
        if not job:
            return jsonify({"error": "Job not found"}), 404

        file_path = job.get("file_path", "")
        if not file_path or not os.path.exists(file_path):
            return jsonify({"error": "Job source file is missing; re-upload the file to print again"}), 400

        ok = job_queue.requeue_job(job_id)
        return jsonify({"ok": ok})

    @app.route(prefix + "/api/jobs/<int:job_id>/delete", methods=["POST", "DELETE"])
    @app.route("/api/jobs/<int:job_id>/delete", methods=["POST", "DELETE"])
    @app.route(prefix + "/api/jobs/<int:job_id>", methods=["DELETE"])
    @app.route("/api/jobs/<int:job_id>", methods=["DELETE"])
    @admin_required
    def delete_job(job_id):
        job = job_queue.get_job(job_id)
        if job and job["status"] == "printing" and job.get("printer_name"):
            printer = farm_manager.get_printer(job["printer_name"])
            if printer:
                printer.stop_print()
        ok = job_queue.delete_job(job_id)
        # Optionally delete the matching library file
        delete_lib = request.args.get("delete_library", "").lower() == "true"
        if ok and delete_lib and file_library and job:
            file_path = job.get("file_path", "")
            if file_path:
                lib_file = file_library.find_by_path(file_path)
                if lib_file:
                    file_library.delete_file(lib_file["id"])
        return jsonify({"ok": ok})

    # ── File Library API ──────────────────────────────────

        @app.route(prefix + "/api/jobs/bulk_delete", methods=["POST"])
        @app.route("/api/jobs/bulk_delete", methods=["POST"])
        @admin_required
        def bulk_delete_jobs():
            data = request.get_json(silent=True) or {}
            ids = data.get("ids", [])
            delete_lib = bool(data.get("delete_library", False))
            if not ids or not isinstance(ids, list):
                return jsonify({"ok": False, "error": "No ids provided"}), 400
            deleted = 0
            for job_id in ids:
                try:
                    job_id = int(job_id)
                except (TypeError, ValueError):
                    continue
                job = job_queue.get_job(job_id)
                if job and job["status"] == "printing" and job.get("printer_name"):
                    printer = farm_manager.get_printer(job["printer_name"])
                    if printer:
                        printer.stop_print()
                ok = job_queue.delete_job(job_id)
                if ok:
                    deleted += 1
                    if delete_lib and file_library and job:
                        file_path = job.get("file_path", "")
                        if file_path:
                            lib_file = file_library.find_by_path(file_path)
                            if lib_file:
                                file_library.delete_file(lib_file["id"])
            return jsonify({"ok": True, "deleted": deleted})

    @app.route(prefix + "/api/library/files")
    @app.route("/api/library/files")
    @login_required
    def library_list_files():
        if not file_library:
            return jsonify({"files": [], "folders": []})
        folder_id = request.args.get("folder_id")
        if folder_id is not None:
            folder_id = int(folder_id)
        files = file_library.get_files(folder_id)
        folders = file_library.get_folders(folder_id)
        return jsonify({"files": files, "folders": folders})

    @app.route(prefix + "/api/library/files/search")
    @app.route("/api/library/files/search")
    @login_required
    def library_search_files():
        if not file_library:
            return jsonify({"files": []})
        q = request.args.get("q", "")
        return jsonify({"files": file_library.search_files(q)})

    @app.route(prefix + "/api/library/files/<int:file_id>")
    @app.route("/api/library/files/<int:file_id>")
    @login_required
    def library_get_file(file_id):
        if not file_library:
            return jsonify({"error": "Library not available"}), 500
        f = file_library.get_file(file_id)
        if not f:
            return jsonify({"error": "File not found"}), 404
        return jsonify(f)

    @app.route(prefix + "/api/library/files/<int:file_id>/thumbnail")
    @app.route("/api/library/files/<int:file_id>/thumbnail")
    @login_required
    def library_file_thumbnail(file_id):
        if not file_library:
            return jsonify({"error": "Library not available"}), 500
        f = file_library.get_file(file_id)
        if not f or not f.get("thumbnail_path"):
            return Response(status=404)
        thumb_path = f["thumbnail_path"]
        if not os.path.exists(thumb_path):
            return Response(status=404)
        abs_path = os.path.abspath(thumb_path)
        return send_from_directory(
            os.path.dirname(abs_path),
            os.path.basename(abs_path),
            mimetype="image/png",
        )

    @app.route(prefix + "/api/library/files/<int:file_id>/toolpath")
    @app.route("/api/library/files/<int:file_id>/toolpath")
    @login_required
    def library_file_toolpath(file_id):
        if not file_library:
            return jsonify({"error": "Library not available"}), 500
        data = file_library.get_toolpath_data(file_id)
        if not data:
            return jsonify({"error": "No toolpath data available"}), 404
        return jsonify(data)

    @app.route(prefix + "/api/library/files/<int:file_id>/move", methods=["POST"])
    @app.route("/api/library/files/<int:file_id>/move", methods=["POST"])
    @login_required
    def library_move_file(file_id):
        if not file_library:
            return jsonify({"error": "Library not available"}), 500
        data = request.get_json(silent=True) or {}
        folder_id = data.get("folder_id")  # None = root
        return jsonify(file_library.move_file(file_id, folder_id))

    @app.route(prefix + "/api/library/files/<int:file_id>/delete", methods=["POST", "DELETE"])
    @app.route("/api/library/files/<int:file_id>/delete", methods=["POST", "DELETE"])
    @app.route(prefix + "/api/library/files/<int:file_id>", methods=["DELETE"])
    @app.route("/api/library/files/<int:file_id>", methods=["DELETE"])
    @admin_required
    def library_delete_file(file_id):
        if not file_library:
            return jsonify({"error": "Library not available"}), 500
        return jsonify(file_library.delete_file(file_id))

    @app.route(prefix + "/api/library/files/<int:file_id>/print", methods=["POST"])
    @app.route("/api/library/files/<int:file_id>/print", methods=["POST"])
    @login_required
    @print_access_required
    def library_print_file(file_id):
        """Create a new job from a library file."""
        if not file_library:
            return jsonify({"error": "Library not available"}), 500
        lib_file = file_library.get_file(file_id)
        if not lib_file:
            return jsonify({"error": "File not found"}), 404
        if not os.path.exists(lib_file["file_path"]):
            return jsonify({"error": "File missing from disk"}), 404

        new_job_id = job_queue.add_job(
            filename=lib_file["stored_name"],
            original_name=lib_file["original_name"],
            file_path=lib_file["file_path"],
            copies=1,
            priority=0,
            notes=f"Reprinted from library (file #{file_id})",
            submitted_by=session.get("username", ""),
            print_time_seconds=lib_file.get("print_time_seconds"),
        )
        file_library.increment_print_count(file_id)

        from .notifications import NotificationManager
        NotificationManager(app_config).notify(
            "job_submitted",
            f"New Job — {lib_file['original_name']}",
            f"Job #{new_job_id} submitted from library by {session.get('username', 'unknown')}.\nFile: {lib_file['original_name']}",
        )

        return jsonify({"ok": True, "job_id": new_job_id})

    @app.route(prefix + "/api/library/folders", methods=["POST"])
    @app.route("/api/library/folders", methods=["POST"])
    @login_required
    def library_create_folder():
        if not file_library:
            return jsonify({"error": "Library not available"}), 500
        data = request.get_json(silent=True) or {}
        name = data.get("name", "").strip()
        parent_id = data.get("parent_id")
        return jsonify(file_library.create_folder(name, parent_id))

    @app.route(prefix + "/api/library/folders/tree")
    @app.route("/api/library/folders/tree")
    @login_required
    def library_folder_tree():
        if not file_library:
            return jsonify({"folders": []})
        return jsonify({"folders": file_library.get_all_folders()})

    @app.route(prefix + "/api/library/folders/<int:folder_id>/rename", methods=["POST"])
    @app.route("/api/library/folders/<int:folder_id>/rename", methods=["POST"])
    @login_required
    def library_rename_folder(folder_id):
        if not file_library:
            return jsonify({"error": "Library not available"}), 500
        data = request.get_json(silent=True) or {}
        name = data.get("name", "").strip()
        return jsonify(file_library.rename_folder(folder_id, name))

    @app.route(prefix + "/api/library/folders/<int:folder_id>/move", methods=["POST"])
    @app.route("/api/library/folders/<int:folder_id>/move", methods=["POST"])
    @login_required
    def library_move_folder(folder_id):
        if not file_library:
            return jsonify({"error": "Library not available"}), 500
        data = request.get_json(silent=True) or {}
        parent_id = data.get("parent_id")
        return jsonify(file_library.move_folder(folder_id, parent_id))

    @app.route(prefix + "/api/library/folders/<int:folder_id>/delete", methods=["POST", "DELETE"])
    @app.route("/api/library/folders/<int:folder_id>/delete", methods=["POST", "DELETE"])
    @app.route(prefix + "/api/library/folders/<int:folder_id>", methods=["DELETE"])
    @app.route("/api/library/folders/<int:folder_id>", methods=["DELETE"])
    @admin_required
    def library_delete_folder(folder_id):
        if not file_library:
            return jsonify({"error": "Library not available"}), 500
        return jsonify(file_library.delete_folder(folder_id))

    # ── Discovery API ─────────────────────────────────────

    @app.route(prefix + "/api/discover/scan", methods=["POST"])
    @app.route("/api/discover/scan", methods=["POST"])
    @admin_required
    def discover_scan():
        """Listen for Bambu UDP broadcasts + optionally scan subnet for Bambu and Klipper."""
        data = request.get_json(silent=True) or {}
        timeout = min(float(data.get("timeout", 5)), 15)
        do_port_scan = data.get("port_scan", False)
        subnet = data.get("subnet", "")

        # UDP broadcast discovery (Bambu only)
        printers = _filter_discovery_results(discover_printers(timeout=timeout))

        # Optional port scan fallback
        scan_results = []
        if do_port_scan:
            if not subnet:
                subnets = get_local_subnets()
            else:
                subnets = [subnet]
            for s in subnets:
                # Scan for Bambu (8883) and Klipper/Moonraker (7125)
                hosts = [
                    h for h in scan_subnet(s, timeout=1.0)
                    if h not in _local_ipv4_addresses()
                ]
                klipper_hosts = scan_moonraker_port(s, timeout=1.0)
                known_ips = {p["host"] for p in printers}
                for h in hosts:
                    if h not in known_ips:
                        scan_results.append({"host": h, "name": f"Unknown ({h})", "serial": "", "model": "Detected via port scan (MQTT 8883)", "type": "bambulab"})
                for h in klipper_hosts:
                    if h not in known_ips:
                        scan_results.append({"host": h, "name": f"Klipper ({h})", "serial": "", "model": "Detected via port scan (Moonraker 7125)", "type": "klipper"})

        return jsonify({
            "discovered": printers,
            "port_scan": scan_results,
            "subnets": get_local_subnets(),
        })

    @app.route(prefix + "/api/discover/test", methods=["POST"])
    @app.route("/api/discover/test", methods=["POST"])
    @admin_required
    def discover_test():
        """Test connection to a printer (Bambu MQTT or Klipper Moonraker)."""
        data = request.get_json(silent=True) or {}
        printer_type = data.get("type", "bambulab").lower()
        host = data.get("host", "")

        if printer_type == "klipper":
            moonraker_port = int(data.get("moonraker_port", 7125))
            api_key = data.get("api_key", "")
            if not host:
                return jsonify({"ok": False, "message": "host is required"}), 400
            result = test_klipper_connection(host, moonraker_port, api_key)
            logger.info(
                "Discovery test for Klipper host %s:%s: ok=%s message=%s",
                host, moonraker_port, result.get("ok"), result.get("message", ""),
            )
            return jsonify(result)
        else:
            access_code = data.get("access_code", "")
            serial = data.get("serial", "")
            if not host or not access_code or not serial:
                return jsonify({"ok": False, "message": "host, access_code, and serial are required"}), 400
            result = test_bambu_connection(host, access_code, serial)
            logger.info(
                "Discovery test for Bambu host %s serial %s: ok=%s message=%s",
                host, serial, result.get("ok"), result.get("message", ""),
            )
            return jsonify(result)

    @app.route(prefix + "/api/discover/add", methods=["POST"])
    @app.route("/api/discover/add", methods=["POST"])
    @admin_required
    def discover_add():
        """Add a printer to the config and connect to it."""
        data = request.get_json(silent=True) or {}
        name = data.get("name", "").strip()
        host = data.get("host", "").strip()
        printer_type = data.get("type", "bambulab").lower().strip()

        if not name or not host:
            return jsonify({"ok": False, "message": "name and host are required"}), 400

        if printer_type == "klipper":
            # Klipper printer — only needs name, host, and optional moonraker_port/api_key
            moonraker_port = int(data.get("moonraker_port", 7125))
            api_key = data.get("api_key", "").strip()
            camera_url = data.get("camera_url", "").strip()

            existing = farm_manager.get_printer(name)
            if existing:
                return jsonify({"ok": False, "message": f"Printer '{name}' already exists"}), 400

            config_path = os.environ.get("FARM_CONFIG", "config/config.yaml")
            try:
                with open(config_path) as f:
                    config = yaml.safe_load(f) or {}
                if not config.get("printers"):
                    config["printers"] = []

                new_printer = {
                    "name": name,
                    "type": "klipper",
                    "host": host,
                    "moonraker_port": moonraker_port,
                }
                if api_key:
                    new_printer["api_key"] = api_key
                if camera_url:
                    new_printer["camera_url"] = camera_url
                orca_port = _next_orca_port()
                new_printer["orca_port"] = orca_port
                config["printers"].append(new_printer)

                with open(config_path, "w") as f:
                    yaml.dump(config, f, default_flow_style=False, sort_keys=False)
                # Update live config
                app_config["printers"] = config["printers"]

                _create_orca_vhost(name, orca_port)

                # Hot-add
                from .klipper_client import KlipperClient
                client = KlipperClient(
                    name=name, host=host, port=moonraker_port,
                    api_key=api_key, camera_url=camera_url,
                )
                farm_manager._printers[name] = client
                farm_manager._printer_types[name] = "klipper"
                connected = client.connect(timeout=10)

                return jsonify({"ok": True, "connected": connected, "message": f"Klipper printer '{name}' added"})
            except Exception as e:
                logger.error(f"Failed to add Klipper printer: {e}")
                return jsonify({"ok": False, "message": str(e)}), 500
        else:
            # BambuLab printer
            access_code = data.get("access_code", "").strip()
            serial = data.get("serial", "").strip()
            ams_serial = data.get("ams_serial", "").strip()

            if not access_code or not serial:
                return jsonify({"ok": False, "message": "access_code and serial are required for BambuLab printers"}), 400

            existing = farm_manager.get_printer(name)
            if existing:
                return jsonify({"ok": False, "message": f"Printer '{name}' already exists"}), 400

            config_path = os.environ.get("FARM_CONFIG", "config/config.yaml")
            try:
                with open(config_path) as f:
                    config = yaml.safe_load(f) or {}
                if not config.get("printers"):
                    config["printers"] = []

                new_printer = {
                    "name": name,
                    "type": "bambulab",
                    "host": host,
                    "access_code": access_code,
                    "serial": serial,
                    "mqtt_port": int(data.get("mqtt_port", 8883)),
                    "ftp_port": int(data.get("ftp_port", 990)),
                    "camera_port": int(data.get("camera_port", 6000)),
                }
                if ams_serial:
                    new_printer["ams_serial"] = ams_serial
                orca_port = _next_orca_port()
                new_printer["orca_port"] = orca_port
                config["printers"].append(new_printer)

                with open(config_path, "w") as f:
                    yaml.dump(config, f, default_flow_style=False, sort_keys=False)
                # Update live config
                app_config["printers"] = config["printers"]

                _create_orca_vhost(name, orca_port)

                # Hot-add
                from .bambu_client import BambuClient
                client = BambuClient(
                    name=name, host=host, access_code=access_code,
                    serial=serial, port=new_printer["mqtt_port"],
                    ftp_port=new_printer["ftp_port"],
                    camera_port=new_printer["camera_port"],
                    ams_serial=ams_serial,
                )
                farm_manager._printers[name] = client
                farm_manager._printer_types[name] = "bambulab"
                connected = client.connect(timeout=10)

                return jsonify({"ok": True, "connected": connected, "message": f"Printer '{name}' added"})
            except Exception as e:
                logger.error(f"Failed to add printer: {e}")
                return jsonify({"ok": False, "message": str(e)}), 500

    @app.route(prefix + "/api/discover/remove", methods=["POST"])
    @app.route("/api/discover/remove", methods=["POST"])
    @admin_required
    def discover_remove():
        """Remove a printer from the config and disconnect."""
        data = request.get_json(silent=True) or {}
        name = data.get("name", "").strip()
        if not name:
            return jsonify({"ok": False, "message": "name is required"}), 400

        # Disconnect
        client = farm_manager.get_printer(name)
        if client:
            client.disconnect()
            del farm_manager._printers[name]
            farm_manager._printer_types.pop(name, None)

        # Remove from config
        config_path = os.environ.get("FARM_CONFIG", "config/config.yaml")
        try:
            with open(config_path) as f:
                config = yaml.safe_load(f) or {}
            # Find the printer entry to get its orca_port before removing
            orca_port = None
            for p in (config.get("printers") or []):
                if p.get("name") == name:
                    orca_port = p.get("orca_port")
                    break
            config["printers"] = [p for p in (config.get("printers") or []) if p.get("name") != name]
            with open(config_path, "w") as f:
                yaml.dump(config, f, default_flow_style=False, sort_keys=False)
            app_config["printers"] = config["printers"]

            _remove_orca_vhost(name, orca_port)

            return jsonify({"ok": True, "message": f"Printer '{name}' removed"})
        except Exception as e:
            return jsonify({"ok": False, "message": str(e)}), 500

    @app.route(prefix + "/api/discover/rename", methods=["POST"])
    @app.route("/api/discover/rename", methods=["POST"])
    @admin_required
    def discover_rename():
        """Rename a printer in the config and live state."""
        data = request.get_json(silent=True) or {}
        old_name = data.get("old_name", "").strip()
        new_name = data.get("new_name", "").strip()
        if not old_name or not new_name:
            return jsonify({"ok": False, "message": "old_name and new_name are required"}), 400
        if old_name == new_name:
            return jsonify({"ok": True, "message": "Name unchanged"})

        # Check new name doesn't conflict
        if farm_manager.get_printer(new_name):
            return jsonify({"ok": False, "message": f"Printer '{new_name}' already exists"}), 400

        client = farm_manager.get_printer(old_name)
        if not client:
            return jsonify({"ok": False, "message": f"Printer '{old_name}' not found"}), 404

        # Update live state
        client.name = new_name
        farm_manager._printers[new_name] = farm_manager._printers.pop(old_name)

        # Update camera manager if active
        if camera_manager and hasattr(camera_manager, '_cameras'):
            if old_name in camera_manager._cameras:
                camera_manager._cameras[new_name] = camera_manager._cameras.pop(old_name)

        # Update any active jobs referencing the old name
        try:
            for job in job_queue.get_active_jobs():
                if job.get("printer_name") == old_name:
                    conn = job_queue._get_conn()
                    conn.execute(
                        "UPDATE jobs SET printer_name = ? WHERE id = ?",
                        (new_name, job["id"]),
                    )
                    conn.commit()
                    conn.close()
        except Exception as e:
            logger.warning(f"Failed to update job printer names: {e}")

        # Update config file
        config_path = os.environ.get("FARM_CONFIG", "config/config.yaml")
        try:
            with open(config_path) as f:
                config = yaml.safe_load(f) or {}
            orca_port = None
            for p in config.get("printers", []):
                if p.get("name") == old_name:
                    p["name"] = new_name
                    orca_port = p.get("orca_port")
                    break
            with open(config_path, "w") as f:
                yaml.dump(config, f, default_flow_style=False, sort_keys=False)
            app_config["printers"] = config["printers"]

            # Recreate Apache vhost with new name
            if orca_port:
                _remove_orca_vhost(old_name, None)  # Don't remove the Listen port
                _create_orca_vhost(new_name, orca_port)

            return jsonify({"ok": True, "message": f"Printer renamed to '{new_name}'"})
        except Exception as e:
            logger.error(f"Failed to rename printer in config: {e}")
            return jsonify({"ok": False, "message": str(e)}), 500

    @app.route(prefix + "/api/printer/<name>/staff_only", methods=["POST"])
    @app.route("/api/printer/<name>/staff_only", methods=["POST"])
    @admin_required
    def set_staff_only(name):
        """Toggle the staff_only flag for a printer."""
        data = request.get_json(silent=True) or {}
        staff_only = bool(data.get("staff_only", False))
        found = False
        for p in app_config.get("printers", []):
            if p.get("name") == name:
                p["staff_only"] = staff_only
                found = True
                break
        if not found:
            return jsonify({"ok": False, "error": "Printer not found"}), 404
        try:
            _save_config()
        except Exception as e:
            logger.error(f"Failed to save staff_only setting: {e}")
            return jsonify({"ok": False, "error": str(e)}), 500
        return jsonify({"ok": True, "staff_only": staff_only})

    # ── Active Directory Config API ───────────────────────

    @app.route(prefix + "/api/ad/config", methods=["GET"])
    @app.route("/api/ad/config", methods=["GET"])
    @admin_required
    def ad_get_config():
        """Get current AD configuration (password masked)."""
        ad = dict(_get_ad_config())
        if ad.get("bind_password"):
            ad["bind_password"] = "********"
        return jsonify(ad)

    @app.route(prefix + "/api/ad/config", methods=["POST"])
    @app.route("/api/ad/config", methods=["POST"])
    @admin_required
    def ad_save_config():
        """Save AD configuration to config.yaml."""
        data = request.get_json(silent=True) or {}
        config_path = os.environ.get("FARM_CONFIG", "config/config.yaml")
        try:
            with open(config_path) as f:
                file_config = yaml.safe_load(f) or {}

            ad = file_config.get("active_directory", {})
            ad["enabled"] = data.get("enabled", False)
            ad["server"] = data.get("server", "").strip()
            ad["port"] = int(data.get("port", 389))
            ad["use_ssl"] = data.get("use_ssl", False)
            ad["base_dn"] = data.get("base_dn", "").strip()
            ad["bind_user"] = data.get("bind_user", "").strip()
            # Only update password if not the mask placeholder
            if data.get("bind_password") and data["bind_password"] != "********":
                ad["bind_password"] = data["bind_password"]
            ad["student_ou"] = data.get("student_ou", "").strip()
            ad["staff_ou"] = data.get("staff_ou", "").strip()

            file_config["active_directory"] = ad

            with open(config_path, "w") as f:
                yaml.dump(file_config, f, default_flow_style=False, sort_keys=False)

            # Update live config
            app_config["active_directory"] = ad

            return jsonify({"ok": True, "message": "AD configuration saved"})
        except Exception as e:
            logger.error(f"Failed to save AD config: {e}")
            return jsonify({"ok": False, "message": str(e)}), 500

    @app.route(prefix + "/api/ad/test", methods=["POST"])
    @app.route("/api/ad/test", methods=["POST"])
    @admin_required
    def ad_test_connection():
        """Test AD connection with provided or saved config."""
        data = request.get_json(silent=True) or {}
        # Use provided values, falling back to saved config
        ad = dict(_get_ad_config())
        if data.get("server"):
            ad["server"] = data["server"].strip()
        if data.get("port"):
            ad["port"] = int(data["port"])
        if "use_ssl" in data:
            ad["use_ssl"] = data["use_ssl"]
        if data.get("bind_user"):
            ad["bind_user"] = data["bind_user"].strip()
        if data.get("bind_password") and data["bind_password"] != "********":
            ad["bind_password"] = data["bind_password"]

        result = test_ad_connection(ad)
        return jsonify(result)

    # ── Student Print Access Config API ───────────────────

    @app.route(prefix + "/api/student-access/config", methods=["GET"])
    @app.route("/api/student-access/config", methods=["GET"])
    @admin_required
    def student_access_get_config():
        access = _get_student_access_config()
        return jsonify({
            "allowlist": access.get("allowlist", []),
            "banlist": access.get("banlist", []),
        })

    @app.route(prefix + "/api/student-access/config", methods=["POST"])
    @app.route("/api/student-access/config", methods=["POST"])
    @admin_required
    def student_access_save_config():
        data = request.get_json(silent=True) or {}

        def clean_list(values):
            if not isinstance(values, list):
                return []
            seen = set()
            cleaned = []
            for value in values:
                name = re.sub(r"\s+", " ", str(value or "").strip())
                key = _normalise_access_name(name)
                if name and key not in seen:
                    seen.add(key)
                    cleaned.append(name)
            return cleaned

        access = {
            "allowlist": clean_list(data.get("allowlist", [])),
            "banlist": clean_list(data.get("banlist", [])),
        }

        config_path = os.environ.get("FARM_CONFIG", "config/config.yaml")
        try:
            with open(config_path) as f:
                file_config = yaml.safe_load(f) or {}
            file_config["student_access"] = access
            with open(config_path, "w") as f:
                yaml.dump(file_config, f, default_flow_style=False, sort_keys=False)
            app_config["student_access"] = access
            return jsonify({"ok": True, "message": "Student access lists saved"})
        except Exception as e:
            logger.error(f"Failed to save student access config: {e}")
            return jsonify({"ok": False, "message": str(e)}), 500

    # ── Obico Configuration ───────────────────────────────

    def _get_obico_config_for_printer(printer_name):
        """Return the obico config dict for a given printer, or empty dict."""
        for p in app_config.get("printers", []):
            if p.get("name") == printer_name and p.get("type") == "klipper":
                return dict(p.get("obico", {}))
        return {}

    @app.route(prefix + "/api/obico/config/<name>", methods=["GET"])
    @app.route("/api/obico/config/<name>", methods=["GET"])
    @admin_required
    def obico_get_config(name):
        """Get Obico configuration for a printer (password masked)."""
        cfg = _get_obico_config_for_printer(name)
        cfg["enabled"] = bool(cfg.get("server"))
        if cfg.get("password"):
            cfg["password"] = "********"
        return jsonify(cfg)

    @app.route(prefix + "/api/obico/config/<name>", methods=["POST"])
    @app.route("/api/obico/config/<name>", methods=["POST"])
    @admin_required
    def obico_save_config(name):
        """Save Obico configuration for a specific printer."""
        data = request.get_json(silent=True) or {}
        config_path = os.environ.get("FARM_CONFIG", "config/config.yaml")
        try:
            with open(config_path) as f:
                file_config = yaml.safe_load(f) or {}

            # Find the printer in config
            printer_found = False
            for p in file_config.get("printers", []):
                if p.get("name") == name and p.get("type") == "klipper":
                    printer_found = True
                    if data.get("enabled"):
                        obico = p.get("obico", {})
                        obico["server"] = data.get("server", "").strip()
                        obico["printer_id"] = int(data.get("printer_id", 0))
                        obico["username"] = data.get("username", "").strip()
                        if data.get("password") and data["password"] != "********":
                            obico["password"] = data["password"]
                        p["obico"] = obico
                    else:
                        # Disabled — remove obico block
                        p.pop("obico", None)
                    break

            if not printer_found:
                return jsonify({"ok": False, "message": f"Klipper printer '{name}' not found"}), 404

            with open(config_path, "w") as f:
                yaml.dump(file_config, f, default_flow_style=False, sort_keys=False)

            # Update live config
            for p in app_config.get("printers", []):
                if p.get("name") == name:
                    if data.get("enabled"):
                        p["obico"] = file_config_obico = next(
                            (pr.get("obico", {}) for pr in file_config["printers"] if pr.get("name") == name), {}
                        )
                    else:
                        p.pop("obico", None)
                    break

            return jsonify({"ok": True, "message": "Obico configuration saved"})
        except Exception as e:
            logger.error(f"Failed to save Obico config: {e}")
            return jsonify({"ok": False, "message": str(e)}), 500

    @app.route(prefix + "/api/obico/test", methods=["POST"])
    @app.route("/api/obico/test", methods=["POST"])
    @admin_required
    def obico_test_connection():
        """Test Obico connection with provided credentials."""
        data = request.get_json(silent=True) or {}
        server = data.get("server", "").strip()
        username = data.get("username", "").strip()
        password = data.get("password", "").strip()
        printer_id = int(data.get("printer_id", 0))

        if not server or not username or not password or not printer_id:
            return jsonify({"ok": False, "message": "All fields are required"}), 400

        try:
            from .obico_client import ObicoClient
            client = ObicoClient(server, username, password, printer_id)
            client._login()
            status = client.fetch_status()
            return jsonify({
                "ok": True,
                "state": status.get("state", "unknown"),
                "watching": status.get("watching", False),
            })
        except Exception as e:
            return jsonify({"ok": False, "message": str(e)}), 500

    # ── UI Preferences API ────────────────────────────────

    @app.route(prefix + "/api/ui/config", methods=["GET"])
    @app.route("/api/ui/config", methods=["GET"])
    @login_required
    def ui_get_config():
        """Get UI preferences (timezone, locale, etc.)."""
        ui = app_config.get("ui", {})
        return jsonify({
            "timezone": ui.get("timezone", ""),
            "locale": ui.get("locale", "en-AU"),
        })

    @app.route(prefix + "/api/ui/config", methods=["POST"])
    @app.route("/api/ui/config", methods=["POST"])
    @admin_required
    def ui_save_config():
        """Save UI preferences to config.yaml."""
        data = request.get_json(silent=True) or {}
        config_path = os.environ.get("FARM_CONFIG", "config/config.yaml")
        try:
            with open(config_path) as f:
                file_config = yaml.safe_load(f) or {}
            ui = file_config.get("ui", {})
            if "timezone" in data:
                ui["timezone"] = data["timezone"].strip()
            if "locale" in data:
                ui["locale"] = data["locale"].strip()
            file_config["ui"] = ui
            with open(config_path, "w") as f:
                yaml.dump(file_config, f, default_flow_style=False, sort_keys=False)
            app_config["ui"] = ui
            return jsonify({"ok": True})
        except Exception as e:
            logger.error(f"Failed to save UI config: {e}")
            return jsonify({"ok": False, "message": str(e)}), 500

    # ── Spoolman Config API ───────────────────────────────

    @app.route(prefix + "/api/spoolman/config", methods=["GET"])
    @app.route("/api/spoolman/config", methods=["GET"])
    @admin_required
    def spoolman_get_config():
        """Get current Spoolman configuration."""
        sm = app_config.get("spoolman", {})
        return jsonify({"url": sm.get("url", "")})

    @app.route(prefix + "/api/spoolman/config", methods=["POST"])
    @app.route("/api/spoolman/config", methods=["POST"])
    @admin_required
    def spoolman_save_config():
        """Save Spoolman configuration to config.yaml."""
        data = request.get_json(silent=True) or {}
        config_path = os.environ.get("FARM_CONFIG", "config/config.yaml")
        try:
            with open(config_path) as f:
                file_config = yaml.safe_load(f) or {}

            sm = file_config.get("spoolman", {})
            url = data.get("url", "").strip().rstrip("/")
            sm["url"] = url
            file_config["spoolman"] = sm

            with open(config_path, "w") as f:
                yaml.dump(file_config, f, default_flow_style=False, sort_keys=False)

            app_config["spoolman"] = sm

            return jsonify({"ok": True, "message": "Spoolman configuration saved. Restart service to apply."})
        except Exception as e:
            logger.error(f"Failed to save Spoolman config: {e}")
            return jsonify({"ok": False, "message": str(e)}), 500

    @app.route(prefix + "/api/spoolman/test", methods=["POST"])
    @app.route("/api/spoolman/test", methods=["POST"])
    @admin_required
    def spoolman_test_connection():
        """Test connectivity to a Spoolman instance."""
        data = request.get_json(silent=True) or {}
        url = data.get("url", "").strip().rstrip("/")
        if not url:
            return jsonify({"ok": False, "message": "URL is required"}), 400
        try:
            import requests as _requests
            info_res = _requests.get(url + "/api/v1/info", timeout=5)
            info_res.raise_for_status()
            info = info_res.json()

            health_res = _requests.get(url + "/api/v1/health", timeout=5)
            health = health_res.json() if health_res.ok else {}

            return jsonify({
                "ok": True,
                "version": info.get("version", "unknown"),
                "healthy": health.get("status") == "healthy",
                "db_type": info.get("db_type", "unknown"),
            })
        except _requests.ConnectionError:
            return jsonify({"ok": False, "message": f"Cannot connect to {url}"}), 502
        except Exception as e:
            return jsonify({"ok": False, "message": str(e)}), 500

    # ── Build Plate Detection Config API ──────────────────

    @app.route(prefix + "/api/plate-detection/config/<name>", methods=["GET"])
    @app.route("/api/plate-detection/config/<name>", methods=["GET"])
    @admin_required
    def plate_detection_get_config(name):
        if not farm_manager.get_printer(name):
            return jsonify({"error": "Printer not found"}), 404
        cfg = _get_plate_detection_config(name)
        refs = _list_plate_references(name, "inspection")
        rest_refs = _list_plate_references(name, "rest")
        return jsonify({
            **cfg,
            "references": refs,
            "rest_references": rest_refs,
            "inspection_references": refs,
            "max_references": 5,
        })

    @app.route(prefix + "/api/plate-detection/config/<name>", methods=["POST"])
    @app.route("/api/plate-detection/config/<name>", methods=["POST"])
    @admin_required
    def plate_detection_save_config(name):
        if not farm_manager.get_printer(name):
            return jsonify({"ok": False, "message": "Printer not found"}), 404
        data = request.get_json(silent=True) or {}
        roi = data.get("roi") or {}
        cfg = {
            "enabled": bool(data.get("enabled", False)),
            "threshold": max(1.0, min(80.0, float(data.get("threshold", 12.0)))),
            "roi": {
                "x": max(0.0, min(100.0, float(roi.get("x", 0)))),
                "y": max(0.0, min(100.0, float(roi.get("y", 0)))),
                "w": max(1.0, min(100.0, float(roi.get("w", 100)))),
                "h": max(1.0, min(100.0, float(roi.get("h", 100)))),
            },
            "prepare_before_check": bool(data.get("prepare_before_check", _bambu_uses_raised_bed(name))),
            "inspection_z": max(0.0, min(250.0, float(data.get("inspection_z", 0.0)))),
            "settle_seconds": max(0.0, min(10.0, float(data.get("settle_seconds", 2.0)))),
        }
        try:
            with open(os.environ.get("FARM_CONFIG", "config/config.yaml")) as f:
                file_config = yaml.safe_load(f) or {}
            plate_cfg = file_config.get("plate_detection", {})
            plate_cfg[name] = cfg
            file_config["plate_detection"] = plate_cfg
            with open(os.environ.get("FARM_CONFIG", "config/config.yaml"), "w") as f:
                yaml.dump(file_config, f, default_flow_style=False, sort_keys=False)
            app_config["plate_detection"] = plate_cfg
            return jsonify({"ok": True, "message": "Build plate detection saved"})
        except Exception as e:
            logger.error(f"Failed to save plate detection config: {e}")
            return jsonify({"ok": False, "message": str(e)}), 500

    @app.route(prefix + "/api/plate-detection/capture/<name>", methods=["POST"])
    @app.route("/api/plate-detection/capture/<name>", methods=["POST"])
    @admin_required
    def plate_detection_capture_reference(name):
        if not farm_manager.get_printer(name):
            return jsonify({"ok": False, "message": "Printer not found"}), 404
        data = request.get_json(silent=True) or {}
        phase = (data.get("phase") or request.args.get("phase") or "inspection").strip().lower()
        if phase not in {"rest", "inspection"}:
            return jsonify({"ok": False, "message": "Reference phase must be rest or inspection"}), 400

        refs = _list_plate_references(name, phase)
        if len(refs) >= 5:
            return jsonify({"ok": False, "message": f"Maximum of 5 {phase} reference images reached"}), 400
        cfg = _get_plate_detection_config(name)
        frame_after = None
        if phase == "inspection":
            prep = _prepare_plate_detection_view(name, cfg)
            if not prep.get("ok"):
                return jsonify({"ok": False, "message": prep.get("message", "Build plate inspection setup failed")}), 400
            frame_after = prep.get("after")
        frame = _current_camera_frame(name, wait_seconds=0.5 if frame_after else 0.0, after=frame_after)
        if not frame:
            return jsonify({"ok": False, "message": "No camera snapshot available"}), 400
        idx = 1
        existing = set(refs)
        prefix = _plate_reference_prefix(phase)
        while f"{prefix}{idx}.jpg" in existing:
            idx += 1
        ref_name = f"{prefix}{idx}.jpg"
        with open(os.path.join(_plate_detection_dir(name), ref_name), "wb") as f:
            f.write(frame)
        return jsonify({
            "ok": True,
            "phase": phase,
            "reference": ref_name,
            "references": _list_plate_references(name, "inspection"),
            "rest_references": _list_plate_references(name, "rest"),
            "inspection_references": _list_plate_references(name, "inspection"),
        })

    @app.route(prefix + "/api/plate-detection/prepare/<name>", methods=["POST"])
    @app.route("/api/plate-detection/prepare/<name>", methods=["POST"])
    @admin_required
    def plate_detection_prepare_view(name):
        if not farm_manager.get_printer(name):
            return jsonify({"ok": False, "message": "Printer not found"}), 404
        if farm_manager.get_printer_type(name) != "bambulab":
            return jsonify({"ok": False, "message": "Raised inspection positioning is only available for Bambu printers"}), 400
        data = request.get_json(silent=True) or {}
        cfg = _get_plate_detection_config(name)
        cfg["prepare_before_check"] = True
        prep = _prepare_plate_detection_view(name, cfg, home=bool(data.get("home", False)))
        if not prep.get("ok"):
            return jsonify({"ok": False, "message": prep.get("message", "Build plate inspection setup failed")}), 400
        return jsonify({
            "ok": True,
            "message": "Build plate moved to raised inspection position",
            "inspection_z": cfg.get("inspection_z", 0.0),
        })

    @app.route(prefix + "/api/plate-detection/jog/<name>", methods=["POST"])
    @app.route("/api/plate-detection/jog/<name>", methods=["POST"])
    @admin_required
    def plate_detection_jog_bed(name):
        printer = farm_manager.get_printer(name)
        if not printer:
            return jsonify({"ok": False, "message": "Printer not found"}), 404
        if farm_manager.get_printer_type(name) != "bambulab":
            return jsonify({"ok": False, "message": "Bed jog is only available for Bambu printers"}), 400
        if not printer.is_connected():
            return jsonify({"ok": False, "message": f"Printer '{name}' not connected"}), 400

        data = request.get_json(silent=True) or {}
        action = str(data.get("action", "jog")).strip().lower()
        if action == "home":
            if not hasattr(printer, "home_build_plate_z"):
                return jsonify({"ok": False, "message": "Printer does not support Z home"}), 400
            ok = printer.home_build_plate_z()
            return jsonify({"ok": ok, "message": "Bambu bed homed" if ok else "Could not home Bambu bed"}), (200 if ok else 500)

        try:
            delta = float(data.get("delta_z", 0))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "message": "delta_z must be a number"}), 400
        delta = max(-50.0, min(50.0, delta))
        if not delta:
            return jsonify({"ok": False, "message": "delta_z is required"}), 400
        if not hasattr(printer, "jog_build_plate"):
            return jsonify({"ok": False, "message": "Printer does not support bed jog"}), 400
        ok = printer.jog_build_plate(delta)
        direction = "up" if delta < 0 else "down"
        return jsonify({
            "ok": ok,
            "message": f"Moved Bambu bed {direction} {abs(delta):g} mm" if ok else "Could not jog Bambu bed",
            "delta_z": delta,
        }), (200 if ok else 500)

    @app.route(prefix + "/api/plate-detection/reference/<name>/<ref_name>", methods=["DELETE", "POST"])
    @app.route("/api/plate-detection/reference/<name>/<ref_name>", methods=["DELETE", "POST"])
    @admin_required
    def plate_detection_delete_reference(name, ref_name):
        if not farm_manager.get_printer(name):
            return jsonify({"ok": False, "message": "Printer not found"}), 404
        if ref_name not in _list_plate_references(name):
            return jsonify({"ok": False, "message": "Reference not found"}), 404
        os.remove(os.path.join(_plate_detection_dir(name), ref_name))
        return jsonify({"ok": True, "references": _list_plate_references(name)})

    @app.route(prefix + "/api/plate-detection/reference/<name>/<ref_name>")
    @app.route("/api/plate-detection/reference/<name>/<ref_name>")
    @admin_required
    def plate_detection_reference_image(name, ref_name):
        if ref_name not in _list_plate_references(name):
            return Response(status=404)
        return send_from_directory(_plate_detection_dir(name), ref_name, mimetype="image/jpeg")

    @app.route(prefix + "/api/plate-detection/test/<name>", methods=["POST"])
    @app.route("/api/plate-detection/test/<name>", methods=["POST"])
    @admin_required
    def plate_detection_test(name):
        if not farm_manager.get_printer(name):
            return jsonify({"ok": False, "message": "Printer not found"}), 404
        data = request.get_json(silent=True) or {}
        if data.get("full_check"):
            result = _check_build_plate_clear(name)
        else:
            result = _test_current_plate_view(name)
        return jsonify(result)

    @app.route(prefix + "/api/plate-detection/test-references/<name>", methods=["POST"])
    @app.route("/api/plate-detection/test-references/<name>", methods=["POST"])
    @admin_required
    def plate_detection_test_references(name):
        if not farm_manager.get_printer(name):
            return jsonify({"ok": False, "message": "Printer not found"}), 404
        return jsonify(_test_plate_reference_images(name))

    # ── Camera helpers ────────────────────────────────────

    def _detect_klipper_webcam(printer):
        """Try to auto-detect the webcam URL from Moonraker's /server/webcams/list."""
        try:
            import requests as _requests
            base = f"http://{printer.host}:{printer.port}"
            resp = _requests.get(f"{base}/server/webcams/list", timeout=5)
            if resp.status_code == 200:
                webcams = resp.json().get("result", {}).get("webcams", [])
                for wc in webcams:
                    stream_url = wc.get("stream_url") or wc.get("snapshot_url") or ""
                    if stream_url:
                        # Resolve relative URLs
                        if stream_url.startswith("/"):
                            stream_url = f"http://{printer.host}{stream_url}"
                        return stream_url
        except Exception as e:
            logger.warning(f"Failed to detect Klipper webcam for {printer.name}: {e}")
        return ""

    # ── Camera API ────────────────────────────────────────

    @app.route(prefix + "/api/camera/<name>/start", methods=["POST"])
    @app.route("/api/camera/<name>/start", methods=["POST"])
    def camera_start(name):
        """Start camera stream for a printer."""
        if not camera_manager:
            return jsonify({"ok": False, "message": "Camera manager not available"}), 503
        printer = farm_manager.get_printer(name)
        if not printer:
            return jsonify({"ok": False, "message": f"Printer '{name}' not found"}), 404

        printer_type = farm_manager.get_printer_type(name)
        if printer_type == "klipper":
            camera_url = getattr(printer, 'camera_url', '')
            if not camera_url:
                # Try auto-detecting from Moonraker
                camera_url = _detect_klipper_webcam(printer)
            if not camera_url:
                return jsonify({"ok": False, "message": "No camera URL configured or detected for this Klipper printer"}), 400
            camera_manager.start_http_camera(name, camera_url)
        else:
            camera_manager.start_camera(name, printer.host, printer.access_code)
        return jsonify({"ok": True, "message": f"Camera started for '{name}'"})

    @app.route(prefix + "/api/camera/<name>/stop", methods=["POST"])
    @app.route("/api/camera/<name>/stop", methods=["POST"])
    def camera_stop(name):
        """Stop camera stream for a printer."""
        if not camera_manager:
            return jsonify({"ok": False, "message": "Camera manager not available"}), 503
        camera_manager.stop_camera(name)
        return jsonify({"ok": True, "message": f"Camera stopped for '{name}'"})

    @app.route(prefix + "/api/camera/<name>/snapshot")
    @app.route("/api/camera/<name>/snapshot")
    def camera_snapshot(name):
        """Return the latest JPEG frame as an image."""
        if not camera_manager:
            return Response("Camera manager not available", status=503)
        frame = camera_manager.get_frame(name)
        if frame is None:
            return Response("No frame available", status=404)
        return Response(frame, mimetype="image/jpeg",
                        headers={"Cache-Control": "no-cache, no-store"})

    @app.route(prefix + "/api/camera/<name>/stream")
    @app.route("/api/camera/<name>/stream")
    def camera_stream(name):
        """MJPEG stream — multipart/x-mixed-replace boundary push."""
        if not camera_manager:
            return Response("Camera manager not available", status=503)

        def generate():
            while True:
                frame = camera_manager.get_frame(name)
                if frame:
                    yield (b"--frame\r\n"
                           b"Content-Type: image/jpeg\r\n\r\n" +
                           frame + b"\r\n")
                time.sleep(0.5)  # ~2 FPS

        return Response(generate(),
                        mimetype="multipart/x-mixed-replace; boundary=frame",
                        headers={"Cache-Control": "no-cache"})

    @app.route(prefix + "/api/camera/status")
    @app.route("/api/camera/status")
    def camera_status():
        """Get streaming status for all cameras."""
        if not camera_manager:
            return jsonify({})
        return jsonify(camera_manager.get_status())

    @app.route(prefix + "/api/camera/status/details")
    @app.route("/api/camera/status/details")
    def camera_status_details():
        """Get detailed streaming/stale status for all cameras."""
        if not camera_manager:
            return jsonify({})
        return jsonify(camera_manager.get_detailed_status())

    # ── OctoPrint-Compatible API (for OrcaSlicer) ─────────

    def _check_octoprint_api_key():
        """Validate X-Api-Key header against configured API key."""
        if not api_key:
            return True  # No key configured = open access
        key = request.headers.get("X-Api-Key", "")
        return key == api_key

    @app.route(prefix + "/api/version")
    @app.route("/api/version")
    def octoprint_version():
        """OctoPrint version endpoint — OrcaSlicer checks this to verify connection."""
        return jsonify({
            "api": "0.1",
            "server": "1.10.0",
            "text": "OctoPrint 1.10.0 (The Print Farm)",
        })

    @app.route(prefix + "/api/connection")
    @app.route("/api/connection")
    def octoprint_connection():
        """OctoPrint connection status — tells OrcaSlicer we're operational."""
        return jsonify({
            "current": {
                "state": "Operational",
                "port": "VIRTUAL",
                "baudrate": 250000,
                "printerProfile": "_default",
            },
            "options": {
                "ports": ["VIRTUAL"],
                "baudrates": [250000],
                "printerProfiles": [{"id": "_default", "name": "The Print Farm"}],
            },
        })

    @app.route(prefix + "/api/printer")
    @app.route("/api/printer")
    def octoprint_printer():
        """OctoPrint printer state — minimal response for compatibility."""
        return jsonify({
            "state": {
                "text": "Operational",
                "flags": {
                    "operational": True,
                    "printing": False,
                    "cancelling": False,
                    "pausing": False,
                    "error": False,
                    "paused": False,
                    "ready": True,
                    "sdReady": False,
                    "closedOrError": False,
                },
            },
            "temperature": {},
        })

    @app.route(prefix + "/api/files/local", methods=["POST"])
    @app.route("/api/files/local", methods=["POST"])
    @app.route(prefix + "/api/files/local/<printer_target>", methods=["POST"])
    @app.route("/api/files/local/<printer_target>", methods=["POST"])
    @app.route("/<printer_target>/api/files/local", methods=["POST"])
    def octoprint_upload(printer_target=None):
        """OctoPrint file upload — receives G-code from OrcaSlicer.
        
        If printer_target is provided in the URL, the job is assigned directly
        to that printer. Otherwise it enters the general queue.
        """
        if not _check_octoprint_api_key():
            return jsonify({"error": "Invalid API key"}), 403

        if "file" not in request.files:
            return jsonify({"error": "No file provided"}), 400

        file = request.files["file"]
        if not file.filename or not allowed_file(file.filename):
            return jsonify({"error": "Invalid file type"}), 400

        original_name = secure_filename(file.filename)
        unique_name = f"{uuid.uuid4().hex}_{original_name}"
        file_path = os.path.join(job_queue.upload_dir, unique_name)
        file.save(file_path)

        # If filename looks like an OrcaSlicer temp name (e.g. 97188.0.gcode),
        # try to extract the real model name from gcode metadata
        if re.match(r"^\d+\.\d+\.gcode$", original_name) and original_name.endswith(".gcode"):
            model_name = parse_gcode_model_name(file_path)
            if model_name:
                original_name = model_name + ".gcode"

        # Check if OrcaSlicer wants to print immediately
        print_flag = request.form.get("print", "false").lower() == "true"
        meta = parse_gcode_metadata(file_path)

        job_id = job_queue.add_job(
            filename=unique_name,
            original_name=original_name,
            file_path=file_path,
            copies=1,
            priority=10 if print_flag else 0,
            notes="Uploaded from OrcaSlicer",
            print_time_seconds=meta.get("print_time_seconds"),
        )

        # Notify
        from .notifications import NotificationManager
        NotificationManager(app_config).notify(
            "job_submitted",
            f"New Job — {original_name}",
            f"Job #{job_id} submitted via OrcaSlicer.\nFile: {original_name}",
        )

        # If a printer target is specified (per-printer virtual printer),
        # tag the job with that printer immediately so it shows in the UI
        # before the slow metadata parse below.
        if printer_target:
            client = farm_manager.get_printer(printer_target)
            if not client:
                job_queue.cancel_job(job_id)
                return jsonify({"error": f"Printer '{printer_target}' not found"}), 404
            conn = job_queue._get_conn()
            conn.execute("UPDATE jobs SET printer_name = ? WHERE id = ?",
                         (printer_target, job_id))
            conn.commit()
            conn.close()

        # Add to file library (metadata parsing can be slow on large gcode files)
        if file_library:
            try:
                file_library.add_file(
                    original_name=original_name,
                    stored_name=unique_name,
                    file_path=file_path,
                    file_size=os.path.getsize(file_path),
                    uploaded_by="OrcaSlicer",
                    metadata=meta,
                )
            except Exception as e:
                logger.warning(f"OrcaSlicer upload: failed to add to library: {e}")

        logger.info(f"OrcaSlicer upload: {original_name} -> job {job_id}"
                    f" (print={print_flag}, printer={printer_target or 'queue'})")

        # OctoPrint-style response
        return jsonify({
            "files": {
                "local": {
                    "name": original_name,
                    "display": original_name,
                    "path": original_name,
                    "origin": "local",
                },
            },
            "done": True,
        }), 201

    # Per-printer OctoPrint compat routes (version/connection/printer)
    @app.route(prefix + "/api/version/<printer_target>")
    @app.route("/api/version/<printer_target>")
    @app.route("/<printer_target>/api/version")
    def octoprint_version_printer(printer_target):
        p = farm_manager.get_printer(printer_target)
        name = printer_target if p else "Unknown"
        return jsonify({
            "api": "0.1",
            "server": "1.10.0",
            "text": f"OctoPrint 1.10.0 ({name})",
        })

    @app.route(prefix + "/api/connection/<printer_target>")
    @app.route("/api/connection/<printer_target>")
    @app.route("/<printer_target>/api/connection")
    def octoprint_connection_printer(printer_target):
        p = farm_manager.get_printer(printer_target)
        connected = p.is_connected() if p else False
        return jsonify({
            "current": {
                "state": "Operational" if connected else "Closed",
                "port": "VIRTUAL",
                "baudrate": 250000,
                "printerProfile": "_default",
            },
            "options": {
                "ports": ["VIRTUAL"],
                "baudrates": [250000],
                "printerProfiles": [{"id": "_default", "name": printer_target}],
            },
        })

    @app.route(prefix + "/api/printer/<printer_target>")
    @app.route("/api/printer/<printer_target>")
    @app.route("/<printer_target>/api/printer")
    def octoprint_printer_target(printer_target):
        p = farm_manager.get_printer(printer_target)
        connected = p.is_connected() if p else False
        printing = False
        if p and connected:
            from .bambu_client import PrintStatus
            printing = p.state.status == PrintStatus.RUNNING
        return jsonify({
            "state": {
                "text": "Printing" if printing else ("Operational" if connected else "Closed"),
                "flags": {
                    "operational": connected,
                    "printing": printing,
                    "cancelling": False,
                    "pausing": False,
                    "error": False,
                    "paused": False,
                    "ready": connected and not printing,
                    "sdReady": False,
                    "closedOrError": not connected,
                },
            },
            "temperature": {},
        })

    # ── Printer Pool Config ───────────────────────────────
    @app.route(prefix + "/api/pool/config", methods=["GET"])
    @app.route("/api/pool/config", methods=["GET"])
    @admin_required
    def pool_get_config():
        pool = app_config.get("pool", {})
        all_printers = [p["name"] for p in app_config.get("printers", [])]
        return jsonify({
            "enabled": pool.get("enabled", False),
            "printers": pool.get("printers", []),
            "all_printers": all_printers,
        })

    @app.route(prefix + "/api/pool/config", methods=["POST"])
    @app.route("/api/pool/config", methods=["POST"])
    @admin_required
    def pool_save_config():
        data = request.get_json(silent=True) or {}
        config_path = os.environ.get("FARM_CONFIG", "config/config.yaml")
        try:
            with open(config_path) as f:
                file_config = yaml.safe_load(f) or {}

            pool = file_config.get("pool", {})
            pool["enabled"] = bool(data.get("enabled", False))
            pool["printers"] = list(data.get("printers", []))
            file_config["pool"] = pool

            with open(config_path, "w") as f:
                yaml.dump(file_config, f, default_flow_style=False, sort_keys=False)

            app_config["pool"] = pool

            return jsonify({"ok": True, "message": "Pool configuration saved."})
        except Exception as e:
            logger.error(f"Failed to save pool config: {e}")
            return jsonify({"ok": False, "message": str(e)}), 500

    # ── Notification Config ───────────────────────────────
    @app.route(prefix + "/api/notifications/config", methods=["GET"])
    @app.route("/api/notifications/config", methods=["GET"])
    @admin_required
    def notifications_get_config():
        n = app_config.get("notifications", {})
        return jsonify({
            "enabled": n.get("enabled", False),
            "events": n.get("events", {}),
            "email": {k: v for k, v in n.get("email", {}).items() if k != "password"},
            "discord": n.get("discord", {}),
        })

    @app.route(prefix + "/api/notifications/config", methods=["POST"])
    @app.route("/api/notifications/config", methods=["POST"])
    @admin_required
    def notifications_save_config():
        data = request.get_json(silent=True) or {}
        config_path = os.environ.get("FARM_CONFIG", "config/config.yaml")
        try:
            with open(config_path) as f:
                file_config = yaml.safe_load(f) or {}

            n = file_config.get("notifications", {})
            n["enabled"] = bool(data.get("enabled", False))
            n["events"] = data.get("events", n.get("events", {}))

            # Email settings
            email_data = data.get("email", {})
            email = n.get("email", {})
            email["enabled"] = bool(email_data.get("enabled", False))
            email["smtp_host"] = email_data.get("smtp_host", email.get("smtp_host", ""))
            email["smtp_port"] = int(email_data.get("smtp_port", email.get("smtp_port", 587)))
            email["use_tls"] = bool(email_data.get("use_tls", email.get("use_tls", True)))
            email["username"] = email_data.get("username", email.get("username", ""))
            # Only update password if provided (non-empty)
            if email_data.get("password"):
                email["password"] = email_data["password"]
            email["from_address"] = email_data.get("from_address", email.get("from_address", ""))
            email["to_addresses"] = email_data.get("to_addresses", email.get("to_addresses", []))
            n["email"] = email

            # Discord settings
            discord_data = data.get("discord", {})
            discord = n.get("discord", {})
            discord["enabled"] = bool(discord_data.get("enabled", False))
            discord["webhook_url"] = discord_data.get("webhook_url", discord.get("webhook_url", ""))
            n["discord"] = discord

            file_config["notifications"] = n
            with open(config_path, "w") as f:
                yaml.dump(file_config, f, default_flow_style=False, sort_keys=False)

            app_config["notifications"] = n
            return jsonify({"ok": True, "message": "Notification settings saved."})
        except Exception as e:
            logger.error(f"Failed to save notification config: {e}")
            return jsonify({"ok": False, "message": str(e)}), 500

    @app.route(prefix + "/api/notifications/test/email", methods=["POST"])
    @app.route("/api/notifications/test/email", methods=["POST"])
    @admin_required
    def notifications_test_email():
        from .notifications import NotificationManager
        nm = NotificationManager(app_config)
        return jsonify(nm.test_email())

    @app.route(prefix + "/api/notifications/test/discord", methods=["POST"])
    @app.route("/api/notifications/test/discord", methods=["POST"])
    @admin_required
    def notifications_test_discord():
        from .notifications import NotificationManager
        nm = NotificationManager(app_config)
        return jsonify(nm.test_discord())

    # ── Software Update ───────────────────────────────────

    def _repo_dir():
        return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

    @app.route(prefix + "/api/update/check", methods=["GET"])
    @app.route("/api/update/check", methods=["GET"])
    @admin_required
    def update_check():
        """Check for available git updates without applying them."""
        git = shutil.which("git")
        if not git:
            return jsonify({"ok": False, "message": "git not found on this system"})
        repo = _repo_dir()
        try:
            # Ensure git trusts this directory regardless of ownership
            subprocess.run([git, "config", "--global", "--add", "safe.directory", repo],
                           capture_output=True, timeout=10)
            # Fetch latest refs from origin (no checkout)
            fetch = subprocess.run([git, "-C", repo, "fetch", "origin"],
                           capture_output=True, timeout=30, text=True)
            if fetch.returncode != 0:
                err = (fetch.stdout + fetch.stderr).strip()
                return jsonify({"ok": False, "message": f"git fetch failed: {err or 'check network/auth'}"})
            # Current short commit hash
            cur = subprocess.run([git, "-C", repo, "rev-parse", "--short", "HEAD"],
                                 capture_output=True, timeout=10, text=True)
            current_commit = cur.stdout.strip()
            # Commits on origin/main that are not in HEAD
            log = subprocess.run(
                [git, "-C", repo, "log", "HEAD..origin/main", "--oneline"],
                capture_output=True, timeout=10, text=True,
            )
            commits = [l.strip() for l in log.stdout.strip().splitlines() if l.strip()]
            return jsonify({
                "ok": True,
                "current_commit": current_commit,
                "updates_available": len(commits),
                "commits": commits,
            })
        except subprocess.TimeoutExpired:
            return jsonify({"ok": False, "message": "git fetch timed out"})
        except Exception as e:
            logger.error(f"Update check failed: {e}")
            return jsonify({"ok": False, "message": "Update check failed — see server logs"})

    @app.route(prefix + "/api/update/apply", methods=["POST"])
    @app.route("/api/update/apply", methods=["POST"])
    @admin_required
    def update_apply():
        """Run git pull and restart the service."""
        git = shutil.which("git")
        if not git:
            return jsonify({"ok": False, "message": "git not found on this system"})
        repo = _repo_dir()
        try:
            # Ensure git trusts this directory regardless of ownership
            subprocess.run([git, "config", "--global", "--add", "safe.directory", repo],
                           capture_output=True, timeout=10)
            result = subprocess.run(
                [git, "-C", repo, "pull", "--ff-only", "origin", "main"],
                capture_output=True, timeout=60, text=True,
            )
            output = (result.stdout + result.stderr).strip()
            if result.returncode != 0:
                return jsonify({"ok": False, "message": output or "git pull failed"})
            # Restart service after the response is delivered
            def _delayed_restart():
                time.sleep(2)
                _sudo_cmd(["systemctl", "restart", "the-print-farm"])
            threading.Thread(target=_delayed_restart, daemon=True).start()
            return jsonify({"ok": True, "message": output, "restarting": True})
        except subprocess.TimeoutExpired:
            return jsonify({"ok": False, "message": "git pull timed out"})
        except Exception as e:
            logger.error(f"Update apply failed: {e}")
            return jsonify({"ok": False, "message": "Update apply failed — see server logs"})

    # ── REST API v1 ───────────────────────────────────────
    api_v1 = create_api_v1(
        farm_manager=farm_manager,
        job_queue=job_queue,
        camera_manager=camera_manager,
        api_key=api_key,
        config=config,
        file_library=file_library,
        send_job_fn=_send_job_to_printer,
        parse_filaments_fn=parse_gcode_filaments,
        parse_model_name_fn=parse_gcode_model_name,
        parse_metadata_fn=parse_gcode_metadata,
        wrap_gcode_fn=wrap_gcode_as_3mf,
        spoolman_client=spoolman_client,
    )
    app.register_blueprint(api_v1, url_prefix=prefix + "/api/v1")
    if prefix:
        app.register_blueprint(api_v1, url_prefix="/api/v1", name="api_v1_nopfx")

    return app


def start_web_server(app, host="0.0.0.0", port=5000):
    """Start Flask in a background daemon thread."""
    thread = threading.Thread(
        target=lambda: app.run(host=host, port=port, debug=False, use_reloader=False),
        daemon=True,
        name="web-ui",
    )
    thread.start()
    logger.info(f"Web UI started at http://{host}:{port}")
    return thread
