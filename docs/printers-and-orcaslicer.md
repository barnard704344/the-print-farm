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

## Obico Integration

If a local [Obico](https://www.obico.io/) server is running and the Obico plugin is installed on a Klipper printer, the dashboard automatically pulls failure detection data and remote monitoring info from it and displays it on the printer card. No additional configuration is required.

## Happy Hare MMU Integration

Full control of [Happy Hare](https://github.com/moggieuk/Happy-Hare) (multi-material unit) on Klipper printers:

- **Auto-detected** — gate status, active tool, filament state, and encoder data appear on the printer card automatically when Happy Hare is present
- **Macro control modal** — Click the MMU section on any printer card to open a modal with all Happy Hare macros organised by category (Selection, Filament, Control, Calibration, Info, Recovery)
- **Parameter support** — Macros with parameters show an input dialog with defaults pre-filled
- **Gate configuration** — Click any loaded gate to set material type and filament colour, sent directly to Happy Hare via `MMU_GATE_MAP`
- **Spoolman integration** — If Spoolman is configured, the gate config modal shows a dropdown of your spool inventory; selecting a spool auto-fills colour, material, and links the spool ID to Happy Hare

No Happy Hare plugin or extra configuration is required — detection is fully automatic.

## BambuLab AMS Integration

Full management of BambuLab AMS (Automatic Material System) units:

- **Auto-detected** — AMS units, tray contents, filament colours, and active tray are shown on printer cards automatically
- **Per-unit monitoring** — Humidity percentage and temperature for each AMS unit
- **Tray management modal** — Click the AMS section to open a popup with Overview and Tray Management tabs
- **Filament configuration** — Set filament type, colour, and nozzle temperature per tray, applied directly to the printer
- **Spoolman integration** — If Spoolman is configured, select spools from your inventory to auto-fill tray settings

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
- Each vhost includes `Header always set Access-Control-Allow-Origin "*"` so the dashboard's port-reachability probe (a cross-origin fetch) succeeds and printers are never falsely shown as firewall-blocked
- The `setup.sh` script auto-configures ports for all printers in `config.yaml`
- Adding, removing, or renaming printers from the dashboard automatically manages Apache vhosts
- Jobs uploaded via a per-printer port are assigned to that printer but **not auto-sent** — send them manually from the Job Queue tab when the printer is ready

---

## Virtual Printer — LAN Mode (OrcaSlicer AMS Sync)

For full AMS slot sync in OrcaSlicer, each BambuLab printer is represented by a **virtual printer** — a lightweight network layer that appears on the LAN as if it were the real printer itself. This allows OrcaSlicer's native BambuLab LAN mode to connect and keep its filament colour/slot state in sync with the actual AMS.

### How It Works

At startup, the farm manager automatically:

1. Creates a macvlan sub-interface (`vbbl-<printer>`) on the server's physical NIC
2. Obtains a real DHCP lease for that interface — the virtual printer gets its own IP address on your LAN
3. Starts a BambuLab-compatible MQTT broker (TLS, port 8883) and implicit FTPS server (port 990) bound to that IP
4. Broadcasts SSDP discovery packets every 30 seconds **and** responds immediately to active SSDP M-SEARCH queries — OrcaSlicer's auto-connect flow works without waiting for the next broadcast
5. Relays live state (AMS contents, print progress, temperatures) from the real printer to OrcaSlicer

No manual IP configuration is needed. DHCP handles everything automatically on each service start.

### Connecting OrcaSlicer in LAN Mode

The virtual printer IPs, serials, and access codes are shown in the **OrcaSlicer Setup** tab of the dashboard (staff/admin login required).

1. In OrcaSlicer, open **Printer Settings → Connection**
2. Set **Host Type** to `Bambu Lab`
3. Choose **LAN Mode**
4. Enter the **IP address** shown in the dashboard for that printer
5. Enter the **Serial** and **Access Code** shown alongside it
6. Click **Test** — OrcaSlicer will connect and AMS slot colours will sync

> **Staff only:** Virtual printer credentials (IP, serial, access code) are only visible to staff and admin accounts. Student accounts do not see this section.

### Requirements

- `isc-dhcp-client` — installed automatically by `setup.sh`
- `openssl` — installed automatically by `setup.sh`; used to generate per-printer TLS certificates
- Systemd capabilities `CAP_NET_ADMIN`, `CAP_NET_RAW` — written to the service unit by `setup.sh`; required to create macvlan interfaces and acquire DHCP leases

### Opting Out a Printer

To disable the virtual printer for a specific printer, add `virtual_printer: false` to its config entry:

```yaml
printers:
  - name: P1S-1
    type: bambulab
    host: 192.168.1.100
    access_code: '12345678'
    serial: 01P00C000000000
    virtual_printer: false   # no virtual printer created for this one
```

### Troubleshooting

| Problem | Check |
|---|---|
| Virtual IP not appearing in dashboard | `journalctl -u the-print-farm -n 50` — look for `dhclient` or `macvlan` errors |
| DHCP lease not obtained | Ensure the server NIC name in `virtual_printer.py` matches your host (default: `eth0`) |
| OrcaSlicer shows "Failed to connect to printer" on first try | Use **Manual Setup** to enter IP and access code directly — this bypasses SSDP discovery. If it succeeds, the virtual printer is working correctly |
| OrcaSlicer can't connect at all | Confirm port 8883 is not blocked by a firewall on the server |
| Permission denied creating interface | Ensure `AmbientCapabilities=CAP_NET_ADMIN CAP_NET_RAW` is present in the service unit (`systemctl cat the-print-farm`) |
