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

- **30+ endpoints** with consistent JSON envelope (`{ok, data, error, meta}`)
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
6. Auto-assign OrcaSlicer ports to each printer and create Apache VirtualHosts
7. Start the farm manager

Access the dashboard at `http://<your-server-ip>/the-print-farm`

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
    orca_port: 5002       # auto-assigned OrcaSlicer port

  # Klipper printer (via Moonraker)
  - name: Voron-01
    type: klipper
    host: 192.168.1.200
    moonraker_port: 7125
    camera_url: ''        # optional — auto-detected from Moonraker if blank
    orca_port: 5001       # auto-assigned OrcaSlicer port
```

Klipper webcams are auto-detected from Moonraker's `/server/webcams/list` endpoint. Happy Hare MMU is auto-detected from Klipper's printer objects.

## OrcaSlicer Setup

The print farm exposes OctoPrint-compatible endpoints so OrcaSlicer can send prints directly — no scripts or batch files needed.

Each printer gets its own dedicated port for OrcaSlicer connections. The port is auto-assigned when a printer is added (starting at 5001) and displayed on the printer card in the dashboard.

### Per-Printer Setup (Recommended)

Each printer has its own OrcaSlicer port. Jobs uploaded this way are assigned to that specific printer and appear in the job queue — you send them to the machine manually from the dashboard.

1. Open **Printer Settings → Connection** (or the physical printer settings)
2. Set **Host Type** to `Octo/Klipper`
3. Set **Hostname, IP or URL** to `<server-ip>:<port>` (e.g. `192.168.1.180:5001`)
4. Paste your **API Key** (from the Settings tab or `config.yaml` → `web.api_key`)
5. Click **Test** — you should see the connection succeed

| Printer | Hostname | Port |
|---|---|---|
| voron | `192.168.1.180` | `5001` |
| P1S-1 | `192.168.1.180` | `5002` |

> **Tip:** The OrcaSlicer port for each printer is shown on its card in the dashboard.

### General Queue

To send jobs to the shared queue (any available printer), use port 80 (default HTTP) with no path:

- **Hostname:** `192.168.1.180`
- **API Key:** same as above

Jobs enter the queue unassigned and can be sent to any printer from the dashboard.

### How It Works

- Each printer gets a dedicated Apache VirtualHost on its own port (5001, 5002, …)
- Apache proxies `/api` requests on that port to Flask's per-printer OctoPrint-compat routes
- The `setup.sh` script auto-configures ports for all printers in `config.yaml`
- Adding/removing/renaming printers from the dashboard automatically manages Apache vhosts
- Jobs uploaded via a per-printer port are assigned to that printer but **not auto-sent** — send them manually from the Job Queue tab when the printer is ready

## Architecture

- **Backend:** Python 3 / Flask with REST API v1 Blueprint
- **Frontend:** Single-page dashboard (vanilla JS, Three.js for 3D viewer)
- **Database:** SQLite (job queue and file library)
- **Protocols:** MQTT + FTPS (BambuLab), HTTP/REST (Klipper/Moonraker)
- **Integrations:** Spoolman (filament tracking), Happy Hare (MMU control), Obico (AI failure detection)
- **Proxy:** Apache reverse proxy at `/the-print-farm` + per-printer OrcaSlicer ports (5001+)
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
