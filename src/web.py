"""
Flask Web Server + REST API for the BambuLab Print Farm.

Provides a dashboard and API endpoints to monitor printers,
manage the job queue, and control individual printers.
"""

import logging
import os
import re
import secrets
import threading
import time
import uuid
from functools import wraps
from typing import Optional

import yaml
from flask import Flask, Response, jsonify, render_template, request, send_from_directory, session
from werkzeug.utils import secure_filename

from .discovery import discover_printers, scan_subnet, get_local_subnets, test_bambu_connection
from .gcode_to_3mf import wrap_gcode_as_3mf, parse_gcode_filaments, parse_gcode_model_name

logger = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = {"gcode", "3mf"}


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def create_app(farm_manager, job_queue, camera_manager=None, api_key=None, admin_password=None):
    """Create the Flask app with references to farm manager, job queue, and camera manager."""
    app = Flask(
        __name__,
        template_folder=os.path.join(os.path.dirname(__file__), "..", "templates"),
        static_folder=os.path.join(os.path.dirname(__file__), "..", "static"),
    )
    app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024  # 500MB max upload
    app.secret_key = secrets.token_hex(32)

    # Support running behind a reverse proxy at /bambulab-farm
    prefix = os.environ.get("APP_PREFIX", "/bambulab-farm")

    def is_admin():
        return session.get("admin") is True

    def admin_required(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if not is_admin():
                return jsonify({"error": "Admin login required"}), 403
            return f(*args, **kwargs)
        return decorated

    @app.route(prefix + "/")
    @app.route(prefix)
    @app.route("/")
    def dashboard():
        return render_template("dashboard.html", prefix=prefix, api_key=api_key or "")

    @app.route(prefix + "/api/auth/status")
    @app.route("/api/auth/status")
    def auth_status():
        return jsonify({"admin": is_admin()})

    @app.route(prefix + "/api/auth/login", methods=["POST"])
    @app.route("/api/auth/login", methods=["POST"])
    def auth_login():
        data = request.get_json(silent=True) or {}
        password = data.get("password", "")
        if admin_password and password == admin_password:
            session["admin"] = True
            return jsonify({"ok": True})
        return jsonify({"ok": False, "error": "Invalid password"}), 401

    @app.route(prefix + "/api/auth/logout", methods=["POST"])
    @app.route("/api/auth/logout", methods=["POST"])
    def auth_logout():
        session.pop("admin", None)
        return jsonify({"ok": True})

    @app.route(prefix + "/static/<path:filename>")
    def prefixed_static(filename):
        return send_from_directory(app.static_folder, filename)

    # ── Farm API ──────────────────────────────────────────

    @app.route(prefix + "/api/farm/status")
    @app.route("/api/farm/status")
    def farm_status():
        """Full status of all printers + farm summary."""
        return jsonify({
            "summary": farm_manager.get_farm_summary(),
            "printers": farm_manager.get_all_states(),
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
    @admin_required
    def printer_pause(name):
        client = farm_manager.get_printer(name)
        if not client:
            return jsonify({"error": "Printer not found"}), 404
        ok = client.pause_print()
        return jsonify({"ok": ok})

    @app.route(prefix + "/api/printer/<name>/resume", methods=["POST"])
    @app.route("/api/printer/<name>/resume", methods=["POST"])
    @admin_required
    def printer_resume(name):
        client = farm_manager.get_printer(name)
        if not client:
            return jsonify({"error": "Printer not found"}), 404
        ok = client.resume_print()
        return jsonify({"ok": ok})

    @app.route(prefix + "/api/printer/<name>/stop", methods=["POST"])
    @app.route("/api/printer/<name>/stop", methods=["POST"])
    @admin_required
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
        ok = client.set_nozzle_temperature(temp)
        return jsonify({"ok": ok, "temp": temp})

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

    @app.route(prefix + "/api/jobs/history")
    @app.route("/api/jobs/history")
    def job_history():
        return jsonify(job_queue.get_history())

    @app.route(prefix + "/api/jobs/<int:job_id>")
    @app.route("/api/jobs/<int:job_id>")
    def get_job(job_id):
        job = job_queue.get_job(job_id)
        if not job:
            return jsonify({"error": "Job not found"}), 404
        return jsonify(job)

    @app.route(prefix + "/api/jobs/upload", methods=["POST"])
    @app.route("/api/jobs/upload", methods=["POST"])
    def upload_job():
        """Upload a G-code/3MF file and add it to the queue."""
        if "file" not in request.files:
            return jsonify({"error": "No file provided"}), 400

        file = request.files["file"]
        if not file.filename or not allowed_file(file.filename):
            return jsonify({"error": "Invalid file type. Allowed: .gcode"}), 400

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

        job_id = job_queue.add_job(
            filename=unique_name,
            original_name=original_name,
            file_path=file_path,
            copies=copies,
            priority=priority,
            notes=notes,
        )

        # If a specific printer was requested, assign and send immediately
        if printer:
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

            # Wrap .gcode into .3mf for the printer
            if remote_name.lower().endswith(".gcode"):
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

            # Upload the file to the printer
            ok = printer.upload_file(file_path, remote_name)
            if ok:
                job_queue.mark_printing(job_id)
                # Wait for SD card to flush the file before starting print
                time.sleep(2)
                printer.start_print(remote_name)
                logger.info(f"Started printing job #{job_id} on {printer_name}")
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
        """Check if a printer's AMS has the filaments a job needs.

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

        # Get printer's AMS state
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
    @admin_required
    def assign_job(job_id):
        data = request.get_json(silent=True) or {}
        printer_name = data.get("printer")
        printers = data.get("printers", [])

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
    @admin_required
    def reprint_job(job_id):
        """Create a new queued copy of an existing job."""
        new_id = job_queue.reprint_job(job_id)
        if new_id is None:
            return jsonify({"error": "Job not found"}), 404
        return jsonify({"ok": True, "job_id": new_id})

    @app.route(prefix + "/api/jobs/<int:job_id>/cancel", methods=["POST"])
    @app.route("/api/jobs/<int:job_id>/cancel", methods=["POST"])
    @admin_required
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
        ok = job_queue.requeue_job(job_id)
        return jsonify({"ok": ok})

    @app.route(prefix + "/api/jobs/<int:job_id>/delete", methods=["POST"])
    @app.route("/api/jobs/<int:job_id>/delete", methods=["POST"])
    @admin_required
    def delete_job(job_id):
        job = job_queue.get_job(job_id)
        if job and job["status"] == "printing" and job.get("printer_name"):
            printer = farm_manager.get_printer(job["printer_name"])
            if printer:
                printer.stop_print()
        ok = job_queue.delete_job(job_id)
        return jsonify({"ok": ok})

    # ── Discovery API ─────────────────────────────────────

    @app.route(prefix + "/api/discover/scan", methods=["POST"])
    @app.route("/api/discover/scan", methods=["POST"])
    @admin_required
    def discover_scan():
        """Listen for Bambu UDP broadcasts + optionally scan subnet."""
        data = request.get_json(silent=True) or {}
        timeout = min(float(data.get("timeout", 5)), 15)
        do_port_scan = data.get("port_scan", False)
        subnet = data.get("subnet", "")

        # UDP broadcast discovery
        printers = discover_printers(timeout=timeout)

        # Optional port scan fallback
        scan_results = []
        if do_port_scan:
            if not subnet:
                subnets = get_local_subnets()
            else:
                subnets = [subnet]
            for s in subnets:
                hosts = scan_subnet(s, timeout=1.0)
                # Filter out already-discovered IPs
                known_ips = {p["host"] for p in printers}
                for h in hosts:
                    if h not in known_ips:
                        scan_results.append({"host": h, "name": f"Unknown ({h})", "serial": "", "model": "Detected via port scan"})

        return jsonify({
            "discovered": printers,
            "port_scan": scan_results,
            "subnets": get_local_subnets(),
        })

    @app.route(prefix + "/api/discover/test", methods=["POST"])
    @app.route("/api/discover/test", methods=["POST"])
    @admin_required
    def discover_test():
        """Test MQTT connection to a printer."""
        data = request.get_json(silent=True) or {}
        host = data.get("host", "")
        access_code = data.get("access_code", "")
        serial = data.get("serial", "")

        if not host or not access_code or not serial:
            return jsonify({"ok": False, "message": "host, access_code, and serial are required"}), 400

        result = test_bambu_connection(host, access_code, serial)
        return jsonify(result)

    @app.route(prefix + "/api/discover/add", methods=["POST"])
    @app.route("/api/discover/add", methods=["POST"])
    @admin_required
    def discover_add():
        """Add a printer to the config and connect to it."""
        data = request.get_json(silent=True) or {}
        name = data.get("name", "").strip()
        host = data.get("host", "").strip()
        access_code = data.get("access_code", "").strip()
        serial = data.get("serial", "").strip()
        ams_serial = data.get("ams_serial", "").strip()

        if not all([name, host, access_code, serial]):
            return jsonify({"ok": False, "message": "name, host, access_code, and serial are required"}), 400

        # Check for duplicate name
        existing = farm_manager.get_printer(name)
        if existing:
            return jsonify({"ok": False, "message": f"Printer '{name}' already exists"}), 400

        # Save to config file
        config_path = os.environ.get("FARM_CONFIG", "config/config.yaml")
        try:
            with open(config_path) as f:
                config = yaml.safe_load(f) or {}

            if not config.get("printers"):
                config["printers"] = []

            new_printer = {
                "name": name,
                "host": host,
                "access_code": access_code,
                "serial": serial,
                "mqtt_port": int(data.get("mqtt_port", 8883)),
                "ftp_port": int(data.get("ftp_port", 990)),
                "camera_port": int(data.get("camera_port", 6000)),
            }
            if ams_serial:
                new_printer["ams_serial"] = ams_serial
            config["printers"].append(new_printer)

            with open(config_path, "w") as f:
                yaml.dump(config, f, default_flow_style=False, sort_keys=False)

            # Hot-add: create client and connect
            from .bambu_client import BambuClient
            client = BambuClient(
                name=name,
                host=host,
                access_code=access_code,
                serial=serial,
                port=new_printer["mqtt_port"],
                ftp_port=new_printer["ftp_port"],
                camera_port=new_printer["camera_port"],
                ams_serial=ams_serial,
            )
            farm_manager._printers[name] = client
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

        # Remove from config
        config_path = os.environ.get("FARM_CONFIG", "config/config.yaml")
        try:
            with open(config_path) as f:
                config = yaml.safe_load(f) or {}
            config["printers"] = [p for p in (config.get("printers") or []) if p.get("name") != name]
            with open(config_path, "w") as f:
                yaml.dump(config, f, default_flow_style=False, sort_keys=False)
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
            for p in config.get("printers", []):
                if p.get("name") == old_name:
                    p["name"] = new_name
                    break
            with open(config_path, "w") as f:
                yaml.dump(config, f, default_flow_style=False, sort_keys=False)
            return jsonify({"ok": True, "message": f"Printer renamed to '{new_name}'"})
        except Exception as e:
            logger.error(f"Failed to rename printer in config: {e}")
            return jsonify({"ok": False, "message": str(e)}), 500

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
            "text": "BambuLab Print Farm (OctoPrint-compat)",
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
                "printerProfiles": [{"id": "_default", "name": "BambuLab Farm"}],
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
    def octoprint_upload():
        """OctoPrint file upload — receives G-code from OrcaSlicer."""
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

        job_id = job_queue.add_job(
            filename=unique_name,
            original_name=original_name,
            file_path=file_path,
            copies=1,
            priority=10 if print_flag else 0,
            notes="Uploaded from OrcaSlicer",
        )

        logger.info(f"OrcaSlicer upload: {original_name} -> job {job_id} (print={print_flag})")

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
