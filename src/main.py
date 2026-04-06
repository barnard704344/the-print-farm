"""
Main entry point for The Print Farm Manager.

Usage:
    python -m src.main                  # Start the farm manager
    python -m src.main -c config.yaml   # Custom config path
    python -m src.main status           # Check connectivity to all printers
"""

import argparse
import logging
import logging.handlers
import os
import signal
import sys
import time
from pathlib import Path

import yaml

from .farm_manager import FarmManager
from .gcode_to_3mf import wrap_gcode_as_3mf
from .job_queue import JobQueue
from .file_library import FileLibrary
from .camera import CameraManager
from .spoolman_client import SpoolmanClient
from .notifications import NotificationManager
from .web import create_app, start_web_server

logger = logging.getLogger("the_print_farm")


def setup_logging(config: dict):
    log_config = config.get("logging", {})
    level = getattr(logging, log_config.get("level", "INFO").upper(), logging.INFO)
    log_file = log_config.get("file", "./logs/farm.log")

    log_dir = Path(log_file).parent
    log_dir.mkdir(parents=True, exist_ok=True)

    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    ))

    file_handler = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=log_config.get("max_size_mb", 10) * 1024 * 1024,
        backupCount=log_config.get("backup_count", 3),
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    ))

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(console)
    root.addHandler(file_handler)


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def _deduct_filament_usage(spoolman, job, farm):
    """After a job completes, report filament usage to Spoolman.

    Looks up spools assigned to the printer (by location) and deducts
    the estimated filament weight parsed from the G-code metadata.
    """
    try:
        from .gcode_to_3mf import parse_gcode_filaments
        printer_name = job.get("printer_name")
        if not printer_name:
            return

        file_path = job.get("file_path", "")
        if not file_path.lower().endswith(".gcode"):
            return

        info = parse_gcode_filaments(file_path)
        used_filaments = info.get("used_filaments", [])
        if not used_filaments:
            return

        total_weight_g = float(info.get("filament_used_g", 0))
        if total_weight_g <= 0:
            logger.debug(f"No filament weight in gcode for job #{job.get('id')}, skipping Spoolman deduction")
            return

        # Get spools assigned to this printer (location = printer name)
        printer_spools = spoolman.get_spools_by_location(printer_name)
        if not printer_spools:
            logger.debug(f"No Spoolman spools at location '{printer_name}', skipping usage deduction")
            return

        # Use per-filament weights when available (MMU gcode has per-slot values)
        per_weights = info.get("per_filament_weights_g", [])
        num_used = len(used_filaments)

        # For each used filament slot, try to match a spool and deduct weight
        for filament_info in used_filaments:
            slot = filament_info.get("slot", -1)

            # Prefer per-slot weight; fall back to splitting total evenly
            if per_weights and 0 <= slot < len(per_weights):
                weight_g = per_weights[slot]
            else:
                weight_g = total_weight_g / num_used

            if weight_g <= 0:
                continue

            material = (filament_info.get("type") or filament_info.get("material") or "").upper()

            # Find the best matching spool at this printer
            matched = None
            for spool in printer_spools:
                spool_material = (spool.get("filament", {}).get("material") or "").upper()
                if material and spool_material == material:
                    matched = spool
                    break
            # Fallback: use first available spool
            if not matched and printer_spools:
                matched = printer_spools[0]

            if matched:
                result = spoolman.use_spool(matched["id"], use_weight=weight_g)
                if result:
                    remaining = result.get("remaining_weight", "?")
                    logger.info(
                        f"Spoolman: deducted {weight_g:.1f}g from spool #{matched['id']} "
                        f"({matched.get('filament', {}).get('name', '?')}), "
                        f"{remaining}g remaining"
                    )
                else:
                    logger.warning(f"Spoolman: failed to deduct usage from spool #{matched['id']}")
    except Exception as e:
        logger.warning(f"Spoolman filament deduction failed for job #{job.get('id')}: {e}")


def cmd_run(args, config: dict):
    """Start the farm manager — connects to all printers and launches web UI."""
    printers = config.get("printers") or []
    farm = FarmManager(printers)
    queue_cfg = config.get("queue", {})
    queue = JobQueue(
        db_path=queue_cfg.get("db_path", "./data/farm.db"),
        upload_dir=queue_cfg.get("upload_dir", "./uploads"),
    )

    library = FileLibrary(
        db_path=queue_cfg.get("db_path", "./data/farm.db"),
        storage_dir=queue_cfg.get("upload_dir", "./uploads"),
    )
    library.backfill_from_jobs()

    # Connect to all printers (if any defined)
    if printers:
        print(f"Connecting to {len(printers)} printers...")
        results = farm.connect_all(timeout=10)
        for name, ok in results.items():
            status = "OK" if ok else "FAILED"
            print(f"  [{status}] {name}")
        connected = sum(1 for v in results.values() if v)
        print(f"\n{connected}/{len(results)} printers online\n")
    else:
        print("No printers configured — dashboard will start without printer connections.")
        print("Add printers to config/config.yaml and restart.\n")

    # Start web UI
    web_cfg = config.get("web", {})
    host = web_cfg.get("host", "0.0.0.0")
    port = web_cfg.get("port", 5000)

    # Camera manager — auto-start cameras for connected printers
    camera_mgr = CameraManager()
    if printers:
        for cfg in printers:
            name = cfg.get("name")
            p = farm.get_printer(name)
            if p and p.is_connected():
                printer_type = cfg.get("type", "bambulab").lower()
                if printer_type == "klipper":
                    camera_url = cfg.get("camera_url", "")
                    if not camera_url:
                        # Auto-detect from Moonraker
                        try:
                            import requests as _requests
                            base = f"http://{cfg['host']}:{cfg.get('moonraker_port', 7125)}"
                            resp = _requests.get(f"{base}/server/webcams/list", timeout=5)
                            if resp.status_code == 200:
                                webcams = resp.json().get("result", {}).get("webcams", [])
                                for wc in webcams:
                                    camera_url = wc.get("stream_url") or wc.get("snapshot_url") or ""
                                    if camera_url:
                                        if camera_url.startswith("/"):
                                            camera_url = f"http://{cfg['host']}{camera_url}"
                                        break
                        except Exception:
                            pass
                    if camera_url:
                        camera_mgr.start_http_camera(name, camera_url)
                else:
                    camera_mgr.start_camera(name, cfg["host"], cfg["access_code"])

    # Spoolman integration (optional)
    spoolman = None
    spoolman_cfg = config.get("spoolman", {})
    spoolman_url = spoolman_cfg.get("url", "")
    if spoolman_url:
        spoolman = SpoolmanClient(spoolman_url)
        info = spoolman.info()
        if info:
            print(f"Spoolman connected: {spoolman_url} (v{info.get('version', '?')})")
        else:
            print(f"WARNING: Spoolman configured but unreachable at {spoolman_url}")

    app = create_app(farm, queue, camera_manager=camera_mgr,
                     api_key=web_cfg.get("api_key", ""),
                     admin_password=web_cfg.get("admin_password", ""),
                     config=config, file_library=library,
                     spoolman_client=spoolman)
    start_web_server(app, host=host, port=port)
    print(f"Dashboard: http://{host}:{port}")

    # Notifications
    notifier = NotificationManager(config)
    if config.get("notifications", {}).get("enabled"):
        print("Notifications enabled")

    pool_cfg = config.get("pool", {})
    if pool_cfg.get("enabled"):
        print(f"Printer pool enabled: {pool_cfg.get('printers', [])}\n")

    print("=== Farm Manager Running ===")
    print("Press Ctrl+C to stop\n")

    def signal_handler(sig, frame):
        print("\nShutting down...")
        camera_mgr.stop_all()
        farm.disconnect_all()
        summary = farm.get_farm_summary()
        print(f"Disconnected {summary['total']} printers")
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)

    # Main loop — handles pool auto-dispatch and print completion tracking
    try:
        while True:
            # Pool auto-dispatch: send unassigned jobs to idle pool printers
            pool_cfg = config.get("pool", {})
            if pool_cfg.get("enabled"):
                pool_list = pool_cfg.get("printers", [])
                idle_printers = [
                    p for p in farm.get_idle_printers() if p in pool_list
                ]
                if idle_printers:
                    queued = queue.get_queued_jobs()
                    for job in queued:
                        if not idle_printers:
                            break
                        # Only auto-dispatch unassigned jobs (from generic port)
                        if job.get("printer_name"):
                            continue
                        printer_name = idle_printers.pop(0)
                        printer = farm.get_printer(printer_name)
                        if not printer:
                            continue

                        logger.info(f"Pool dispatch: job #{job['id']} → {printer_name}")
                        queue.assign_job(job["id"], printer_name)

                        file_path = job["file_path"]
                        remote_name = job["filename"]
                        printer_type = farm.get_printer_type(printer_name)

                        if printer_type == "klipper" and job.get("original_name"):
                            remote_name = job["original_name"]

                        if printer_type == "bambulab" and remote_name.lower().endswith(".gcode"):
                            threemf_path = file_path + ".3mf"
                            try:
                                wrap_gcode_as_3mf(file_path, threemf_path)
                                file_path = threemf_path
                                remote_name = remote_name.rsplit(".", 1)[0] + ".3mf"
                                logger.info(f"Wrapped gcode as 3mf: {remote_name}")
                            except Exception as e:
                                logger.error(f"Failed to wrap gcode as 3mf: {e}")
                                queue.mark_failed(job["id"])
                                continue
                        elif printer_type == "klipper" and remote_name.lower().endswith(".3mf"):
                            logger.error(f"Cannot send .3mf to Klipper printer '{printer_name}'")
                            queue.mark_failed(job["id"])
                            continue

                        ok = printer.upload_file(file_path, remote_name)
                        if ok:
                            queue.mark_printing(job["id"])
                            time.sleep(2)
                            printer.start_print(remote_name)
                            logger.info(f"Started printing job #{job['id']} on {printer_name}")
                        else:
                            queue.mark_failed(job["id"])
                            logger.error(f"Failed to upload job #{job['id']} to {printer_name}")

            # Check for completed prints (only jobs in 'printing' state, not 'uploading')
            active = queue.get_active_jobs()
            for job in active:
                if job["status"] != "printing":
                    continue
                if job["printer_name"]:
                    printer = farm.get_printer(job["printer_name"])
                    if printer and printer.is_connected():
                        state = printer.state
                        from .bambu_client import PrintStatus
                        from datetime import datetime, timezone

                        # Grace period: ignore printer status for 60s after job
                        # enters 'printing'. The printer needs time to transition
                        # from its old state (e.g. FAILED) to RUNNING for the new job.
                        started = job.get("started_at")
                        if started:
                            if isinstance(started, str):
                                started = datetime.fromisoformat(started)
                            age = (datetime.now(timezone.utc) - started).total_seconds()
                            if age < 60:
                                continue

                        if state.status == PrintStatus.FINISH:
                            queue.mark_completed(job["id"])
                            logger.info(f"Job #{job['id']} completed on {job['printer_name']}")
                            if spoolman:
                                _deduct_filament_usage(spoolman, job, farm)
                            notifier.notify(
                                "print_completed",
                                f"Print Completed — {job.get('original_name', job['filename'])}",
                                f"Job #{job['id']} finished on {job['printer_name']}.\nFile: {job.get('original_name', job['filename'])}",
                            )
                        elif state.status == PrintStatus.FAILED:
                            queue.mark_failed(job["id"])
                            logger.warning(f"Job #{job['id']} failed on {job['printer_name']}")
                            notifier.notify(
                                "print_failed",
                                f"Print Failed — {job.get('original_name', job['filename'])}",
                                f"Job #{job['id']} failed on {job['printer_name']}.\nFile: {job.get('original_name', job['filename'])}",
                            )

            time.sleep(5)
    except KeyboardInterrupt:
        signal_handler(None, None)


def cmd_status(args, config: dict):
    """Check connectivity to all printers."""
    print("=== The Print Farm — Status ===\n")

    printers = config.get("printers") or []
    if not printers:
        print("No printers configured in config/config.yaml")
        return

    farm = FarmManager(printers)
    results = farm.connect_all(timeout=5)

    for name, ok in results.items():
        printer = farm.get_printer(name)
        if ok:
            time.sleep(2)  # Wait for initial status
            s = printer.state
            print(f"[{name}] CONNECTED")
            print(f"  Status: {s.status.value}")
            if s.subtask_name:
                print(f"  Job: {s.subtask_name} ({s.mc_percent}%)")
                print(f"  Layer: {s.layer_num}/{s.total_layers} | ETA: {s.mc_remaining_time}min")
            print(f"  Temps — Nozzle: {s.nozzle_temper:.1f}/{s.nozzle_target_temper:.1f}°C | "
                  f"Bed: {s.bed_temper:.1f}/{s.bed_target_temper:.1f}°C | "
                  f"Chamber: {s.chamber_temper:.1f}°C")
            if s.nozzle_diameter:
                print(f"  Nozzle: {s.nozzle_diameter}mm {s.nozzle_type}")
            print(f"  WiFi: {s.wifi_signal} | Speed: {s.spd_mag}%")
        else:
            print(f"[{name}] OFFLINE — Check IP and access code")
        print()

    farm.disconnect_all()


def main():
    parser = argparse.ArgumentParser(
        prog="print-farm",
        description="The Print Farm — manage BambuLab and Klipper printers",
    )
    parser.add_argument(
        "-c", "--config",
        default="config/config.yaml",
        help="Path to config file (default: config/config.yaml)",
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")
    subparsers.add_parser("run", help="Start the farm manager (default)")
    subparsers.add_parser("status", help="Check connectivity to all printers")

    args = parser.parse_args()

    config = load_config(args.config)
    setup_logging(config)

    if args.command == "status":
        cmd_status(args, config)
    else:
        cmd_run(args, config)


if __name__ == "__main__":
    main()
