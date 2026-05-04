"""
REST API v1 Blueprint for The Print Farm.

Provides a proper RESTful interface with:
- Standard HTTP methods (GET, POST, PUT, PATCH, DELETE)
- Consistent JSON envelope responses
- API key authentication via X-Api-Key header
- Versioned URL prefix: /api/v1/
- OpenAPI-compatible structure
"""

import logging
import os
import re
import threading
import time
import uuid

from flask import Blueprint, jsonify, request, session
from werkzeug.utils import secure_filename

logger = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = {"gcode", "3mf"}


def _allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def _ok(data=None, status=200, meta=None):
    """Standard success envelope."""
    body = {"ok": True}
    if data is not None:
        body["data"] = data
    if meta:
        body["meta"] = meta
    return jsonify(body), status


def _error(message, status=400, code=None):
    """Standard error envelope."""
    body = {"ok": False, "error": {"message": message}}
    if code:
        body["error"]["code"] = code
    return jsonify(body), status


def create_api_v1(farm_manager, job_queue, camera_manager=None,
                  api_key=None, config=None, file_library=None,
                  send_job_fn=None, parse_filaments_fn=None,
                  parse_model_name_fn=None, parse_metadata_fn=None,
                  wrap_gcode_fn=None, spoolman_client=None):
    """
    Create and return the /api/v1 Blueprint.

    Parameters:
        farm_manager: FarmManager instance
        job_queue: JobQueue instance
        camera_manager: CameraManager instance (optional)
        api_key: API key string for authentication
        config: App config dict
        file_library: FileLibrary instance (optional)
        send_job_fn: callable(job_id, printer_name) to send a job to a printer
        parse_filaments_fn: callable(file_path) -> dict
        parse_model_name_fn: callable(file_path) -> str or None
        parse_metadata_fn: callable(file_path) -> dict
        wrap_gcode_fn: callable(gcode_path, output_path) -> None
        spoolman_client: SpoolmanClient instance (optional)
    """
    bp = Blueprint("api_v1", __name__, url_prefix="/api/v1")
    app_config = config or {}

    # ── Authentication ────────────────────────────────────

    def _check_api_key():
        if not api_key:
            return True  # No key configured = open access
        return request.headers.get("X-Api-Key", "") == api_key

    def _is_admin():
        return session.get("role") == "staff" or session.get("admin") is True

    def _is_authenticated():
        return session.get("role") in ("staff", "student") or session.get("admin") is True

    @bp.before_request
    def require_api_key():
        """All v1 endpoints require a valid API key or an authenticated session."""
        if not _check_api_key() and not _is_authenticated():
            return _error("Invalid or missing API key", 401, "AUTH_REQUIRED")

    def _admin_only(f):
        """Decorator: require staff/admin role (session) or just API key if no session."""
        from functools import wraps
        @wraps(f)
        def decorated(*args, **kwargs):
            # API key alone grants full access (for external integrations)
            # If session is active, require admin role
            if _is_authenticated() and not _is_admin():
                return _error("Admin privileges required", 403, "ADMIN_REQUIRED")
            return f(*args, **kwargs)
        return decorated

    # ── Server Info ───────────────────────────────────────

    @bp.route("/server", methods=["GET"])
    def server_info():
        """Server information and capabilities."""
        return _ok({
            "name": "The Print Farm",
            "api_version": "1.0",
            "capabilities": [
                "printers", "jobs", "library", "cameras",
            ],
        })

    # ── Farm / Printers ──────────────────────────────────

    @bp.route("/printers", methods=["GET"])
    def list_printers():
        """List all printers with their current state."""
        states = farm_manager.get_all_states()
        printers = list(states.values())
        summary = farm_manager.get_farm_summary()
        return _ok(printers, meta={"summary": summary})

    @bp.route("/printers/<name>", methods=["GET"])
    def get_printer(name):
        """Get a single printer's detailed state."""
        printer = farm_manager.get_printer(name)
        if not printer:
            return _error("Printer not found", 404, "PRINTER_NOT_FOUND")
        states = farm_manager.get_all_states()
        return _ok(states.get(name))

    @bp.route("/printers/<name>/command", methods=["POST"])
    def printer_command(name):
        """
        Send a command to a printer.

        Body: {"command": "<cmd>", ...params}
        Commands: pause, resume, stop, emergency_stop,
                  light, set_bed_temp, set_nozzle_temp,
                  unload_filament, load_filament
        """
        printer = farm_manager.get_printer(name)
        if not printer:
            return _error("Printer not found", 404, "PRINTER_NOT_FOUND")

        data = request.get_json(silent=True) or {}
        cmd = data.get("command", "").lower()

        if cmd == "pause":
            printer.pause_print()
            return _ok({"command": "pause", "printer": name})

        elif cmd == "resume":
            printer.resume_print()
            return _ok({"command": "resume", "printer": name})

        elif cmd == "stop":
            printer.stop_print()
            return _ok({"command": "stop", "printer": name})

        elif cmd == "emergency_stop":
            if hasattr(printer, "emergency_stop"):
                printer.emergency_stop()
            else:
                printer.stop_print()
            return _ok({"command": "emergency_stop", "printer": name})

        elif cmd == "light":
            state = data.get("state", "toggle")
            if hasattr(printer, "set_chamber_light"):
                printer.set_chamber_light(state)
            return _ok({"command": "light", "state": state, "printer": name})

        elif cmd == "set_bed_temp":
            temp = data.get("temperature")
            if temp is None:
                return _error("'temperature' is required", 400)
            printer.set_bed_temperature(int(temp))
            return _ok({"command": "set_bed_temp", "temperature": int(temp), "printer": name})

        elif cmd == "set_nozzle_temp":
            temp = data.get("temperature")
            if temp is None:
                return _error("'temperature' is required", 400)
            printer.set_nozzle_temperature(int(temp))
            return _ok({"command": "set_nozzle_temp", "temperature": int(temp), "printer": name})

        elif cmd == "unload_filament":
            printer.unload_filament()
            return _ok({"command": "unload_filament", "printer": name})

        elif cmd == "load_filament":
            printer.load_filament()
            return _ok({"command": "load_filament", "printer": name})

        else:
            return _error(f"Unknown command: {cmd}", 400, "UNKNOWN_COMMAND")

    # ── Happy Hare (MMU) ─────────────────────────────────

    # Known Happy Hare macro prefixes
    _HH_PREFIXES = ("MMU_", "_MMU_", "MMU ")

    # Categorised Happy Hare macros with descriptions and parameters
    _HH_MACRO_INFO = {
        "MMU_HOME": {"category": "Setup", "description": "Home the MMU selector and gear", "params": ["TOOL", "FORCE_UNLOAD"]},
        "MMU_SELECT": {"category": "Selection", "description": "Select a tool/gate", "params": ["TOOL", "GATE"]},
        "MMU_SELECT_BYPASS": {"category": "Selection", "description": "Select the bypass (filament passes straight through)"},
        "MMU_CHANGE_TOOL": {"category": "Selection", "description": "Perform a full tool change", "params": ["TOOL", "STANDALONE", "QUIET"]},
        "MMU_LOAD": {"category": "Filament", "description": "Load filament from gate to nozzle", "params": ["EXTRUDER_ONLY"]},
        "MMU_UNLOAD": {"category": "Filament", "description": "Unload filament from nozzle to gate", "params": ["EXTRUDER_ONLY"]},
        "MMU_EJECT": {"category": "Filament", "description": "Eject filament from the MMU", "params": ["TOOL", "GATE"]},
        "MMU_PRELOAD": {"category": "Filament", "description": "Preload filament to the gate sensor", "params": ["GATE"]},
        "MMU_CHECK_GATE": {"category": "Filament", "description": "Check if filament is present at gate(s)", "params": ["GATE", "TOOLS", "ALL"]},
        "MMU_RECOVER": {"category": "Recovery", "description": "Recover MMU state after an error"},
        "MMU_RESET": {"category": "Recovery", "description": "Reset MMU state and statistics", "params": ["CONFIRM"]},
        "MMU_PAUSE": {"category": "Control", "description": "Pause the MMU (user intervention required)"},
        "MMU_UNLOCK": {"category": "Control", "description": "Unlock the MMU after a pause/error"},
        "MMU_RESUME": {"category": "Control", "description": "Resume after MMU pause"},
        "MMU_SERVO": {"category": "Control", "description": "Control the servo position", "params": ["POS", "ANGLE"]},
        "MMU_MOTORS_OFF": {"category": "Control", "description": "Turn off MMU motors"},
        "MMU_ENCODER": {"category": "Calibration", "description": "Calibrate the encoder", "params": ["CALIBRATE", "RESET"]},
        "MMU_CALIBRATE_GEAR": {"category": "Calibration", "description": "Calibrate gear stepper rotation distance"},
        "MMU_CALIBRATE_ENCODER": {"category": "Calibration", "description": "Calibrate encoder resolution"},
        "MMU_CALIBRATE_SELECTOR": {"category": "Calibration", "description": "Calibrate selector positions"},
        "MMU_CALIBRATE_BOWDEN": {"category": "Calibration", "description": "Calibrate bowden tube length"},
        "MMU_CALIBRATE_GATES": {"category": "Calibration", "description": "Calibrate all gates"},
        "MMU_SOAKTEST_SELECTOR": {"category": "Calibration", "description": "Run selector soak test"},
        "MMU_SOAKTEST_LOAD": {"category": "Calibration", "description": "Run load/unload soak test"},
        "MMU_STATUS": {"category": "Info", "description": "Display MMU status", "params": ["DETAIL", "SHOWCONFIG"]},
        "MMU_STATS": {"category": "Info", "description": "Display MMU statistics", "params": ["RESET", "DETAIL", "TOTAL"]},
        "MMU_GATE_MAP": {"category": "Info", "description": "Display or update the gate map", "params": ["GATE", "MATERIAL", "COLOR", "SPOOL_ID", "AVAILABLE", "RESET"]},
        "MMU_TTG_MAP": {"category": "Info", "description": "Display or set tool-to-gate mapping", "params": ["TOOL", "GATE", "RESET"]},
        "MMU_ENDLESS_SPOOL": {"category": "Info", "description": "Display or set endless spool groups", "params": ["ENABLE", "GROUPS", "RESET"]},
        "MMU_SLICER_TOOL_MAP": {"category": "Info", "description": "Display slicer tool map", "params": ["DETAIL", "RESET", "PURGE_VOLUMES"]},
        "MMU_FORM_TIP": {"category": "Filament", "description": "Form filament tip for unloading", "params": ["FINAL_EJECT", "SHOW"]},
        "MMU_CUT_TIP": {"category": "Filament", "description": "Cut filament tip (if cutter installed)"},
        "MMU_REMAP_TTG": {"category": "Selection", "description": "Remap tool to gate", "params": ["TOOL", "GATE", "RESET"]},
        "MMU_TEST_CONFIG": {"category": "Calibration", "description": "Test MMU configuration"},
        "MMU_TEST_GRIP": {"category": "Calibration", "description": "Test the gear grip on filament"},
        "MMU_TEST_MOVE": {"category": "Calibration", "description": "Test a move", "params": ["MOVE", "SPEED", "ACCEL"]},
        "MMU_TEST_HOMING_MOVE": {"category": "Calibration", "description": "Test a homing move", "params": ["MOVE", "SPEED", "ACCEL", "ENDSTOP", "STOP_ON_ENDSTOP"]},
        "MMU_TEST_TRACKING": {"category": "Calibration", "description": "Test encoder tracking"},
        "MMU_TEST_LOAD": {"category": "Calibration", "description": "Test full load sequence"},
        "MMU_LOG": {"category": "Info", "description": "Set or query MMU log level", "params": ["LEVEL"]},
    }

    @bp.route("/printers/<name>/happyhare/macros", methods=["GET"])
    def happyhare_macros(name):
        """
        Get available Happy Hare macros for a Klipper printer.
        Queries Moonraker for gcode_macro objects and filters for MMU-related macros.
        """
        printer = farm_manager.get_printer(name)
        if not printer:
            return _error("Printer not found", 404, "PRINTER_NOT_FOUND")

        ptype = farm_manager.get_printer_type(name)
        if ptype != "klipper":
            return _error("Happy Hare is only available on Klipper printers", 400, "NOT_KLIPPER")

        if not hasattr(printer, '_has_mmu') or not printer._has_mmu:
            return _error("No Happy Hare/MMU detected on this printer", 404, "NO_MMU")

        # Query Moonraker for all available objects
        try:
            import requests as _requests
            base_url = f"http://{printer.host}:{printer.port}"
            resp = _requests.get(f"{base_url}/printer/objects/list", timeout=5)
            resp.raise_for_status()
            objects = resp.json().get("result", {}).get("objects", [])
        except Exception as e:
            return _error(f"Failed to query Moonraker: {e}", 502)

        # Extract gcode_macro names that are Happy Hare related
        macros = []
        for obj in objects:
            if not obj.startswith("gcode_macro "):
                continue
            macro_name = obj.replace("gcode_macro ", "")
            upper_name = macro_name.upper()
            if any(upper_name.startswith(p) for p in _HH_PREFIXES):
                info = _HH_MACRO_INFO.get(upper_name, {})
                macros.append({
                    "name": macro_name,
                    "category": info.get("category", "Other"),
                    "description": info.get("description", ""),
                    "params": info.get("params", []),
                })

        # Group by category
        categories = {}
        for m in macros:
            cat = m["category"]
            if cat not in categories:
                categories[cat] = []
            categories[cat].append(m)

        # Category display order
        cat_order = ["Setup", "Selection", "Filament", "Control", "Recovery", "Calibration", "Info", "Other"]
        ordered = []
        for cat in cat_order:
            if cat in categories:
                ordered.append({"name": cat, "macros": categories[cat]})
        for cat in categories:
            if cat not in cat_order:
                ordered.append({"name": cat, "macros": categories[cat]})

        return _ok({
            "printer": name,
            "mmu_state": printer.state.mmu if hasattr(printer.state, 'mmu') else None,
            "categories": ordered,
            "total_macros": len(macros),
        })

    @bp.route("/printers/<name>/happyhare/run", methods=["POST"])
    def happyhare_run(name):
        """
        Execute a Happy Hare macro on a Klipper printer.

        Body: {"macro": "MMU_HOME", "params": {"TOOL": "0", "FORCE_UNLOAD": "1"}}
        """
        printer = farm_manager.get_printer(name)
        if not printer:
            return _error("Printer not found", 404, "PRINTER_NOT_FOUND")

        ptype = farm_manager.get_printer_type(name)
        if ptype != "klipper":
            return _error("Happy Hare is only available on Klipper printers", 400, "NOT_KLIPPER")

        if not hasattr(printer, '_has_mmu') or not printer._has_mmu:
            return _error("No Happy Hare/MMU detected on this printer", 404, "NO_MMU")

        data = request.get_json(silent=True) or {}
        macro = data.get("macro", "").strip()
        if not macro:
            return _error("'macro' is required", 400)

        # Security: only allow Happy Hare macros
        upper_macro = macro.upper()
        if not any(upper_macro.startswith(p) for p in _HH_PREFIXES):
            return _error("Only Happy Hare macros (MMU_*) are allowed", 403, "MACRO_NOT_ALLOWED")

        # Build gcode command with parameters
        params = data.get("params", {})
        gcode = upper_macro
        for key, val in params.items():
            # Sanitise parameter names and values
            safe_key = "".join(c for c in str(key).upper() if c.isalnum() or c == '_')
            safe_val = str(val).strip()
            if safe_key and safe_val:
                gcode += f" {safe_key}={safe_val}"

        ok = printer.send_gcode(gcode)
        if ok:
            # If this is an MMU_GATE_MAP call, persist the gate assignment so it
            # survives Happy Hare clearing its state after a print completes.
            if upper_macro == "MMU_GATE_MAP" and "GATE" in params:
                try:
                    gate_idx = int(params["GATE"])
                    material = str(params.get("MATERIAL", ""))
                    color = params.get("COLOR", "")
                    if color and not color.startswith("#"):
                        color = "#" + color
                    spool_id = int(params["SPOOL_ID"]) if "SPOOL_ID" in params else -1
                    farm_manager.save_gate_config(name, gate_idx, material, color, spool_id)
                except (ValueError, TypeError):
                    pass  # Malformed params — don't crash the response

            return _ok({"executed": gcode, "printer": name})
        else:
            return _error("Failed to send command to printer", 502, "GCODE_FAILED")

    # ── Jobs ─────────────────────────────────────────────

    @bp.route("/jobs", methods=["GET"])
    def list_jobs():
        """
        List jobs with optional filtering.

        Query params:
            status: queued|printing|completed|failed|cancelled
            limit: max results (default 100)
        """
        status_filter = request.args.get("status")
        limit = request.args.get("limit", 100, type=int)

        if status_filter == "queued":
            jobs = job_queue.get_queued_jobs()
        elif status_filter in ("printing", "active"):
            jobs = job_queue.get_active_jobs()
        else:
            jobs = job_queue.get_all_jobs(limit=limit)

        stats = job_queue.get_stats()
        return _ok(jobs, meta={"stats": stats, "count": len(jobs)})

    @bp.route("/jobs/<int:job_id>", methods=["GET"])
    def get_job(job_id):
        """Get a single job's details."""
        job = job_queue.get_job(job_id)
        if not job:
            return _error("Job not found", 404, "JOB_NOT_FOUND")
        return _ok(job)

    @bp.route("/jobs", methods=["POST"])
    def create_job():
        """
        Upload a file and create a new print job.

        Multipart form data:
            file: .gcode or .3mf file (required)
            copies: number of copies (default 1)
            priority: queue priority (default 0)
            notes: optional notes
            printer: optional printer name for immediate assignment
        """
        if "file" not in request.files:
            return _error("No file provided", 400, "NO_FILE")

        file = request.files["file"]
        if not file.filename or not _allowed_file(file.filename):
            return _error("Invalid file type. Allowed: .gcode, .3mf", 400, "INVALID_FILE_TYPE")

        original_name = secure_filename(file.filename)
        unique_name = f"{uuid.uuid4().hex}_{original_name}"
        file_path = os.path.join(job_queue.upload_dir, unique_name)
        file.save(file_path)

        # Extract real model name from OrcaSlicer temp filenames
        if parse_model_name_fn and re.match(r"^\d+\.\d+\.gcode$", original_name):
            model_name = parse_model_name_fn(file_path)
            if model_name:
                original_name = model_name + ".gcode"

        copies = int(request.form.get("copies", 1))
        priority = int(request.form.get("priority", 0))
        notes = request.form.get("notes", "")
        printer_name = request.form.get("printer", "")
        submitted_by = session.get("username", "api")

        job_id = job_queue.add_job(
            filename=unique_name,
            original_name=original_name,
            file_path=file_path,
            copies=copies,
            priority=priority,
            notes=notes,
            submitted_by=submitted_by,
        )

        # Add to file library
        if file_library and parse_metadata_fn:
            try:
                meta = parse_metadata_fn(file_path)
                file_library.add_file(
                    original_name=original_name,
                    stored_name=unique_name,
                    file_path=file_path,
                    file_size=os.path.getsize(file_path),
                    uploaded_by=submitted_by,
                    metadata=meta,
                )
            except Exception as e:
                logger.warning(f"Failed to add file to library: {e}")

        # Assign to printer if requested
        if printer_name and send_job_fn:
            ok = job_queue.assign_job(job_id, printer_name)
            if ok:
                t = threading.Thread(target=send_job_fn, args=(job_id, printer_name), daemon=True)
                t.start()

        job = job_queue.get_job(job_id)
        return _ok(job, 201)

    @bp.route("/jobs/<int:job_id>", methods=["DELETE"])
    @_admin_only
    def delete_job(job_id):
        """Delete a job. Stops the print if active."""
        job = job_queue.get_job(job_id)
        if not job:
            return _error("Job not found", 404, "JOB_NOT_FOUND")
        if job["status"] == "printing" and job.get("printer_name"):
            printer = farm_manager.get_printer(job["printer_name"])
            if printer:
                printer.stop_print()
        ok = job_queue.delete_job(job_id)
        if not ok:
            return _error("Failed to delete job", 500)
        return "", 204

    @bp.route("/jobs/<int:job_id>/cancel", methods=["POST"])
    def cancel_job(job_id):
        """Cancel a queued or active job."""
        job = job_queue.get_job(job_id)
        if not job:
            return _error("Job not found", 404, "JOB_NOT_FOUND")
        if job["status"] == "printing" and job.get("printer_name"):
            printer = farm_manager.get_printer(job["printer_name"])
            if printer:
                printer.stop_print()
        ok = job_queue.cancel_job(job_id)
        return _ok({"cancelled": ok})

    @bp.route("/jobs/<int:job_id>/requeue", methods=["POST"])
    @_admin_only
    def requeue_job(job_id):
        """Re-queue a failed or cancelled job."""
        ok = job_queue.requeue_job(job_id)
        if not ok:
            return _error("Job not found or cannot be requeued", 404)
        job = job_queue.get_job(job_id)
        return _ok(job)

    @bp.route("/jobs/<int:job_id>/reprint", methods=["POST"])
    def reprint_job(job_id):
        """Create a new copy of a job, optionally sending to one or more printers."""
        data = request.get_json(silent=True) or {}
        printer_name = data.get("printer")
        printer_names = data.get("printers", [])

        if printer_name and not printer_names:
            printer_names = [printer_name]

        # Validate all target printers first when immediate send is requested
        for pname in printer_names:
            printer = farm_manager.get_printer(pname)
            if not printer:
                return _error(f"Printer '{pname}' not found", 404, "PRINTER_NOT_FOUND")

        new_id = job_queue.reprint_job(job_id)
        if new_id is None:
            return _error("Job not found", 404, "JOB_NOT_FOUND")

        # Backward-compatible behavior: create queued copy only
        if not printer_names:
            job = job_queue.get_job(new_id)
            return _ok(job, 201)

        results = []
        first = printer_names[0]
        ok = job_queue.assign_job(new_id, first)
        if ok and send_job_fn:
            t = threading.Thread(target=send_job_fn, args=(new_id, first), daemon=True)
            t.start()
        results.append({"printer": first, "job_id": new_id, "ok": bool(ok)})

        # Additional printers get cloned jobs for parallel copies
        for pname in printer_names[1:]:
            clone_id = job_queue.clone_job_for_printer(new_id)
            ok2 = job_queue.assign_job(clone_id, pname) if clone_id else False
            if ok2 and send_job_fn:
                t = threading.Thread(target=send_job_fn, args=(clone_id, pname), daemon=True)
                t.start()
            results.append({"printer": pname, "job_id": clone_id, "ok": bool(ok2)})

        job = job_queue.get_job(new_id)
        return _ok({"job": job, "results": results, "all_ok": all(r["ok"] for r in results)}, 201)

    @bp.route("/jobs/<int:job_id>/assign", methods=["POST"])
    @_admin_only
    def assign_job(job_id):
        """
        Assign a job to one or more printers.

        Body: {"printer": "name"} or {"printers": ["name1", "name2"]}
        """
        job = job_queue.get_job(job_id)
        if not job:
            return _error("Job not found", 404, "JOB_NOT_FOUND")
        if job["status"] != "queued":
            return _error("Only queued jobs can be assigned", 409, "INVALID_STATE")

        data = request.get_json(silent=True) or {}
        printer_name = data.get("printer")
        printer_names = data.get("printers", [])

        if printer_name:
            printer_names = [printer_name]
        if not printer_names:
            return _error("'printer' or 'printers' is required", 400)

        results = []
        for i, pname in enumerate(printer_names):
            if not farm_manager.get_printer(pname):
                results.append({"printer": pname, "ok": False, "error": "Printer not found"})
                continue
            if i == 0:
                ok = job_queue.assign_job(job_id, pname)
                jid = job_id
            else:
                jid = job_queue.clone_job_for_printer(job_id)
                ok = job_queue.assign_job(jid, pname) if jid else False

            if ok and send_job_fn:
                t = threading.Thread(target=send_job_fn, args=(jid, pname), daemon=True)
                t.start()
            results.append({"printer": pname, "job_id": jid, "ok": bool(ok)})

        return _ok(results)

    @bp.route("/jobs/<int:job_id>/filaments", methods=["GET"])
    def job_filaments(job_id):
        """Get filament requirements parsed from the G-code."""
        job = job_queue.get_job(job_id)
        if not job:
            return _error("Job not found", 404, "JOB_NOT_FOUND")
        if not parse_filaments_fn:
            return _ok({"filaments": [], "used_slots": [], "used_filaments": []})
        file_path = job["file_path"]
        if not file_path.lower().endswith(".gcode"):
            return _ok({"filaments": [], "used_slots": [], "used_filaments": []})
        try:
            info = parse_filaments_fn(file_path)
            return _ok(info)
        except Exception as e:
            logger.error(f"Failed to parse filaments for job #{job_id}: {e}")
            return _ok({"filaments": [], "used_slots": [], "used_filaments": []})

    # ── File Library ─────────────────────────────────────

    @bp.route("/library/files", methods=["GET"])
    def library_list_files():
        """List files in the library, optionally filtered by folder."""
        if not file_library:
            return _ok({"files": [], "folders": []})
        folder_id = request.args.get("folder_id", type=int)
        files = file_library.get_files(folder_id)
        folders = file_library.get_folders(folder_id)
        return _ok({"files": files, "folders": folders})

    @bp.route("/library/files/search", methods=["GET"])
    def library_search():
        """Search the file library. Query param: q=<search term>"""
        if not file_library:
            return _ok({"files": []})
        q = request.args.get("q", "")
        return _ok({"files": file_library.search_files(q)})

    @bp.route("/library/files/<int:file_id>", methods=["GET"])
    def library_get_file(file_id):
        """Get a single library file's metadata."""
        if not file_library:
            return _error("Library not available", 500)
        f = file_library.get_file(file_id)
        if not f:
            return _error("File not found", 404, "FILE_NOT_FOUND")
        return _ok(f)

    @bp.route("/library/files/<int:file_id>", methods=["PATCH"])
    def library_move_file(file_id):
        """Move a file to a different folder. Body: {"folder_id": <id|null>}"""
        if not file_library:
            return _error("Library not available", 500)
        data = request.get_json(silent=True) or {}
        folder_id = data.get("folder_id")
        result = file_library.move_file(file_id, folder_id)
        return _ok(result)

    @bp.route("/library/files/<int:file_id>", methods=["DELETE"])
    @_admin_only
    def library_delete_file(file_id):
        """Delete a file from the library."""
        if not file_library:
            return _error("Library not available", 500)
        result = file_library.delete_file(file_id)
        return _ok(result)

    @bp.route("/library/files/<int:file_id>/print", methods=["POST"])
    def library_print_file(file_id):
        """Create a new print job from a library file."""
        if not file_library:
            return _error("Library not available", 500)
        lib_file = file_library.get_file(file_id)
        if not lib_file:
            return _error("File not found", 404, "FILE_NOT_FOUND")
        if not os.path.exists(lib_file["file_path"]):
            return _error("File missing from disk", 404, "FILE_MISSING")

        new_job_id = job_queue.add_job(
            filename=lib_file["stored_name"],
            original_name=lib_file["original_name"],
            file_path=lib_file["file_path"],
            copies=1,
            priority=0,
            notes=f"Printed from library (file #{file_id})",
            submitted_by=session.get("username", "api"),
        )
        file_library.increment_print_count(file_id)
        job = job_queue.get_job(new_job_id)
        return _ok(job, 201)

    # ── Library Folders ──────────────────────────────────

    @bp.route("/library/folders", methods=["GET"])
    def library_list_folders():
        """List folders at root level or under a parent."""
        if not file_library:
            return _ok({"folders": []})
        parent_id = request.args.get("parent_id", type=int)
        return _ok({"folders": file_library.get_folders(parent_id)})

    @bp.route("/library/folders", methods=["POST"])
    def library_create_folder():
        """Create a folder. Body: {"name": "...", "parent_id": <optional>}"""
        if not file_library:
            return _error("Library not available", 500)
        data = request.get_json(silent=True) or {}
        name = data.get("name", "").strip()
        if not name:
            return _error("Folder name is required", 400)
        parent_id = data.get("parent_id")
        result = file_library.create_folder(name, parent_id)
        return _ok(result, 201)

    @bp.route("/library/folders/<int:folder_id>", methods=["PATCH"])
    def library_rename_folder(folder_id):
        """Rename a folder. Body: {"name": "new name"}"""
        if not file_library:
            return _error("Library not available", 500)
        data = request.get_json(silent=True) or {}
        name = data.get("name", "").strip()
        if not name:
            return _error("Folder name is required", 400)
        result = file_library.rename_folder(folder_id, name)
        return _ok(result)

    @bp.route("/library/folders/<int:folder_id>", methods=["DELETE"])
    @_admin_only
    def library_delete_folder(folder_id):
        """Delete a folder."""
        if not file_library:
            return _error("Library not available", 500)
        result = file_library.delete_folder(folder_id)
        return _ok(result)

    # ── Cameras ──────────────────────────────────────────

    @bp.route("/cameras", methods=["GET"])
    def camera_status():
        """Get camera status for all printers."""
        if not camera_manager:
            return _ok({"cameras": {}})
        status = {}
        for name in farm_manager.get_all_printers():
            status[name] = {
                "streaming": camera_manager.is_streaming(name),
            }
        return _ok({"cameras": status})

    @bp.route("/cameras/<name>/snapshot", methods=["GET"])
    def camera_snapshot(name):
        """Get the latest camera snapshot (JPEG)."""
        if not camera_manager:
            return _error("Camera manager not available", 500)
        frame = camera_manager.get_latest_frame(name)
        if frame is None:
            return _error("No snapshot available", 404, "NO_SNAPSHOT")
        from flask import Response
        return Response(frame, mimetype="image/jpeg")

    # ── Spoolman Integration ─────────────────────────────

    def _spoolman_required(f):
        """Decorator: return 503 if Spoolman is not configured."""
        from functools import wraps
        @wraps(f)
        def decorated(*args, **kwargs):
            if not spoolman_client:
                return _error("Spoolman is not configured. Set spoolman.url in config.yaml", 503, "SPOOLMAN_NOT_CONFIGURED")
            return f(*args, **kwargs)
        return decorated

    @bp.route("/spoolman/status", methods=["GET"])
    @_spoolman_required
    def spoolman_status():
        """Check Spoolman connectivity and server info."""
        info = spoolman_client.info()
        health = spoolman_client.health()
        if info is None:
            return _error("Cannot reach Spoolman server", 502, "SPOOLMAN_UNREACHABLE")
        return _ok({
            "connected": health is not None and health.get("status") == "healthy",
            "info": info,
            "health": health,
        })

    # ── Spoolman: Spools ─────────────────────────────────

    @bp.route("/spoolman/spools", methods=["GET"])
    @_spoolman_required
    def spoolman_list_spools():
        """
        List spools from Spoolman.

        Query params: filament.material, filament.vendor.name, location,
                      allow_archived (default false), sort, limit, offset
        """
        params = {}
        for key in ("filament.material", "filament.vendor.name", "filament.name",
                     "location", "allow_archived", "sort", "limit", "offset"):
            val = request.args.get(key)
            if val is not None:
                params[key] = val
        spools = spoolman_client.get_spools(**params)
        if spools is None:
            return _error("Failed to fetch spools from Spoolman", 502)
        return _ok(spools, meta={"count": len(spools)})

    @bp.route("/spoolman/spools/<int:spool_id>", methods=["GET"])
    @_spoolman_required
    def spoolman_get_spool(spool_id):
        """Get a single spool from Spoolman."""
        spool = spoolman_client.get_spool(spool_id)
        if spool is None:
            return _error("Spool not found", 404, "SPOOL_NOT_FOUND")
        return _ok(spool)

    @bp.route("/spoolman/spools", methods=["POST"])
    @_spoolman_required
    def spoolman_create_spool():
        """Create a spool in Spoolman. Body: Spoolman spool schema."""
        data = request.get_json(silent=True) or {}
        result = spoolman_client.create_spool(data)
        if result is None:
            return _error("Failed to create spool", 502)
        return _ok(result, 201)

    @bp.route("/spoolman/spools/<int:spool_id>", methods=["PATCH"])
    @_spoolman_required
    def spoolman_update_spool(spool_id):
        """Update a spool in Spoolman."""
        data = request.get_json(silent=True) or {}
        result = spoolman_client.update_spool(spool_id, data)
        if result is None:
            return _error("Failed to update spool (not found?)", 404)
        return _ok(result)

    @bp.route("/spoolman/spools/<int:spool_id>", methods=["DELETE"])
    @_spoolman_required
    @_admin_only
    def spoolman_delete_spool(spool_id):
        """Delete a spool in Spoolman."""
        ok = spoolman_client.delete_spool(spool_id)
        if not ok:
            return _error("Failed to delete spool", 404)
        return "", 204

    @bp.route("/spoolman/spools/<int:spool_id>/use", methods=["POST"])
    @_spoolman_required
    def spoolman_use_spool(spool_id):
        """
        Consume filament from a spool.

        Body: {"use_weight": <grams>} or {"use_length": <mm>}
        """
        data = request.get_json(silent=True) or {}
        result = spoolman_client.use_spool(
            spool_id,
            use_weight=data.get("use_weight"),
            use_length=data.get("use_length"),
        )
        if result is None:
            return _error("Failed to update spool usage", 502)
        return _ok(result)

    # ── Spoolman: Filaments ──────────────────────────────

    @bp.route("/spoolman/filaments", methods=["GET"])
    @_spoolman_required
    def spoolman_list_filaments():
        """List filament types from Spoolman."""
        params = {}
        for key in ("name", "material", "vendor.name", "vendor.id", "sort", "limit", "offset"):
            val = request.args.get(key)
            if val is not None:
                params[key] = val
        filaments = spoolman_client.get_filaments(**params)
        if filaments is None:
            return _error("Failed to fetch filaments from Spoolman", 502)
        return _ok(filaments, meta={"count": len(filaments)})

    @bp.route("/spoolman/filaments/<int:filament_id>", methods=["GET"])
    @_spoolman_required
    def spoolman_get_filament(filament_id):
        """Get a single filament type from Spoolman."""
        filament = spoolman_client.get_filament(filament_id)
        if filament is None:
            return _error("Filament not found", 404, "FILAMENT_NOT_FOUND")
        return _ok(filament)

    # ── Spoolman: Vendors ────────────────────────────────

    @bp.route("/spoolman/vendors", methods=["GET"])
    @_spoolman_required
    def spoolman_list_vendors():
        """List vendors from Spoolman."""
        params = {}
        for key in ("name", "sort", "limit", "offset"):
            val = request.args.get(key)
            if val is not None:
                params[key] = val
        vendors = spoolman_client.get_vendors(**params)
        if vendors is None:
            return _error("Failed to fetch vendors from Spoolman", 502)
        return _ok(vendors, meta={"count": len(vendors)})

    @bp.route("/spoolman/vendors/<int:vendor_id>", methods=["GET"])
    @_spoolman_required
    def spoolman_get_vendor(vendor_id):
        """Get a single vendor from Spoolman."""
        vendor = spoolman_client.get_vendor(vendor_id)
        if vendor is None:
            return _error("Vendor not found", 404, "VENDOR_NOT_FOUND")
        return _ok(vendor)

    # ── Spoolman: Printer Spool Mapping ──────────────────

    @bp.route("/spoolman/printers/<name>/spools", methods=["GET"])
    @_spoolman_required
    def spoolman_printer_spools(name):
        """
        Get spools located at a specific printer.
        Uses Spoolman's location field to match printer names.
        """
        if not farm_manager.get_printer(name):
            return _error("Printer not found", 404, "PRINTER_NOT_FOUND")
        spools = spoolman_client.get_spools_by_location(name)
        if spools is None:
            return _error("Failed to fetch spools from Spoolman", 502)
        return _ok(spools)

    @bp.route("/spoolman/printers/<name>/spools/<int:spool_id>", methods=["PUT"])
    @_spoolman_required
    def spoolman_assign_spool_to_printer(name, spool_id):
        """Assign a spool to a printer by setting its location in Spoolman."""
        if not farm_manager.get_printer(name):
            return _error("Printer not found", 404, "PRINTER_NOT_FOUND")
        result = spoolman_client.update_spool(spool_id, {"location": name})
        if result is None:
            return _error("Failed to update spool location", 502)
        return _ok(result)

    @bp.route("/spoolman/printers/<name>/spools/<int:spool_id>", methods=["DELETE"])
    @_spoolman_required
    def spoolman_remove_spool_from_printer(name, spool_id):
        """Remove a spool's printer assignment (clear its location)."""
        result = spoolman_client.update_spool(spool_id, {"location": ""})
        if result is None:
            return _error("Failed to update spool location", 502)
        return _ok(result)

    # ── OpenAPI Spec ─────────────────────────────────────

    @bp.route("/openapi.json", methods=["GET"])
    def openapi_spec():
        """Serve a minimal OpenAPI 3.0 spec for discoverability."""
        spec = {
            "openapi": "3.0.3",
            "info": {
                "title": "The Print Farm API",
                "version": "1.0.0",
                "description": "REST API for managing a 3D printer farm (BambuLab + Klipper).",
            },
            "servers": [
                {"url": "/api/v1", "description": "V1 API"},
            ],
            "security": [{"ApiKeyAuth": []}],
            "components": {
                "securitySchemes": {
                    "ApiKeyAuth": {
                        "type": "apiKey",
                        "in": "header",
                        "name": "X-Api-Key",
                    },
                },
            },
            "paths": {
                "/server": {
                    "get": {"summary": "Server info", "tags": ["Server"]},
                },
                "/printers": {
                    "get": {"summary": "List all printers", "tags": ["Printers"]},
                },
                "/printers/{name}": {
                    "get": {"summary": "Get printer details", "tags": ["Printers"]},
                },
                "/printers/{name}/command": {
                    "post": {
                        "summary": "Send command to printer",
                        "tags": ["Printers"],
                        "description": "Commands: pause, resume, stop, emergency_stop, light, set_bed_temp, set_nozzle_temp, unload_filament, load_filament",
                    },
                },
                "/jobs": {
                    "get": {"summary": "List jobs", "tags": ["Jobs"]},
                    "post": {"summary": "Create job (file upload)", "tags": ["Jobs"]},
                },
                "/jobs/{job_id}": {
                    "get": {"summary": "Get job details", "tags": ["Jobs"]},
                    "delete": {"summary": "Delete job", "tags": ["Jobs"]},
                },
                "/jobs/{job_id}/cancel": {
                    "post": {"summary": "Cancel job", "tags": ["Jobs"]},
                },
                "/jobs/{job_id}/requeue": {
                    "post": {"summary": "Requeue job", "tags": ["Jobs"]},
                },
                "/jobs/{job_id}/reprint": {
                    "post": {
                        "summary": "Reprint job",
                        "tags": ["Jobs"],
                        "description": "Create a new reprint job. Optionally provide a target printer or list of printers to dispatch copies immediately.",
                        "requestBody": {
                            "required": False,
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "printer": {
                                                "type": "string",
                                                "description": "Single target printer name",
                                            },
                                            "printers": {
                                                "type": "array",
                                                "items": {"type": "string"},
                                                "description": "Multiple target printers for parallel copies",
                                            },
                                        },
                                    },
                                    "examples": {
                                        "queued_only": {
                                            "summary": "Create queued reprint only",
                                            "value": {},
                                        },
                                        "single_printer": {
                                            "summary": "Reprint and send to one printer",
                                            "value": {"printer": "voron"},
                                        },
                                        "multi_printer": {
                                            "summary": "Reprint and send parallel copies",
                                            "value": {"printers": ["voron", "P1S-1"]},
                                        },
                                    },
                                },
                            },
                        },
                    },
                },
                "/jobs/{job_id}/assign": {
                    "post": {"summary": "Assign job to printer(s)", "tags": ["Jobs"]},
                },
                "/jobs/{job_id}/filaments": {
                    "get": {"summary": "Get filament requirements", "tags": ["Jobs"]},
                },
                "/library/files": {
                    "get": {"summary": "List library files", "tags": ["Library"]},
                },
                "/library/files/search": {
                    "get": {"summary": "Search library", "tags": ["Library"]},
                },
                "/library/files/{file_id}": {
                    "get": {"summary": "Get file details", "tags": ["Library"]},
                    "patch": {"summary": "Move file to folder", "tags": ["Library"]},
                    "delete": {"summary": "Delete file", "tags": ["Library"]},
                },
                "/library/files/{file_id}/print": {
                    "post": {"summary": "Print from library", "tags": ["Library"]},
                },
                "/library/folders": {
                    "get": {"summary": "List folders", "tags": ["Library"]},
                    "post": {"summary": "Create folder", "tags": ["Library"]},
                },
                "/library/folders/{folder_id}": {
                    "patch": {"summary": "Rename folder", "tags": ["Library"]},
                    "delete": {"summary": "Delete folder", "tags": ["Library"]},
                },
                "/cameras": {
                    "get": {"summary": "Camera status", "tags": ["Cameras"]},
                },
                "/cameras/{name}/snapshot": {
                    "get": {"summary": "Get camera snapshot", "tags": ["Cameras"]},
                },
                "/spoolman/status": {
                    "get": {"summary": "Spoolman connection status", "tags": ["Spoolman"]},
                },
                "/spoolman/spools": {
                    "get": {"summary": "List spools", "tags": ["Spoolman"]},
                    "post": {"summary": "Create spool", "tags": ["Spoolman"]},
                },
                "/spoolman/spools/{spool_id}": {
                    "get": {"summary": "Get spool", "tags": ["Spoolman"]},
                    "patch": {"summary": "Update spool", "tags": ["Spoolman"]},
                    "delete": {"summary": "Delete spool", "tags": ["Spoolman"]},
                },
                "/spoolman/spools/{spool_id}/use": {
                    "post": {"summary": "Consume filament from spool", "tags": ["Spoolman"]},
                },
                "/spoolman/filaments": {
                    "get": {"summary": "List filament types", "tags": ["Spoolman"]},
                },
                "/spoolman/filaments/{filament_id}": {
                    "get": {"summary": "Get filament type", "tags": ["Spoolman"]},
                },
                "/spoolman/vendors": {
                    "get": {"summary": "List vendors", "tags": ["Spoolman"]},
                },
                "/spoolman/vendors/{vendor_id}": {
                    "get": {"summary": "Get vendor", "tags": ["Spoolman"]},
                },
                "/spoolman/printers/{name}/spools": {
                    "get": {"summary": "Get spools at printer", "tags": ["Spoolman"]},
                },
                "/spoolman/printers/{name}/spools/{spool_id}": {
                    "put": {"summary": "Assign spool to printer", "tags": ["Spoolman"]},
                    "delete": {"summary": "Remove spool from printer", "tags": ["Spoolman"]},
                },
            },
        }
        return jsonify(spec)

    return bp
