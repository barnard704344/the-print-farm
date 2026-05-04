# Printers and OrcaSlicer

## Adding Printers

### From Dashboard

Use Settings -> Add Printer and select BambuLab or Klipper.

### In config.yaml

```yaml
printers:
  - name: P1S-1
    type: bambulab
    host: 192.168.1.100
    access_code: '12345678'
    serial: 01P00C000000000
    orca_port: 5002

  - name: Voron-01
    type: klipper
    host: 192.168.1.200
    moonraker_port: 7125
    camera_url: ''
    orca_port: 5001
```

Klipper webcams are auto-detected from Moonraker when possible.

## OrcaSlicer Setup

The project exposes OctoPrint-compatible endpoints so OrcaSlicer can send jobs directly.

### Per-Printer Setup (Recommended)

1. Open Printer Settings -> Connection in OrcaSlicer
2. Set Host Type to Octo/Klipper
3. Set Hostname to <server-ip>:<port>
4. Set API Key from config.yaml web.api_key
5. Click Test

### General Queue

Use default HTTP host without a per-printer port:

- Hostname: <server-ip>
- API Key: same key

Jobs arrive unassigned and can be sent from the dashboard.

### How It Works

- Each printer gets a dedicated Orca-compatible port
- Apache routes per-printer /api requests to Flask routes
- Per-printer uploads are assigned to that printer but not auto-started
