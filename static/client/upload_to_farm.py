#!/usr/bin/env python3
"""
BambuLab Print Farm - OrcaSlicer Post-Processing Upload Script (Python)

Setup in OrcaSlicer:
  Print Settings → Others → Post-processing Scripts:
    python "C:\\path\\to\\upload_to_farm.py"     (Windows)
    python3 /path/to/upload_to_farm.py            (macOS/Linux)
"""

import os
import sys
import json
import urllib.request
import urllib.error
import mimetypes

# ─── Configuration ───────────────────────────────────────────
FARM_URL = "http://0941-webserver.seatonhs.internal/bambulab-farm"
API_KEY  = "bambulab-farm-2026"
# ─────────────────────────────────────────────────────────────


def upload_gcode(file_path: str) -> None:
    if not os.path.isfile(file_path):
        print(f"[Farm Upload] File not found: {file_path}")
        sys.exit(1)

    filename = os.path.basename(file_path)
    print(f"[Farm Upload] Uploading {filename} to print farm...")

    # Build multipart/form-data body
    boundary = "----FarmUploadBoundary"
    with open(file_path, "rb") as f:
        file_data = f.read()

    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: application/octet-stream\r\n"
        f"\r\n"
    ).encode("utf-8") + file_data + f"\r\n--{boundary}--\r\n".encode("utf-8")

    headers = {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "X-Api-Key": API_KEY,
    }

    req = urllib.request.Request(
        f"{FARM_URL}/api/jobs/upload",
        data=body,
        headers=headers,
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            if result.get("ok"):
                print(f"[Farm Upload] Success! Job ID: {result.get('job_id')}")
            else:
                print(f"[Farm Upload] Server returned: {json.dumps(result)}")
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")
        print(f"[Farm Upload] HTTP {e.code}: {error_body}")
        sys.exit(1)
    except Exception as e:
        print(f"[Farm Upload] Upload failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: upload_to_farm.py <gcode_file_path>")
        print("This script is meant to be used as an OrcaSlicer post-processing script.")
        sys.exit(1)

    upload_gcode(sys.argv[1])
