# The Print Farm

A web-based print farm manager for **BambuLab** and **Klipper** 3D printers. Monitor, control, and queue print jobs across multiple printers from a single dashboard.

## Features

- **Multi-printer support** — BambuLab (P1S, X1C, A1) via MQTT/FTPS and Klipper via Moonraker HTTP API
- **Real-time dashboard** — Live status, temperatures, progress, and camera feeds
- **Job queue** — Upload G-code, queue jobs, auto-assign to idle printers
- **File library** — Persistent storage with folder organisation, search, and 3D interactive viewer
- **Printer discovery** — Auto-detect BambuLab (UDP broadcast) and Klipper (Moonraker port scan)
- **Authentication** — Local users, Active Directory/LDAP, student/staff roles
- **OrcaSlicer integration** — Slice and print directly from OrcaSlicer via virtual printers (OctoPrint-compatible) — no batch files needed
- **AMS support** — Full filament tray management for BambuLab printers with AMS
- **Camera streaming** — Live camera feeds from BambuLab printers and Klipper webcams (MJPEG/snapshot auto-detected via Moonraker)
- **Obico integration** — If a local Obico server is running and the Obico plugin is installed on your Klipper printer, the dashboard will automatically pull failure detection data and remote monitoring info from it
- **Klipper Adaptive Flow** — If [Klipper Adaptive Flow](https://github.com/barnard704344/Klipper-Adaptive-Flow) is installed on a Klipper printer, the dashboard auto-detects it and shows a direct link to the Adaptive Flow analysis dashboard on the printer card

### REST API v1

A full RESTful API at `/api/v1/` for external integrations:

- **19+ endpoints** with consistent JSON envelope (`{ok, data, error, meta}`)
- **API key authentication** via `X-Api-Key` header
- Printers: list, status, commands (pause/resume/stop/temps/filament)
- Jobs: create, list, status, assign, cancel, delete
- File library: list, upload, download, delete
- Cameras: snapshot, streaming control
- OpenAPI 3.0 spec at `/api/v1/openapi.json`

### Happy Hare MMU Integration

Full control of Happy Hare (multi-material unit) on Klipper printers:

- **Auto-detected** gate status, active tool, filament state, and encoder data
- **Macro control modal** — Click the MMU section on any printer card to open a modal with all Happy Hare macros organised by category (Selection, Filament, Control, Calibration, Info, Recovery)
- **Parameter support** — Macros with parameters show an input dialog with defaults
- **Gate configuration** — Click any loaded gate to set material type and filament colour, sent directly to Happy Hare via `MMU_GATE_MAP`
- **Spoolman integration** — If Spoolman is configured, the gate config modal shows a dropdown of your spool inventory; selecting a spool auto-fills colour, material, and links the spool ID to Happy Hare

### Spoolman Integration

Optional integration with [Spoolman](https://github.com/Donkie/Spoolman) filament tracking:

- **Spool management** — View, search, and manage spools via proxied API endpoints
- **Auto-deduction** — Filament usage is automatically deducted from matched spools when print jobs complete
- **Gate linking** — Assign Spoolman spools to Happy Hare MMU gates for per-gate filament tracking
- **Settings UI** — Configure the Spoolman URL and test connectivity from the dashboard Settings tab
- **Graceful fallback** — All Spoolman features are optional; the system works normally without it

## Quick Start

```bash
# Clone the repo
git clone https://github.com/barnard704344/the-print-farm.git
cd the-print-farm

# Run the setup script (Debian/Ubuntu/Raspberry Pi)
sudo bash setup.sh
```

The setup script will:
1. Install Python 3, pip, and Apache
2. Create a virtual environment and install dependencies
3. Create an admin user account
4. Detect whether the install location is accessible by `www-data` (falls back to `root` if under `/root/`)
5. Configure the systemd service and Apache reverse proxy
6. Start the farm manager

Access the dashboard at `http://<your-server-ip>:5000/`

## Manual Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp config/config.example.yaml config/config.yaml
# Edit config.yaml with your printer details
python -m src.main
```

## Adding Printers

### From the Dashboard
Click **Add Printer** in the Settings tab and choose BambuLab or Klipper.

### In config.yaml

```yaml
printers:
  # BambuLab printer
  - name: P1S-1
    type: bambulab
    host: 192.168.1.100
    access_code: '12345678'
    serial: 01P00C000000000

  # Klipper printer (via Moonraker)
  - name: Voron-01
    type: klipper
    host: 192.168.1.200
    moonraker_port: 7125
    camera_url: ''  # optional — auto-detected from Moonraker if blank
```

Klipper webcams are auto-detected from Moonraker's `/server/webcams/list` endpoint. Happy Hare MMU is auto-detected from Klipper's printer objects.

## OrcaSlicer Setup

### Virtual Printer (Recommended)

The print farm exposes OctoPrint-compatible endpoints so OrcaSlicer can send prints directly — no scripts or batch files needed.

**Step 1 — Add a physical printer in OrcaSlicer:**

1. Open **Printer Settings → Connection** (or *File → Preferences → Printer*)
2. Set **Host Type** to **OctoPrint**
3. Set the URL depending on how you want to use it:

| Mode | URL | Behaviour |
|---|---|---|
| Queue (any printer) | `http://<server-ip>:5000/the-print-farm` | Job enters the shared queue and is auto-assigned to the next idle printer |
| Specific printer | `http://<server-ip>:5000/the-print-farm/api/files/local/<PrinterName>` | Job is sent directly to that printer |

4. Paste your **API Key** (from `config.yaml` → `web.api_key`) into the API Key field
5. Click **Test** — you should see "Connected to The Print Farm"

**Step 2 — Print:**

Slice your model, then click **Print** (the upload icon next to "Slice"). OrcaSlicer will send the G-code directly to the farm.

> **Tip:** Create one OrcaSlicer printer profile per farm printer (e.g. "Farm — P1S-1", "Farm — Klipper 66") so you can target each from the printer dropdown.

### Upload Script (Legacy)

Copy the appropriate script from `static/client/` to your PC and configure it as a post-processing script in OrcaSlicer:

- **Windows (bat):** `C:\Windows\System32\cmd.exe /c C:\path\to\upload_to_farm.bat`
- **Windows (PowerShell):** `powershell -ExecutionPolicy Bypass -File "C:\path\to\upload_to_farm.ps1"`
- **macOS/Linux:** `python3 /path/to/upload_to_farm.py`

Edit the script to set your server URL and API key.

## Architecture

- **Backend:** Python 3 / Flask with REST API v1 Blueprint
- **Frontend:** Single-page dashboard (vanilla JS, Three.js for 3D viewer)
- **Database:** SQLite (job queue and file library)
- **Protocols:** MQTT + FTPS (BambuLab), HTTP/REST (Klipper/Moonraker)
- **Integrations:** Spoolman (filament tracking), Happy Hare (MMU control), Obico (AI failure detection)
- **Proxy:** Apache reverse proxy at `/the-print-farm`
- **Service:** systemd (`the-print-farm.service`)

## Configuration

### Spoolman

Add to `config/config.yaml` to enable filament tracking:

```yaml
spoolman:
  url: http://localhost:7912    # URL of your Spoolman instance
```

Or configure from the dashboard Settings tab. Leave unconfigured to disable Spoolman features.

### Happy Hare

No configuration needed — Happy Hare MMU is **auto-detected** on Klipper printers that have it installed. The MMU section appears on printer cards automatically with gate status, active tool, and filament state.

### REST API

All endpoints require an API key (set in `config.yaml` under `web.api_key`):

```bash
# List printers
curl -H "X-Api-Key: YOUR_KEY" http://localhost:5000/the-print-farm/api/v1/printers

# Get printer status
curl -H "X-Api-Key: YOUR_KEY" http://localhost:5000/the-print-farm/api/v1/printers/MyPrinter

# Queue a job
curl -X POST -H "X-Api-Key: YOUR_KEY" -F "file=@model.gcode" \
  http://localhost:5000/the-print-farm/api/v1/jobs

# View full API spec
curl -H "X-Api-Key: YOUR_KEY" http://localhost:5000/the-print-farm/api/v1/openapi.json
```

## Requirements

- Python 3.9+
- Apache 2 with `mod_proxy`
- Debian 11+ / Ubuntu 22.04+ / Raspberry Pi OS
- Spoolman (optional) — for filament tracking
- Happy Hare (optional) — for MMU control on Klipper printers

## License

Internal use.
