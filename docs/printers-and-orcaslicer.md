# Printers and OrcaSlicer

## Adding Printers

### From Dashboard

Click Add Printer in the Settings tab and choose BambuLab or Klipper.

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
    camera_url: ''        # optional - auto-detected from Moonraker if blank
    orca_port: 5001       # auto-assigned OrcaSlicer port
```

Klipper webcams are auto-detected from Moonraker's `/server/webcams/list` endpoint. Happy Hare MMU is auto-detected from Klipper printer objects.

## OrcaSlicer Setup

The print farm exposes OctoPrint-compatible endpoints so OrcaSlicer can send prints directly with no scripts or batch files needed.

Each printer gets its own dedicated port for OrcaSlicer connections. The port is auto-assigned when a printer is added (starting at 5001) and displayed on the printer card in the dashboard.

### Per-Printer Setup (Recommended)

Each printer has its own OrcaSlicer port. Jobs uploaded this way are assigned to that specific printer and appear in the job queue. Send them to the machine manually from the dashboard.

1. Open Printer Settings -> Connection (or the physical printer settings)
2. Set Host Type to `Octo/Klipper`
3. Set Hostname, IP or URL to `<server-ip>:<port>` (example `192.168.1.180:5001`)
4. Paste your API key (from Settings tab or `config.yaml` -> `web.api_key`)
5. Click Test and confirm the connection succeeds

| Printer | Hostname | Port |
|---|---|---|
| voron | `192.168.1.180` | `5001` |
| P1S-1 | `192.168.1.180` | `5002` |

Tip: the OrcaSlicer port for each printer is shown on its dashboard card.

### General Queue

To send jobs to the shared queue (any available printer), use port 80 (default HTTP) with no path:

- Hostname: `192.168.1.180`
- API Key: same as above

Jobs enter the queue unassigned and can be sent to any printer from the dashboard.

### How It Works

- Each printer gets a dedicated Apache VirtualHost on its own port (5001, 5002, ...)
- Apache proxies `/api` requests on that port to Flask per-printer OctoPrint-compatible routes
- `setup.sh` auto-configures ports for all printers in `config.yaml`
- Adding, removing, or renaming printers from the dashboard automatically manages Apache vhosts
- Jobs uploaded via a per-printer port are assigned to that printer but are not auto-sent. Send them manually from the Job Queue tab when ready
