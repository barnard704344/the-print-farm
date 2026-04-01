"""
Main entry point for the BambuLab Print Farm Manager.

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
from .camera import CameraManager
from .web import create_app, start_web_server

logger = logging.getLogger("bambulab_farm")


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


def cmd_run(args, config: dict):
    """Start the farm manager — connects to all printers and launches web UI."""
    printers = config.get("printers") or []
    farm = FarmManager(printers)
    queue_cfg = config.get("queue", {})
    queue = JobQueue(
        db_path=queue_cfg.get("db_path", "./data/farm.db"),
        upload_dir=queue_cfg.get("upload_dir", "./uploads"),
    )

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
                camera_mgr.start_camera(name, cfg["host"], cfg["access_code"])

    app = create_app(farm, queue, camera_manager=camera_mgr,
                     api_key=web_cfg.get("api_key", ""),
                     admin_password=web_cfg.get("admin_password", ""))
    start_web_server(app, host=host, port=port)
    print(f"Dashboard: http://{host}:{port}")

    # Auto-assign loop
    auto_assign = queue_cfg.get("auto_assign", True)
    if auto_assign:
        print("Auto-assign enabled: queued jobs will be sent to idle printers\n")

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

    # Main loop — handles auto-assignment
    try:
        while True:
            if auto_assign:
                idle_printers = farm.get_idle_printers()
                if idle_printers:
                    queued = queue.get_queued_jobs()
                    for job in queued:
                        if not idle_printers:
                            break
                        printer_name = idle_printers.pop(0)
                        printer = farm.get_printer(printer_name)
                        if not printer:
                            continue

                        logger.info(f"Auto-assigning job #{job['id']} to {printer_name}")
                        queue.assign_job(job["id"], printer_name)

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
                                queue.mark_failed(job["id"])
                                continue

                        # Upload the file to the printer
                        ok = printer.upload_file(file_path, remote_name)
                        if ok:
                            queue.mark_printing(job["id"])
                            # Wait for SD card to flush the file before starting print
                            # Without this delay, the P1S may get 0500-C010 (SD read/write error)
                            time.sleep(2)
                            # Start the print
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
                        elif state.status == PrintStatus.FAILED:
                            queue.mark_failed(job["id"])
                            logger.warning(f"Job #{job['id']} failed on {job['printer_name']}")

            time.sleep(5)
    except KeyboardInterrupt:
        signal_handler(None, None)


def cmd_status(args, config: dict):
    """Check connectivity to all printers."""
    print("=== BambuLab Print Farm Status ===\n")

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
        prog="bambulab-farm",
        description="BambuLab Print Farm Manager — manage multiple P1S printers via MQTT",
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
