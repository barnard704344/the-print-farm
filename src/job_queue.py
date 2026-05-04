"""
Job Queue — SQLite-backed print job management.

Tracks uploaded files and job assignments to printers.
"""

import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class JobStatus(Enum):
    QUEUED = "queued"
    UPLOADING = "uploading"
    PRINTING = "printing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobQueue:
    """SQLite-backed print job queue."""

    def __init__(self, db_path: str, upload_dir: str):
        self.db_path = db_path
        self.upload_dir = upload_dir
        self._lock = threading.Lock()

        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        os.makedirs(upload_dir, exist_ok=True)

        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_db(self):
        conn = self._get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT NOT NULL,
                original_name TEXT NOT NULL,
                file_path TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                printer_name TEXT,
                priority INTEGER NOT NULL DEFAULT 0,
                copies_total INTEGER NOT NULL DEFAULT 1,
                copies_done INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT,
                notes TEXT,
                submitted_by TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS printer_gate_configs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                printer_name TEXT NOT NULL,
                gate INTEGER NOT NULL,
                material TEXT NOT NULL DEFAULT '',
                color TEXT NOT NULL DEFAULT '',
                spool_id INTEGER NOT NULL DEFAULT -1,
                updated_at TEXT NOT NULL,
                UNIQUE(printer_name, gate)
            );
        """)
        conn.commit()
        conn.close()

        # Migration: add submitted_by column if missing
        conn = self._get_conn()
        try:
            conn.execute("SELECT submitted_by FROM jobs LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute("ALTER TABLE jobs ADD COLUMN submitted_by TEXT NOT NULL DEFAULT ''")
            conn.commit()
            logger.info("Migrated jobs table: added submitted_by column")
        conn.close()

    def add_job(self, filename: str, original_name: str, file_path: str,
                copies: int = 1, priority: int = 0, notes: str = "",
                submitted_by: str = "", printer_name: str = "") -> int:
        """Add a new job to the queue. Returns the job ID."""
        with self._lock:
            conn = self._get_conn()
            cursor = conn.execute(
                """INSERT INTO jobs (filename, original_name, file_path, copies_total,
                   priority, notes, created_at, submitted_by, printer_name)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (filename, original_name, file_path, copies, priority, notes,
                 datetime.now(timezone.utc).isoformat(), submitted_by,
                 printer_name or None),
            )
            job_id = cursor.lastrowid
            conn.commit()
            conn.close()
            logger.info(f"Job #{job_id} added: {original_name} x{copies}")
            return job_id

    def get_job(self, job_id: int) -> Optional[dict]:
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        conn.close()
        return dict(row) if row else None

    def get_queued_jobs(self) -> list:
        """Get all queued jobs ordered by priority (desc) then created_at (asc)."""
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT * FROM jobs WHERE status = 'queued'
               ORDER BY priority DESC, created_at ASC"""
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def get_active_jobs(self) -> list:
        """Get all jobs currently printing or uploading."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM jobs WHERE status IN ('printing', 'uploading')"
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def get_all_jobs(self, limit: int = 100) -> list:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def assign_job(self, job_id: int, printer_name: str) -> bool:
        """Assign a queued job to a printer."""
        with self._lock:
            conn = self._get_conn()
            now = datetime.now(timezone.utc).isoformat()
            result = conn.execute(
                """UPDATE jobs SET status = 'uploading', printer_name = ?, started_at = ?
                   WHERE id = ? AND status = 'queued'""",
                (printer_name, now, job_id),
            )
            conn.commit()
            updated = result.rowcount > 0
            conn.close()
            if updated:
                logger.info(f"Job #{job_id} assigned to {printer_name}")
            return updated

    def mark_printing(self, job_id: int) -> bool:
        with self._lock:
            conn = self._get_conn()
            now = datetime.now(timezone.utc).isoformat()
            result = conn.execute(
                "UPDATE jobs SET status = 'printing', started_at = ? WHERE id = ? AND status = 'uploading'",
                (now, job_id),
            )
            conn.commit()
            conn.close()
            return result.rowcount > 0

    def mark_completed(self, job_id: int) -> bool:
        with self._lock:
            conn = self._get_conn()
            now = datetime.now(timezone.utc).isoformat()
            job = self.get_job(job_id)
            if not job:
                conn.close()
                return False

            new_copies_done = job["copies_done"] + 1

            if new_copies_done >= job["copies_total"]:
                # All copies done
                conn.execute(
                    """UPDATE jobs SET status = 'completed', copies_done = ?,
                       completed_at = ? WHERE id = ?""",
                    (new_copies_done, now, job_id),
                )
            else:
                # More copies needed — re-queue
                conn.execute(
                    """UPDATE jobs SET status = 'queued', copies_done = ?,
                       printer_name = NULL WHERE id = ?""",
                    (new_copies_done, job_id),
                )
            conn.commit()
            conn.close()
            logger.info(f"Job #{job_id} copy {new_copies_done}/{job['copies_total']} completed")
            return True

    def mark_failed(self, job_id: int) -> bool:
        with self._lock:
            conn = self._get_conn()
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "UPDATE jobs SET status = 'failed', completed_at = ? WHERE id = ?",
                (now, job_id),
            )
            conn.commit()
            conn.close()
            logger.info(f"Job #{job_id} failed")
            return True

    def cancel_job(self, job_id: int) -> bool:
        with self._lock:
            conn = self._get_conn()
            now = datetime.now(timezone.utc).isoformat()
            result = conn.execute(
                """UPDATE jobs SET status = 'cancelled', completed_at = ?
                   WHERE id = ? AND status IN ('queued', 'uploading', 'printing')""",
                (now, job_id),
            )
            conn.commit()
            conn.close()
            return result.rowcount > 0

    def requeue_job(self, job_id: int) -> bool:
        """Re-queue a failed or cancelled job."""
        with self._lock:
            conn = self._get_conn()
            result = conn.execute(
                """UPDATE jobs SET status = 'queued', printer_name = NULL,
                   started_at = NULL, completed_at = NULL
                   WHERE id = ? AND status IN ('failed', 'cancelled', 'completed')""",
                (job_id,),
            )
            conn.commit()
            conn.close()
            return result.rowcount > 0

    def reprint_job(self, job_id: int) -> Optional[int]:
        """Create a new queued job from an existing one (any status). Returns new job ID."""
        job = self.get_job(job_id)
        if not job:
            return None
        return self.add_job(
            filename=job["filename"],
            original_name=job["original_name"],
            file_path=job["file_path"],
            copies=1,
            priority=job["priority"],
            notes=f"Reprint of job #{job_id}",
            submitted_by=job.get("submitted_by", ""),
            printer_name=job.get("printer_name", ""),
        )

    def clone_job_for_printer(self, job_id: int) -> Optional[int]:
        """Clone a queued job so it can be assigned to another printer. Returns new job ID."""
        job = self.get_job(job_id)
        if not job:
            return None
        return self.add_job(
            filename=job["filename"],
            original_name=job["original_name"],
            file_path=job["file_path"],
            copies=1,
            priority=job["priority"],
            notes=job.get("notes", ""),
            submitted_by=job.get("submitted_by", ""),
            printer_name=job.get("printer_name", ""),
        )

    def delete_job(self, job_id: int) -> bool:
        """Delete a job and its file."""
        with self._lock:
            conn = self._get_conn()
            job = dict(conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone() or {})
            if not job:
                conn.close()
                return False

            conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
            conn.commit()

            # Only remove file if it's not in the file library
            file_path = job.get("file_path", "")
            in_library = False
            if file_path:
                try:
                    row = conn.execute(
                        "SELECT id FROM files WHERE file_path = ? LIMIT 1", (file_path,)
                    ).fetchone()
                    in_library = row is not None
                except Exception:
                    pass  # files table may not exist yet

            conn.close()

            if file_path and not in_library and os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except OSError:
                    pass
            return True

    def get_stats(self) -> dict:
        conn = self._get_conn()
        row = conn.execute("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN status='queued' THEN 1 ELSE 0 END) as queued,
                SUM(CASE WHEN status='printing' THEN 1 ELSE 0 END) as printing,
                SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) as completed,
                SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) as failed,
                SUM(CASE WHEN status='cancelled' THEN 1 ELSE 0 END) as cancelled
            FROM jobs
        """).fetchone()
        conn.close()
        return dict(row)

    # ── Printer Gate Configs (MMU / Happy Hare) ───────────

    def save_gate_config(self, printer_name: str, gate: int,
                         material: str = '', color: str = '',
                         spool_id: int = -1) -> None:
        """Persist an MMU gate's filament assignment (material, color, spool_id)."""
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            conn = self._get_conn()
            conn.execute(
                """INSERT INTO printer_gate_configs
                       (printer_name, gate, material, color, spool_id, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(printer_name, gate)
                   DO UPDATE SET material=excluded.material,
                                 color=excluded.color,
                                 spool_id=excluded.spool_id,
                                 updated_at=excluded.updated_at""",
                (printer_name, gate, material or '', color or '', spool_id, now),
            )
            conn.commit()
            conn.close()

    def get_gate_configs(self, printer_name: str) -> list:
        """Return all persisted gate configs for a printer as a list of dicts."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT gate, material, color, spool_id FROM printer_gate_configs "
            "WHERE printer_name = ? ORDER BY gate",
            (printer_name,),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
