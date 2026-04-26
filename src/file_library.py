"""
File Library — persistent storage for uploaded gcode/3mf files with folder organisation.

Files are preserved independently of the job queue so they can be reprinted later.
Thumbnails are extracted from OrcaSlicer gcode (base64 PNG comments) or 3mf archives.
"""

import base64
import logging
import os
import re
import shutil
import sqlite3
import threading
import zipfile
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


class FileLibrary:
    """SQLite-backed file library with folder support."""

    def __init__(self, db_path: str, storage_dir: str):
        self.db_path = db_path
        self.storage_dir = storage_dir
        self._lock = threading.Lock()

        os.makedirs(storage_dir, exist_ok=True)
        os.makedirs(os.path.join(storage_dir, "thumbnails"), exist_ok=True)
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
            CREATE TABLE IF NOT EXISTS folders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                parent_id INTEGER REFERENCES folders(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL,
                UNIQUE(name, parent_id)
            );

            CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                original_name TEXT NOT NULL,
                stored_name TEXT NOT NULL,
                file_path TEXT NOT NULL,
                thumbnail_path TEXT,
                folder_id INTEGER REFERENCES folders(id) ON DELETE SET NULL,
                file_size INTEGER NOT NULL DEFAULT 0,
                print_time_seconds INTEGER,
                filament_type TEXT,
                filament_weight_g REAL,
                layer_count INTEGER,
                uploaded_by TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                last_printed_at TEXT,
                print_count INTEGER NOT NULL DEFAULT 0
            );
        """)
        conn.commit()
        conn.close()

    # ── Folder operations ─────────────────────────────────

    def create_folder(self, name: str, parent_id: Optional[int] = None) -> dict:
        name = name.strip()
        if not name:
            return {"ok": False, "error": "Folder name is required"}
        with self._lock:
            conn = self._get_conn()
            now = datetime.now(timezone.utc).isoformat()
            try:
                conn.execute(
                    "INSERT INTO folders (name, parent_id, created_at) VALUES (?, ?, ?)",
                    (name, parent_id, now),
                )
                conn.commit()
                folder_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                conn.close()
                return {"ok": True, "id": folder_id}
            except sqlite3.IntegrityError:
                conn.close()
                return {"ok": False, "error": "Folder already exists"}

    def rename_folder(self, folder_id: int, name: str) -> dict:
        name = name.strip()
        if not name:
            return {"ok": False, "error": "Folder name is required"}
        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute("UPDATE folders SET name = ? WHERE id = ?", (name, folder_id))
                conn.commit()
                conn.close()
                return {"ok": True}
            except sqlite3.IntegrityError:
                conn.close()
                return {"ok": False, "error": "Folder name already exists"}

    def delete_folder(self, folder_id: int) -> dict:
        with self._lock:
            conn = self._get_conn()
            # Move files in this folder to root (no folder)
            conn.execute("UPDATE files SET folder_id = NULL WHERE folder_id = ?", (folder_id,))
            conn.execute("DELETE FROM folders WHERE id = ?", (folder_id,))
            conn.commit()
            conn.close()
            return {"ok": True}

    def get_folders(self, parent_id: Optional[int] = None) -> list:
        conn = self._get_conn()
        if parent_id is None:
            rows = conn.execute(
                "SELECT * FROM folders WHERE parent_id IS NULL ORDER BY name",
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM folders WHERE parent_id = ? ORDER BY name",
                (parent_id,),
            ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    # ── File operations ───────────────────────────────────

    def add_file(self, original_name: str, stored_name: str, file_path: str,
                 file_size: int, uploaded_by: str = "", folder_id: Optional[int] = None,
                 metadata: Optional[dict] = None, thumbnail_override: Optional[str] = None) -> int:
        meta = metadata or {}
        now = datetime.now(timezone.utc).isoformat()

        # Use provided thumbnail or try to extract one
        thumbnail_path = thumbnail_override or self._extract_thumbnail(file_path, stored_name)

        with self._lock:
            conn = self._get_conn()
            conn.execute(
                """INSERT INTO files (original_name, stored_name, file_path, thumbnail_path,
                   folder_id, file_size, print_time_seconds, filament_type,
                   filament_weight_g, layer_count, uploaded_by, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    original_name, stored_name, file_path, thumbnail_path,
                    folder_id, file_size,
                    meta.get("print_time_seconds"),
                    meta.get("filament_type"),
                    meta.get("filament_weight_g"),
                    meta.get("layer_count"),
                    uploaded_by, now,
                ),
            )
            conn.commit()
            file_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.close()
            return file_id

    def get_file(self, file_id: int) -> Optional[dict]:
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
        conn.close()
        return dict(row) if row else None

    def find_by_path(self, file_path: str) -> Optional[dict]:
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM files WHERE file_path = ? LIMIT 1", (file_path,)).fetchone()
        conn.close()
        return dict(row) if row else None

    def get_files(self, folder_id: Optional[int] = None) -> list:
        conn = self._get_conn()
        if folder_id is None:
            rows = conn.execute(
                "SELECT * FROM files WHERE folder_id IS NULL ORDER BY created_at DESC",
            ).fetchall()
        elif folder_id == -1:
            # All files
            rows = conn.execute(
                "SELECT * FROM files ORDER BY created_at DESC",
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM files WHERE folder_id = ? ORDER BY created_at DESC",
                (folder_id,),
            ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def move_file(self, file_id: int, folder_id: Optional[int]) -> dict:
        with self._lock:
            conn = self._get_conn()
            conn.execute("UPDATE files SET folder_id = ? WHERE id = ?", (folder_id, file_id))
            conn.commit()
            conn.close()
            return {"ok": True}

    def delete_file(self, file_id: int) -> dict:
        with self._lock:
            conn = self._get_conn()
            row = conn.execute("SELECT file_path, thumbnail_path FROM files WHERE id = ?", (file_id,)).fetchone()
            if not row:
                conn.close()
                return {"ok": False, "error": "File not found"}
            conn.execute("DELETE FROM files WHERE id = ?", (file_id,))
            conn.commit()
            conn.close()

            # Remove physical files
            for path in [row["file_path"], row["thumbnail_path"]]:
                if path and os.path.exists(path):
                    try:
                        os.remove(path)
                    except OSError:
                        pass
            return {"ok": True}

    def increment_print_count(self, file_id: int):
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            conn = self._get_conn()
            conn.execute(
                "UPDATE files SET print_count = print_count + 1, last_printed_at = ? WHERE id = ?",
                (now, file_id),
            )
            conn.commit()
            conn.close()

    def search_files(self, query: str) -> list:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM files WHERE original_name LIKE ? ORDER BY created_at DESC",
            (f"%{query}%",),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def backfill_from_jobs(self):
        """Import any job-queue files that aren't already in the library."""
        conn = self._get_conn()
        try:
            jobs = conn.execute(
                "SELECT filename, original_name, file_path, submitted_by, created_at FROM jobs"
            ).fetchall()
        except Exception:
            conn.close()
            return
        for job in jobs:
            existing = conn.execute(
                "SELECT id FROM files WHERE stored_name = ?", (job["filename"],)
            ).fetchone()
            if existing:
                continue
            fp = job["file_path"]
            if not os.path.exists(fp):
                continue
            try:
                meta = parse_gcode_metadata(fp)
                self.add_file(
                    original_name=job["original_name"],
                    stored_name=job["filename"],
                    file_path=fp,
                    file_size=os.path.getsize(fp),
                    uploaded_by=job["submitted_by"] or "",
                    metadata=meta,
                )
                logger.info(f"Backfilled library file: {job['original_name']}")
            except Exception as e:
                logger.warning(f"Failed to backfill {job['original_name']}: {e}")
        conn.close()

    # ── Thumbnail extraction ──────────────────────────────

    def _extract_thumbnail(self, file_path: str, stored_name: str) -> Optional[str]:
        """Try to extract a thumbnail image from the gcode or 3mf file."""
        try:
            if file_path.endswith(".3mf") and not file_path.endswith(".gcode.3mf"):
                result = self._extract_3mf_thumbnail(file_path, stored_name)
                if result:
                    return result
            # For raw gcode or wrapped gcode.3mf, look inside for gcode thumbnail comments
            result = self._extract_gcode_thumbnail(file_path, stored_name)
            if result:
                return result
            # Fallback: render toolpaths from gcode
            return self._render_gcode_thumbnail(file_path, stored_name)
        except Exception as e:
            logger.debug(f"Thumbnail extraction failed for {stored_name}: {e}")
            return None

    def _extract_3mf_thumbnail(self, file_path: str, stored_name: str) -> Optional[str]:
        """Extract thumbnail from a native OrcaSlicer .3mf file."""
        with zipfile.ZipFile(file_path) as z:
            # OrcaSlicer puts thumbnails in Metadata/plate_1.png or Metadata/thumbnail.png
            for candidate in ["Metadata/plate_1.png", "Metadata/top_1.png",
                              "Metadata/thumbnail.png", "Thumbnails/thumbnail.png"]:
                if candidate in z.namelist():
                    thumb_name = f"{stored_name}.thumb.png"
                    thumb_path = os.path.join(self.storage_dir, "thumbnails", thumb_name)
                    with z.open(candidate) as src, open(thumb_path, "wb") as dst:
                        dst.write(src.read())
                    return thumb_path
        return None

    def _extract_gcode_thumbnail(self, file_path: str, stored_name: str) -> Optional[str]:
        """Extract base64-encoded PNG thumbnail from gcode comments.

        OrcaSlicer format:
        ; thumbnail begin 300x300
        <base64 data lines preceded by ; >
        ; thumbnail end
        """
        gcode_text = None
        if file_path.endswith(".gcode.3mf"):
            # Read gcode from inside the 3mf wrapper
            try:
                with zipfile.ZipFile(file_path) as z:
                    if "Metadata/plate_1.gcode" in z.namelist():
                        with z.open("Metadata/plate_1.gcode") as gf:
                            # Only read first 500KB — thumbnails are at the top
                            gcode_text = gf.read(512 * 1024).decode("utf-8", errors="replace")
            except Exception:
                return None
        elif file_path.endswith(".gcode"):
            try:
                with open(file_path, "r", errors="replace") as f:
                    gcode_text = f.read(512 * 1024)
            except Exception:
                return None

        if not gcode_text:
            return None

        # Find the largest thumbnail (prefer 300x300 over 48x48)
        best_b64 = None
        best_size = 0
        for m in re.finditer(
            r";\s*thumbnail begin\s+(\d+)x(\d+)\s*\n(.*?);\s*thumbnail end",
            gcode_text, re.DOTALL
        ):
            w, h = int(m.group(1)), int(m.group(2))
            size = w * h
            if size > best_size:
                best_size = size
                # Strip leading '; ' from each line
                raw_lines = m.group(3).strip().split("\n")
                b64_data = "".join(line.lstrip("; ").strip() for line in raw_lines)
                best_b64 = b64_data

        if not best_b64:
            return None

        try:
            png_data = base64.b64decode(best_b64)
            thumb_name = f"{stored_name}.thumb.png"
            thumb_path = os.path.join(self.storage_dir, "thumbnails", thumb_name)
            with open(thumb_path, "wb") as f:
                f.write(png_data)
            return thumb_path
        except Exception as e:
            logger.debug(f"Failed to decode thumbnail for {stored_name}: {e}")
            return None

    @staticmethod
    def _tessellate_arc(cx, cy, cz, nx, ny, nz, i_off, j_off, clockwise, segments=8):
        """Expand a G2/G3 arc into a list of (x1,y1,z1,x2,y2,z2) line segments."""
        import math
        # Arc centre
        center_x = cx + i_off
        center_y = cy + j_off
        r = math.sqrt(i_off * i_off + j_off * j_off)
        if r < 0.001:
            return [(cx, cy, cz, nx, ny, nz)]

        start_angle = math.atan2(cy - center_y, cx - center_x)
        end_angle = math.atan2(ny - center_y, nx - center_x)

        if clockwise:  # G2
            if end_angle >= start_angle:
                end_angle -= 2 * math.pi
        else:  # G3
            if end_angle <= start_angle:
                end_angle += 2 * math.pi

        sweep = end_angle - start_angle
        # Adaptive segment count based on arc length
        arc_len = abs(sweep) * r
        n = max(3, min(segments, int(arc_len / 0.5)))

        result = []
        pz = cz
        px, py = cx, cy
        for s in range(1, n + 1):
            t = s / n
            a = start_angle + sweep * t
            sx = center_x + r * math.cos(a)
            sy = center_y + r * math.sin(a)
            sz = cz + (nz - cz) * t
            result.append((px, py, pz, sx, sy, sz))
            px, py, pz = sx, sy, sz
        # Snap last point to exact endpoint
        if result:
            lx1, ly1, lz1, _, _, _ = result[-1]
            result[-1] = (lx1, ly1, lz1, nx, ny, nz)
        return result

    def get_toolpath_data(self, file_id: int) -> Optional[dict]:
        """Return parsed gcode toolpath moves as JSON-friendly data for the 3D viewer."""
        conn = self._get_conn()
        row = conn.execute("SELECT file_path, stored_name FROM files WHERE id = ?", (file_id,)).fetchone()
        conn.close()
        if not row:
            return None

        gcode_lines = self._read_gcode_lines(row["file_path"])
        if not gcode_lines:
            return None

        SKIP_FEATURES = {"Sparse infill", "Internal solid infill", "Gap infill", "Custom"}

        moves = []
        cx, cy, cz = 0.0, 0.0, 0.0
        current_feature = ""
        for line in gcode_lines:
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith(";"):
                m = re.match(r";\s*(?:FEATURE|TYPE):\s*(.+)", stripped)
                if m:
                    current_feature = m.group(1).strip()
                continue
            parts = stripped.split(";")[0].split()
            if not parts or parts[0] not in ("G0", "G1", "G2", "G3"):
                continue
            params = {}
            for p in parts[1:]:
                if p and p[0] in "XYZEFIJxyzefij":
                    try:
                        params[p[0].upper()] = float(p[1:])
                    except ValueError:
                        pass
            nx = params.get("X", cx)
            ny = params.get("Y", cy)
            nz = params.get("Z", cz)
            has_extrusion = "E" in params and params["E"] > 0
            is_move = parts[0] in ("G1", "G2", "G3")
            if is_move and has_extrusion and (nx != cx or ny != cy):
                if current_feature and current_feature not in SKIP_FEATURES:
                    if parts[0] in ("G2", "G3") and ("I" in params or "J" in params):
                        arc_segs = self._tessellate_arc(
                            cx, cy, cz, nx, ny, nz,
                            params.get("I", 0), params.get("J", 0),
                            clockwise=(parts[0] == "G2"))
                        for seg in arc_segs:
                            moves.append((*seg, current_feature))
                    else:
                        moves.append((cx, cy, cz, nx, ny, nz, current_feature))
            cx, cy, cz = nx, ny, nz

        if not moves:
            return None

        # Downsample if too many moves (keep it under ~20k for browser perf)
        MAX_MOVES = 20000
        if len(moves) > MAX_MOVES:
            step = len(moves) / MAX_MOVES
            sampled = []
            i = 0.0
            while int(i) < len(moves):
                sampled.append(moves[int(i)])
                i += step
            moves = sampled

        # Compute bounds for centering
        all_x = [m[0] for m in moves] + [m[3] for m in moves]
        all_y = [m[1] for m in moves] + [m[4] for m in moves]
        all_z = [m[2] for m in moves] + [m[5] for m in moves]

        # Pack as flat arrays for compact transfer: [x1,y1,z1,x2,y2,z2, ...]
        positions = []
        features = []
        feat_map = {}
        feat_idx = 0
        for x1, y1, z1, x2, y2, z2, feat in moves:
            positions.extend([round(x1, 2), round(y1, 2), round(z1, 2),
                              round(x2, 2), round(y2, 2), round(z2, 2)])
            if feat not in feat_map:
                feat_map[feat] = feat_idx
                feat_idx += 1
            features.append(feat_map[feat])

        return {
            "positions": positions,
            "features": features,
            "feature_names": {v: k for k, v in feat_map.items()},
            "bounds": {
                "min": [round(min(all_x), 2), round(min(all_y), 2), round(min(all_z), 2)],
                "max": [round(max(all_x), 2), round(max(all_y), 2), round(max(all_z), 2)],
            },
            "count": len(moves),
        }

    def _render_gcode_thumbnail(self, file_path: str, stored_name: str) -> Optional[str]:
        """Generate an isometric 3D toolpath preview from gcode moves.

        Parses X/Y/Z coordinates and projects them with an isometric camera.
        Walls and surfaces are rendered with feature-based colouring;
        infill is skipped. Drawn back-to-front (painter's algorithm) and
        supersampled at 2x with LANCZOS downscale.
        """
        try:
            from PIL import Image, ImageDraw
        except ImportError:
            logger.debug("Pillow not installed, skipping gcode render")
            return None

        import math

        gcode_lines = self._read_gcode_lines(file_path)
        if not gcode_lines:
            return None

        SKIP_FEATURES = {"Sparse infill", "Internal solid infill", "Gap infill", "Custom"}
        SURFACE_FEATURES = {"Top surface", "Bottom surface", "Bridge", "Internal Bridge"}

        # Parse extrusion moves with XYZ + feature
        moves = []
        cx, cy, cz = 0.0, 0.0, 0.0
        current_feature = ""
        for line in gcode_lines:
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith(";"):
                m = re.match(r";\s*(?:FEATURE|TYPE):\s*(.+)", stripped)
                if m:
                    current_feature = m.group(1).strip()
                continue
            parts = stripped.split(";")[0].split()
            if not parts or parts[0] not in ("G0", "G1", "G2", "G3"):
                continue
            params = {}
            for p in parts[1:]:
                if p and p[0] in "XYZEFIJxyzefij":
                    try:
                        params[p[0].upper()] = float(p[1:])
                    except ValueError:
                        pass
            nx = params.get("X", cx)
            ny = params.get("Y", cy)
            nz = params.get("Z", cz)
            has_extrusion = "E" in params and params["E"] > 0
            is_move = parts[0] in ("G1", "G2", "G3")
            if is_move and has_extrusion and (nx != cx or ny != cy):
                if current_feature not in SKIP_FEATURES:
                    if parts[0] in ("G2", "G3") and ("I" in params or "J" in params):
                        arc_segs = self._tessellate_arc(
                            cx, cy, cz, nx, ny, nz,
                            params.get("I", 0), params.get("J", 0),
                            clockwise=(parts[0] == "G2"))
                        for seg in arc_segs:
                            moves.append((*seg, current_feature))
                    else:
                        moves.append((cx, cy, cz, nx, ny, nz, current_feature))
            cx, cy, cz = nx, ny, nz

        if not moves:
            return None

        # Downsample if too many moves to keep thumbnail generation fast
        MAX_THUMB_MOVES = 50000
        if len(moves) > MAX_THUMB_MOVES:
            step = len(moves) / MAX_THUMB_MOVES
            sampled = []
            i = 0.0
            while int(i) < len(moves):
                sampled.append(moves[int(i)])
                i += step
            moves = sampled

        # Centre the model at the origin for rotation
        all_x = [m[0] for m in moves] + [m[3] for m in moves]
        all_y = [m[1] for m in moves] + [m[4] for m in moves]
        all_z = [m[2] for m in moves] + [m[5] for m in moves]
        mid_x = (min(all_x) + max(all_x)) / 2
        mid_y = (min(all_y) + max(all_y)) / 2
        mid_z = (min(all_z) + max(all_z)) / 2
        max_z = max(all_z)

        # Isometric projection angles
        rot_z = math.radians(-45)   # rotate around Z (turntable)
        rot_x = math.radians(25)    # tilt down
        cos_z, sin_z = math.cos(rot_z), math.sin(rot_z)
        cos_x, sin_x = math.cos(rot_x), math.sin(rot_x)

        def project(x, y, z):
            """Project 3D point to 2D isometric screen coords + depth."""
            # Centre
            x -= mid_x
            y -= mid_y
            z -= mid_z
            # Rotate around Z axis
            rx = x * cos_z - y * sin_z
            ry = x * sin_z + y * cos_z
            rz = z
            # Rotate around X axis (tilt)
            fy = ry * cos_x - rz * sin_x
            fz = ry * sin_x + rz * cos_x
            fx = rx
            # Orthographic projection: screen_x = fx, screen_y = -fz (up on screen)
            return fx, -fz, fy  # (screen_x, screen_y, depth)

        # Project all moves and compute screen bounds
        projected = []
        for x1, y1, z1, x2, y2, z2, feat in moves:
            sx1, sy1, d1 = project(x1, y1, z1)
            sx2, sy2, d2 = project(x2, y2, z2)
            depth = (d1 + d2) / 2  # average depth for sorting
            projected.append((sx1, sy1, sx2, sy2, depth, z1, feat))

        all_sx = [p[0] for p in projected] + [p[2] for p in projected]
        all_sy = [p[1] for p in projected] + [p[3] for p in projected]
        min_sx, max_sx = min(all_sx), max(all_sx)
        min_sy, max_sy = min(all_sy), max(all_sy)
        range_sx = max_sx - min_sx or 1
        range_sy = max_sy - min_sy or 1

        # Render at 2x
        final_size = 400
        ss = 2
        size = final_size * ss
        margin = 30 * ss
        draw_size = size - 2 * margin
        scale = min(draw_size / range_sx, draw_size / range_sy)
        off_x = margin + (draw_size - range_sx * scale) / 2
        off_y = margin + (draw_size - range_sy * scale) / 2

        img = Image.new("RGB", (size, size), (24, 24, 28))
        draw = ImageDraw.Draw(img)

        # Draw subtle ground shadow ellipse
        g_cx = off_x + (0 - min_sx) * scale  # centre at mid
        g_cy_low = off_y + (max_sy - min_sy) * scale  # near bottom
        g_rx = range_sx * scale * 0.4
        g_ry = g_rx * 0.25
        for i in range(3, 0, -1):
            opacity = 28 + i * 4
            draw.ellipse([g_cx - g_rx - i * 4, g_cy_low - g_ry - i * 2,
                          g_cx + g_rx + i * 4, g_cy_low + g_ry + i * 2],
                         fill=(opacity, opacity, opacity))

        # Sort by depth (painter's algorithm: draw far objects first)
        projected.sort(key=lambda p: p[4])

        range_z = max_z - min(all_z) or 1

        def get_color(feature, z_val):
            t = (z_val - min(all_z)) / range_z
            if feature in ("Outer wall", "Overhang wall"):
                r = int(30 + 70 * t)
                g = int(170 + 85 * t)
                b = int(210 - 20 * t)
            elif feature == "Inner wall":
                r = int(40 + 30 * t)
                g = int(90 + 50 * t)
                b = int(150 + 30 * t)
            elif feature in SURFACE_FEATURES:
                r = int(190 + 60 * min(t, 1.0))
                g = int(150 + 50 * min(t, 1.0))
                b = int(50 + 30 * min(t, 1.0))
            else:
                r = int(50 + 40 * t)
                g = int(130 + 60 * t)
                b = int(180 + 30 * t)
            return (min(r, 255), min(g, 255), min(b, 255))

        for sx1, sy1, sx2, sy2, depth, z_val, feat in projected:
            color = get_color(feat, z_val)
            px1 = off_x + (sx1 - min_sx) * scale
            py1 = off_y + (sy1 - min_sy) * scale
            px2 = off_x + (sx2 - min_sx) * scale
            py2 = off_y + (sy2 - min_sy) * scale
            w = 2 * ss if feat in ("Outer wall", "Overhang wall") else 1 * ss
            draw.line([(px1, py1), (px2, py2)], fill=color, width=w)

        # Downscale
        img = img.resize((final_size, final_size), Image.LANCZOS)

        thumb_name = f"{stored_name}.thumb.png"
        thumb_path = os.path.join(self.storage_dir, "thumbnails", thumb_name)
        img.save(thumb_path, "PNG")
        logger.info(f"Generated 3D toolpath thumbnail for {stored_name}")
        return thumb_path

    def _read_gcode_lines(self, file_path: str, max_bytes: Optional[int] = None) -> list:
        """Read gcode lines from a .gcode, .3mf, or .gcode.3mf file.

        When *max_bytes* is None (the default) the entire file is read so that
        every layer of the print is available for thumbnail generation and the
        interactive 3-D viewer.  Pass an explicit value only when a hard upper
        bound on memory consumption is required.
        """
        if file_path.endswith(".3mf"):
            try:
                with zipfile.ZipFile(file_path) as z:
                    if "Metadata/plate_1.gcode" in z.namelist():
                        with z.open("Metadata/plate_1.gcode") as gf:
                            raw = gf.read(max_bytes) if max_bytes is not None else gf.read()
                            return raw.decode("utf-8", errors="replace").split("\n")
            except Exception:
                return []
        elif file_path.endswith(".gcode"):
            try:
                with open(file_path, "r", errors="replace") as f:
                    if max_bytes is not None:
                        return f.read(max_bytes).split("\n")
                    return f.read().split("\n")
            except Exception:
                return []
        return []


def parse_gcode_metadata(file_path: str) -> dict:
    """Extract print metadata from a gcode file (or gcode inside a .gcode.3mf)."""
    gcode_text = None
    if file_path.endswith(".3mf"):
        try:
            with zipfile.ZipFile(file_path) as z:
                if "Metadata/plate_1.gcode" in z.namelist():
                    with z.open("Metadata/plate_1.gcode") as gf:
                        gcode_text = gf.read(32768).decode("utf-8", errors="replace")
        except Exception:
            pass
    elif file_path.endswith(".gcode"):
        try:
            with open(file_path, "r", errors="replace") as f:
                gcode_text = f.read(32768)
        except Exception:
            pass

    if not gcode_text:
        return {}

    meta = {}

    # Total estimated time
    m = re.search(r";\s*total estimated time:\s*(?:(\d+)h\s*)?(?:(\d+)m\s*)?(?:(\d+)s)?", gcode_text)
    if m:
        hours = int(m.group(1) or 0)
        minutes = int(m.group(2) or 0)
        seconds = int(m.group(3) or 0)
        meta["print_time_seconds"] = hours * 3600 + minutes * 60 + seconds

    # Filament type
    m = re.search(r";\s*filament_type\s*=\s*(.+)", gcode_text)
    if m:
        meta["filament_type"] = m.group(1).split(";")[0].strip()

    # Filament weight
    # Read more of the file for footer metadata
    weight_text = gcode_text
    if file_path.endswith(".gcode"):
        try:
            with open(file_path, "r", errors="replace") as f:
                f.seek(max(0, os.path.getsize(file_path) - 4096))
                weight_text = f.read()
        except Exception:
            pass
    m = re.search(r";\s*filament used \[g\]\s*=\s*([\d.]+)", weight_text)
    if m:
        meta["filament_weight_g"] = float(m.group(1))

    # Layer count
    m = re.search(r";\s*total layer(?:s| number):\s*(\d+)", gcode_text)
    if m:
        meta["layer_count"] = int(m.group(1))

    return meta
