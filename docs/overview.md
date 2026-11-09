# Overview

The Print Farm is a lightweight web-based 3D printer management system designed
primarily for schools using BambuLab and Klipper printers. It is equally usable
at home, in makerspaces, and in other small print farms. Operators remain
responsible for approving and dispatching prints.

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
- Enable Developer Mode on current firmware when available. P1/P1S firmware 01.08.02.00 and newer blocks third-party local start/control commands unless LAN-only mode and Developer Mode are both enabled.
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

Macros:

- User macros are listed from Moonraker with their descriptions and
  parameters. Staff pin favourites as buttons on the printer card. See
  [Printers and OrcaSlicer](printers-and-orcaslicer.md#klipper-macros).

No Klipper plugin or extra component is required beyond a standard Moonraker installation.

## Core Features

- **Multi-printer support** — BambuLab (P1S, X1C, A1) via MQTT/FTPS (LAN mode) and Klipper via Moonraker HTTP API
- **Real-time dashboard** — Printer cards with status chips (Wi-Fi, HMS
  alerts, chamber light, staff-only), a now-printing panel (job thumbnail,
  progress, time remaining, finish time, layers), click-to-set temperature
  tiles, per-unit AMS blocks with remaining-filament bars, and camera feeds.
  Printers that can take a job show **READY**
- **Readable job names** — Printers and the dashboard show short names such as
  "cade pip2" instead of stored file names with random prefixes and
  OrcaSlicer's `_PLA_9m8s` suffix
- **Job queue** — Upload G-code, queue jobs, auto-assign to idle printers
- **File library** — Persistent storage with a folder tree, search across all
  folders, multi-select with bulk print/move/delete, drag-and-drop moves, grid
  and list views with sorting, a print dialog that sends straight to an idle
  printer, and an interactive 3D toolpath viewer (supports OrcaSlicer,
  PrusaSlicer, and Cura G-code)
- **Printer discovery** — Auto-detect BambuLab (UDP broadcast) and Klipper (Moonraker port scan)
- **Authentication** — Local users, Active Directory/LDAP, student/staff
  roles, and an optional single-user mode with no login for one-person farms
- **OrcaSlicer integration** — Slice and print directly from OrcaSlicer via virtual printers (OctoPrint-compatible) — no batch files needed
- **AMS support** — Live RFID/tag data when available plus manual type, colour, temperature, and Spoolman assignment for generic or overridden filament
- **Filament load/unload** — Bambu Lab load and unload use the same commands
  as Bambu Studio and are refused while a print is running
- **Klipper macros** — Run any user macro from the dashboard, with a parameter
  form and pinned favourites on each card
- **Printer pool** — Auto-dispatch generic OrcaSlicer jobs to the next idle printer in a configurable pool
- **Multi-printer dispatch** — Send a queued job to multiple printers at once; the job is cloned automatically
- **Reprint to selected printers** — Reprint actions can create a queued copy and optionally dispatch to one or more selected printers
- **Failed-print readiness timeout** — A connected failed printer can return to Ready after a configurable delay; jobs are never resent automatically
- **In-app software updates** — Native installs can safely fast-forward,
  reconcile the deployment in a privileged transient unit, and queue a restart
  from Settings after printer and job checks pass
- **Mobile responsive** — Dashboard adapts to phones and tablets with touch-friendly targets and stacked layouts
- **Camera streaming** — Live BambuLab and Klipper feeds with per-printer 90-degree rotation saved in each browser
- **Notifications** — Email (SMTP) and Discord webhook alerts for job submission, print completion, pause, and failure
- **Obico integration** — Optionally pulls failure-detection and monitoring data
  from a configured self-hosted Obico printer, with connection diagnostics in
  Settings and Printer Details
- **Spoolman integration** — Optionally connect a local [Spoolman](https://github.com/Donkie/Spoolman) instance for assignment and usage tracking

## Build Plate Check and Camera Wall

After a print finishes or fails, the printer card shows an amber **FINISH**
and waits until someone presses **Plate cleared**, so a new job never starts
on top of an old one. Once cleared, the card shows a green **READY**.
The **Camera Wall** shows every printer camera in one grid for a classroom
screen. See [Configuration](configuration.md#build-plate-check).

## Student Print Access

The Print Farm includes a student access control system designed for school environments.

Staff and admin users can always print. Student accounts can sign in and view the dashboard, but they must be added to the **Student Print Access allowlist** before they can upload jobs, send jobs to printers, reprint jobs, or print from the file library.

What each account can do:

| | Students not on the allowlist | Allowlisted students | Staff |
|---|---|---|---|
| View printer cards and live cameras | Yes | Yes | Yes |
| Printer card controls (temperatures, light, fans, AMS trays, filament load/unload, Plate cleared) | Read-only | Yes | Yes |
| Run Klipper macros | No | Yes | Yes |
| Pause, resume, or stop a print | Own active job only | Any printer that is not staff-only | Yes |
| Print from the File Library, including straight to an idle printer | No | Yes | Yes |
| Upload, send, and reprint their own jobs | No | Yes | Yes |
| View the shared **Queued Jobs** list, and send or cancel any queued job | No | Yes | Yes |
| Staff-only printers, delete/bulk actions, requeue, e-stop, printer settings (Staff only, Build plate check, pinned macros), library folders and moves, Settings | No | No | Yes |

Job History continues to show students only their own jobs. Allowlisted
students cannot cancel another user's print once it has started.

Allowlist entries match the student's login username (for example
`firstname.lastname`) or display name, ignoring case. Email addresses are not
matched.

A separate **ban list** is also available. This is useful for keeping a record of students whose print access has been removed. If a student appears on both lists, the ban list takes priority.

Student access is managed from:

**Settings → Student Print Access**

Add one username or display name per line.

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
