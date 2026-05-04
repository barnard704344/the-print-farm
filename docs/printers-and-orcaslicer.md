# Printers and OrcaSlicer

## Printer Discovery

Printers can be discovered automatically or added manually:

- **BambuLab** — Auto-detected via UDP broadcast on the local network. The printer serial and access code are still required in config.
- **Klipper** — Auto-detected by scanning for Moonraker on common ports. A printer at a known IP can also be added directly.

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

Klipper webcams are auto-detected from Moonraker's `/server/webcams/list` endpoint. Happy Hare MMU is auto-detected from Klipper printer objects.

## Klipper Adaptive Flow

If [Klipper Adaptive Flow](https://github.com/barnard704344/Klipper-Adaptive-Flow) is installed on a Klipper printer, the dashboard auto-detects it and shows a direct link to the Adaptive Flow analysis dashboard on the printer card.

## Obico Integration

If a local [Obico](https://www.obico.io/) server is running and the Obico plugin is installed on a Klipper printer, the dashboard automatically pulls failure detection data and remote monitoring info from it and displays it on the printer card. No additional configuration is required.

## OrcaSlicer Setup

The print farm exposes OctoPrint-compatible endpoints so OrcaSlicer can send prints directly — no scripts or batch files needed.

Each printer gets its own dedicated port for OrcaSlicer connections. The port is auto-assigned when a printer is added (starting at 5001) and displayed on the printer card in the dashboard.

### Per-Printer Setup (Recommended)

Each printer has its own OrcaSlicer port. Jobs uploaded this way are assigned to that specific printer and appear in the job queue. Send them to the machine manually from the dashboard.

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
- Apache proxies `/api` requests on that port to Flask's per-printer OctoPrint-compatible routes
- The `setup.sh` script auto-configures ports for all printers in `config.yaml`
- Adding, removing, or renaming printers from the dashboard automatically manages Apache vhosts
- Jobs uploaded via a per-printer port are assigned to that printer but **not auto-sent** — send them manually from the Job Queue tab when the printer is ready
