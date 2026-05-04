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

- Multi-printer support (BambuLab and Klipper)
- Real-time dashboard with status, progress, and camera feeds
- Job queue with upload, assign, and dispatch actions
- File library with interactive 3D toolpath viewer
- Authentication with local users and optional Active Directory
- OrcaSlicer integration using OctoPrint-compatible endpoints
- AMS and Happy Hare MMU support
- Spoolman integration for filament tracking
- Email and Discord notifications
- In-app software update checks and apply flow

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
