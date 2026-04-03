# The Print Farm

A web-based print farm manager for **BambuLab** and **Klipper** 3D printers. Monitor, control, and queue print jobs across multiple printers from a single dashboard.

## Features

- **Multi-printer support** — BambuLab (P1S, X1C, A1) via MQTT/FTPS and Klipper via Moonraker HTTP API
- **Real-time dashboard** — Live status, temperatures, progress, and camera feeds
- **Job queue** — Upload G-code, queue jobs, auto-assign to idle printers
- **File library** — Persistent storage with folder organisation, search, and 3D interactive viewer
- **Printer discovery** — Auto-detect BambuLab (UDP broadcast) and Klipper (Moonraker port scan)
- **Authentication** — Local users, Active Directory/LDAP, student/staff roles
- **OrcaSlicer integration** — Upload directly from slicer via post-processing script
- **AMS support** — Full filament tray management for BambuLab printers with AMS
- **Happy Hare MMU support** — Auto-detected gate status, active tool, filament state, and encoder data for Klipper printers with Happy Hare
- **Camera streaming** — Live camera feeds from BambuLab printers and Klipper webcams (MJPEG/snapshot auto-detected via Moonraker)

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

## OrcaSlicer Upload Script

Copy the appropriate script from `static/client/` to your PC and configure it as a post-processing script in OrcaSlicer:

- **Windows (bat):** `C:\Windows\System32\cmd.exe /c C:\path\to\upload_to_farm.bat`
- **Windows (PowerShell):** `powershell -ExecutionPolicy Bypass -File "C:\path\to\upload_to_farm.ps1"`
- **macOS/Linux:** `python3 /path/to/upload_to_farm.py`

Edit the script to set your server URL and API key.

## Architecture

- **Backend:** Python 3 / Flask
- **Frontend:** Single-page dashboard (vanilla JS, Three.js for 3D viewer)
- **Database:** SQLite (job queue and file library)
- **Protocols:** MQTT + FTPS (BambuLab), HTTP/REST (Klipper/Moonraker)
- **Proxy:** Apache reverse proxy at `/bambulab-farm`
- **Service:** systemd (`bambulab-farm.service`)

## Requirements

- Python 3.9+
- Apache 2 with `mod_proxy`
- Debian 11+ / Ubuntu 22.04+ / Raspberry Pi OS

## License

Internal use.
