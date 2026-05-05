# Overview

The Print Farm is a web-based print farm manager for BambuLab and Klipper printers.

## BambuLab Support

Supported BambuLab families include:

- P1 series (for example P1S)
- X1 series (for example X1C)
- A1 series

Connection method:

- MQTT over LAN for printer state and control
- FTPS over LAN for file transfer

Prerequisites on each BambuLab printer:

- Enable LAN mode / LAN-only access on the printer
- Use the printer access code and serial in config or Add Printer flow

Cloud-only mode is not used for core printer communication.

## Klipper Support

Any printer running Klipper with the Moonraker API server is supported.

Connection method:

- Moonraker HTTP REST API (default port 7125)
- Optional Moonraker API key for installations with authentication enabled

Webcam:

- Camera URL is auto-detected from the Moonraker `/server/webcams/list` endpoint
- A specific URL can also be set manually in the printer config

Multi-material:

- Happy Hare MMU is auto-detected from Klipper printer objects when present

Peripheral discovery:

- `fan_generic` objects are enumerated for auxiliary fan control
- `led` and `neopixel` objects are discovered for lighting control

No Klipper plugin or extra component is required beyond a standard Moonraker installation.

## Core Features

- **Multi-printer support** — BambuLab (P1S, X1C, A1) via MQTT/FTPS (LAN mode) and Klipper via Moonraker HTTP API
- **Real-time dashboard** — Live status, temperatures, progress, and camera feeds
- **Job queue** — Upload G-code, queue jobs, auto-assign to idle printers
- **File library** — Persistent storage with folder organisation, search, and interactive 3D toolpath viewer (supports OrcaSlicer, PrusaSlicer, and Cura G-code), with staged loading feedback and feature-based colours including support and interface paths
- **Printer discovery** — Auto-detect BambuLab (UDP broadcast) and Klipper (Moonraker port scan)
- **Authentication** — Local users, Active Directory/LDAP, student/staff roles
- **OrcaSlicer integration** — Slice and print directly from OrcaSlicer via virtual printers (OctoPrint-compatible) — no batch files needed
- **AMS support** — Full filament tray management for BambuLab printers with AMS, including per-unit humidity and temperature monitoring
- **Printer pool** — Auto-dispatch generic OrcaSlicer jobs to the next idle printer in a configurable pool
- **Multi-printer dispatch** — Send a queued job to multiple printers at once; the job is cloned automatically
- **Reprint to selected printers** — Reprint actions can create a queued copy and optionally dispatch to one or more selected printers
- **In-app software updates** — Check upstream commits and apply updates from Settings (git pull + service restart)
- **Mobile responsive** — Dashboard adapts to phones and tablets with touch-friendly targets and stacked layouts
- **Camera streaming** — Live camera feeds from BambuLab printers and Klipper webcams (MJPEG/snapshot auto-detected via Moonraker)
- **Notifications** — Email (SMTP) and Discord webhook alerts for job submission, print completion, pause, and failure
- **Obico integration** — Pulls AI failure detection data from a local Obico server when present
- **Spoolman integration** - Add in your local instance of spoolman and track your filament usage. https://github.com/Donkie/Spoolman

## UI Screenshots

<img width="1890" height="875" alt="image" src="https://github.com/user-attachments/assets/1d42796c-3535-4304-b573-d93d24c8d310" />
<br>
<br>
<img width="1875" height="765" alt="image" src="https://github.com/user-attachments/assets/7db34b79-c0a4-433f-a5e2-18f206de9a5a" />
<br>
<br>
<img width="1900" height="662" alt="image" src="https://github.com/user-attachments/assets/dac962dd-214e-48d3-9c94-05028af6562c" />
<br>
<br>
<img width="1918" height="722" alt="image" src="https://github.com/user-attachments/assets/b8e62e2f-adb0-40cf-a9f6-e3c8eab723e4" />
<br>
<br>
<img width="1902" height="932" alt="image" src="https://github.com/user-attachments/assets/affc0baa-4061-4eae-9a77-56e4c277ae28" />
