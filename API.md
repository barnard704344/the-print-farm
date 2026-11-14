# The Print Farm — API Documentation

Complete reference for all REST API endpoints, reviewed for `v1.0.15`.

---

## Table of Contents

- [Authentication](#authentication)
- [Farm Status](#farm-status)
- [Printer Control](#printer-control)
- [Job Queue](#job-queue)
- [File Library](#file-library)
- [Camera](#camera)
- [Printer Discovery](#printer-discovery)
- [Spoolman Integration](#spoolman-integration)
- [Happy Hare / MMU](#happy-hare--mmu)
- [Klipper Macros](#klipper-macros)
- [Active Directory Config](#active-directory-config)
- [Student Print Access](#student-print-access)
- [Obico Config](#obico-config)
- [UI Preferences](#ui-preferences)
- [Software Update](#software-update)
- [OctoPrint-Compatible API](#octoprint-compatible-api)
- [API v1 Reference](#api-v1-reference)

---

## Authentication

The Print Farm separates browser sessions, administrator integrations, and
OrcaSlicer uploads.

### Administrator API Key

Pass the hidden administrator key in the `X-Api-Key` header. It is stored under
`web.admin_api_key` in the private server configuration and is never displayed
by the dashboard.

```
X-Api-Key: your-hidden-administrator-key
```

This key is intended only for trusted external integrations. It grants
privileged access where an endpoint accepts integration keys, but deployment
updates and other staff-session-only browser operations do not accept it.

### Orca Upload Key

OrcaSlicer uses the separate `web.orca_api_key`. Logged-in students and staff
can copy it from **OrcaSlicer Setup → Connection Details**. It authorises only
the OctoPrint-compatible connection and upload endpoints and cannot access
printer controls, settings, job administration, or `/api/v1`.

Upgrades preserve the former `web.api_key` value as `web.orca_api_key`, so
existing OrcaSlicer clients do not need to be changed. A new hidden
`web.admin_api_key` is generated during migration.

### Session (Browser)

Session-based authentication via login. Roles:

| Role | Access |
|------|--------|
| `staff` | Full admin access to all endpoints |
| `student` | Can view status. Uploading, sending, and reprinting require the student to be on the Student Print Access allowlist and not on the ban list. |

Allowlisted students who are not banned are **queue operators**. They can use
the routes marked *printer control* below on printers that are not staff-only.

Login sources (checked in order):
1. **Local users** — defined in `config.yaml` under `local_users`
2. **Active Directory** — LDAP lookup against configured AD server
3. **Legacy password** — single `admin_password` in config (deprecated)

#### Single-user mode

When `web.auth_enabled` is `false`, every request is treated as a staff
session named `admin`, so all browser routes work without signing in. Login
and logout return success without changing anything. Turning login back on
drops these automatic sessions. API-key authentication is unaffected. See
[Configuration](docs/configuration.md#login-and-single-user-mode).

### Auth Endpoints

#### `GET /api/auth/status`

Returns current session status. No auth required.

**Response:**
```json
{
  "auth_enabled": true,
  "admin": true,
  "authenticated": true,
  "role": "staff",
  "display_name": "Admin User",
  "username": "admin",
  "queue_operator": false,
  "ad_enabled": false,
  "has_local_users": true,
  "print_allowed": true,
  "print_denied_reason": ""
}
```

`auth_enabled` is `false` in single-user mode. `queue_operator` is `true` for
allowlisted students.

#### `POST /api/auth/login`

**Body:**
```json
{
  "username": "admin",
  "password": "secret"
}
```

**Response:**
```json
{
  "ok": true,
  "role": "staff",
  "display_name": "Admin User"
}
```

#### `POST /api/auth/logout`

Clears session. Returns `{"ok": true}`.

#### `GET /api/orca/api-key`

Returns the upload-only OrcaSlicer key to an authenticated student or staff
browser session. Administrator-key authentication and anonymous requests are
not accepted. The response uses `Cache-Control: no-store`.

**Response:**
```json
{
  "ok": true,
  "api_key": "configured-orca-upload-key",
  "configured": true
}
```

#### `POST /api/auth/sso`

SSO login for Apache GSSAPI integration. Requires AD and SSO enabled, and the
request must come from a trusted proxy. The identity is read from the
configured header (default `X-Remote-User`), which the managed Apache config
sets after Kerberos authentication; any client-supplied copy is stripped. The
request body is ignored.

#### `GET /api/auth/config`

Staff browser session only. Returns whether login is enabled and whether a
staff sign-in method exists:

```json
{ "auth_enabled": true, "staff_login_available": true }
```

#### `POST /api/auth/config`

Staff browser session only. Turns login on or off and saves
`web.auth_enabled`.

```json
{ "auth_enabled": false }
```

To turn login back on when no staff sign-in method exists (no staff local
user, legacy password or Active Directory), include a new staff account; the
request otherwise fails with `400` and `"needs_account": true`:

```json
{ "auth_enabled": true, "username": "admin", "password": "at-least-12-characters" }
```

Turning login on clears the current session.

---

## Farm Status

#### `GET /api/farm/status`

Full status of all printers and farm summary. Login required. Sensitive
connection fields are omitted from non-staff responses.

**Response:**
```json
{
  "summary": {
    "total": 2,
    "connected": 2,
    "printing": 1,
    "idle": 1,
    "errored": 0
  },
  "printers": {
    "voron": {
      "name": "voron",
      "type": "klipper",
      "host": "192.168.1.66",
      "connected": true,
      "status": "RUNNING",
      "display_status": "RUNNING",
      "staff_only": false,
      "plate_clear_required": true,
      "plate_needs_clear": false,
      "pinned_macros": ["NOZZLE_WIPE"],
      "mc_percent": 45,
      "mc_remaining_time": 23,
      "bed_temper": 60.0,
      "nozzle_temper": 215.0,
      "cooling_fan_speed": "128",
      "klipper_fans": [],
      "klipper_leds": [],
      "has_mmu": true,
      "mmu": {}
    }
  }
}
```

`status` is the printer's own state (`IDLE`, `RUNNING`, `PAUSED`,
`PAUSE_FILAMENT`, `FINISH`, `FAILED`, `UNKNOWN`). `display_status` is what the
dashboard shows:

- `READY` when the printer is idle, or finished with its build plate confirmed
  clear (or the plate check turned off). Bambu printers stay in `FINISH` until
  the next job starts.
- `READY` also when a failed printer has passed the failed-printer cooldown.
- Otherwise the same as `status`. A `FINISH` printer whose plate still needs
  clearing keeps `FINISH`.

Other printer fields include `plate_needs_clear`, `plate_finished_status`,
`plate_finished_at`, `ams_units` (per-unit `id`, `humidity`, `humidity_raw`
percentage on newer firmware, and `temp`), `wifi_signal`, `hms`,
`print_error`, `error_message` (Klipper: why a print was blocked or failed,
or why Klipper shut down; empty otherwise), `nozzle_diameter` and
`nozzle_type`.

#### `GET /api/farm/summary`

Farm summary counts only. Login required.

**Response:**
```json
{
  "total": 2,
  "connected": 2,
  "printing": 1,
  "idle": 1,
  "errored": 0
}
```

`idle` counts connected printers that can take a job now (idle or finished,
with the build plate clear).

---

## Printer Control

All control endpoints are `POST`. **Printer control** below means staff, the
administrator API key, or an approved student on a printer that is not
staff-only.

### Status

#### `GET /api/printer/<name>/status`

Returns full state for a single printer. Login required.

### Print Control

These require printer control, or a student whose own job is active on that
printer.

| Endpoint | Description |
|----------|-------------|
| `POST /api/printer/<name>/pause` | Pause current print |
| `POST /api/printer/<name>/resume` | Resume paused print |
| `POST /api/printer/<name>/stop` | Stop/cancel current print |

### Temperature

Printer control. A non-numeric `temp` returns `400`.

#### `POST /api/printer/<name>/bed_temp`

```json
{ "temp": 60 }
```
Range: 0–120°C.

#### `POST /api/printer/<name>/nozzle_temp`

```json
{ "temp": 215, "heater": "extruder1" }
```
Range: 0–300°C. `heater` is optional and used only by Klipper printers to pick
a toolhead (`extruder`, `extruder1`, …). Heating a docked toolchanger tool is
refused.

### Light Control

#### `POST /api/printer/<name>/light`

Printer control. Toggle chamber light (all printer types). Returns:
```json
{ "ok": true, "light": true }
```

#### `POST /api/printer/<name>/led`

Printer control. Toggle a specific LED or output pin. **Klipper only.**

**Body:**
```json
{
  "object": "neopixel chamber_light",
  "on": true
}
```
If `on` is omitted, toggles current state. The `object` must match a discovered LED/pin on the printer.

### Fan Control

#### `POST /api/printer/<name>/fan_speed`

Printer control. Set speed of a `fan_generic` object. **Klipper only.**

**Body:**
```json
{
  "object": "fan_generic exhaust_fan",
  "speed": 0.5
}
```
Speed range: 0.0 (off) to 1.0 (full).

Only `fan_generic` objects are controllable. `heater_fan` and `controller_fan` are read-only (managed by Klipper firmware).

### Emergency Stop

#### `POST /api/printer/<name>/emergency_stop`

Immediately halt printer. **Klipper only.** Admin required. Klipper needs a
`FIRMWARE_RESTART` afterwards; the dashboard asks for confirmation first.

### Filament

| Endpoint | Auth | Description |
|----------|------|-------------|
| `POST /api/printer/<name>/unload_filament` | printer control | Unload whatever is loaded (AMS tray or external spool) |
| `POST /api/printer/<name>/load_filament` | printer control | Load from the external spool (printers without an AMS) |
| `POST /api/printer/<name>/ams_load` | printer control | Load from an AMS tray. Body: `{"tray_id": 0}` |
| `POST /api/printer/<name>/tray_config` | printer control | Set AMS tray type/color (Bambu). Body: `{"tray_id": 0, "type": "PLA", "color": "FF0000"}` |

Load and unload are **Bambu Lab only** and send the same `ams_change_filament`
commands as Bambu Studio:

- **Loading:** an AMS load names the AMS unit and slot and lets the printer
  heat to the filament's own temperature.
- **Unloading:** runs at the current nozzle temperature, or 210 °C if the
  nozzle is cold.
- **`tray_id` values:** `0`–`15` regular AMS slots (unit × 4 + slot),
  `64`–`67` AMS Lite, `128`–`135` AMS HT, `254` external spool, `255` unload.

Responses are `{"ok": true, "message": "Loading filament from A1"}`. Errors:

| Status | Reason |
|--------|--------|
| `400` | Klipper printer, or invalid `tray_id` |
| `409` | Printer not connected, or a print is **running** (paused prints are allowed, e.g. after a filament runout) |
| `502` | The printer did not accept the command |

`tray_config` works for loaded generic/untagged trays and can override
tag-provided type or colour. Optional `nozzle_temp_min` and
`nozzle_temp_max` values are accepted. The printer can reject changes while
busy or while the target tray is active; this returns HTTP 409.

### Printer Settings

The dashboard shows these in the printer's **Details** panel under **Printer
settings**.

#### `POST /api/printer/<name>/staff_only`

Admin. Restrict printer to staff only.

```json
{ "staff_only": true }
```

#### `POST /api/printer/<name>/plate_clear_required`

Admin. Turn the build plate check on or off for a printer (saved as
`require_plate_clear`).

```json
{ "required": false }
```

#### `POST /api/printer/<name>/plate_cleared`

Printer control. Confirm the build plate is clear after a finished or failed
print, so the printer can accept the next job.

### Job Thumbnail

#### `GET /api/printer/<name>/thumbnail`

Login required. PNG thumbnail of the job the printer is currently running,
taken from the File Library. Returns `404` when the printer has no active job
or the job's file has no thumbnail, and `403` to students for staff-only
printers.

### Printer File Names

When a job is sent, the printer receives a short readable file name, e.g.
`cade_pip2.3mf`, built from the job's original name without the stored-file
prefix or OrcaSlicer's `_PLA_9m8s` filament/time suffix. Printers report that
name as `subtask_name`.

---

## Job Queue

### List Jobs

| Endpoint | Description |
|----------|-------------|
| `GET /api/jobs` | All jobs + stats |
| `GET /api/jobs/queued` | Queued jobs only |
| `GET /api/jobs/active` | Active/printing jobs |
| `GET /api/jobs/<job_id>` | Single job details |

Job objects include `print_time_seconds` when the slicer provided an estimated print time in the uploaded G-code/3MF metadata. The dashboard shows this as **Est. Time** in the Job Queue.

### Upload & Create Job

#### `POST /api/jobs/upload`

Requires staff access or a student session currently allowed to print.
Multipart form upload.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `file` | file | yes | `.gcode` or `.3mf` file |
| `thumbnail` | file | no | Thumbnail image |
| `copies` | int | no | Number of copies (default 1) |
| `priority` | int | no | Priority level |
| `notes` | string | no | Job notes |
| `printer` | string | no | Assign directly to printer |

**Response:**
```json
{ "ok": true, "job_id": 42 }
```

### Job Actions

| Endpoint | Auth | Description |
|----------|------|-------------|
| `POST /api/jobs/<id>/assign` | owner/admin | Assign to printer. Body: `{"printer": "voron"}` or `{"printers": ["voron", "P1S-1"]}` |
| `POST /api/jobs/<id>/cancel` | owner/admin | Cancel job (stops print if active) |
| `POST /api/jobs/<id>/requeue` | admin | Requeue failed/cancelled job |
| `POST /api/jobs/<id>/reprint` | owner/admin | Create queued reprint. Optional body: `{"printer": "voron"}` or `{"printers": ["voron", "P1S-1"]}` to immediately dispatch copies |
| `POST /api/jobs/<id>/delete` | admin | Delete job. Query: `?delete_library=true` to also remove library file |
| `DELETE /api/jobs/<id>` | admin | Same as above |
| `POST /api/jobs/bulk_delete` | admin | Delete several jobs. Body: `{"ids": [1, 2], "delete_library": false}` |

Deleting a job stops it first if it is printing. The job's file stays on disk
while the File Library or any queued, uploading or printing job still uses it;
reprints, per-printer copies and repeat OrcaSlicer uploads share one file.

With `delete_library`, both delete routes return `library_kept`, listing
library files that were not deleted because another active job uses them:

```json
{ "ok": true, "library_kept": [{ "name": "bracket.gcode", "error": "File is referenced by an active or queued job" }] }
```

### Filament Check

#### `GET /api/jobs/<id>/filaments`

Staff, queue operators, or the job's owner. Parse filament requirements from
G-code.

#### `POST /api/jobs/<id>/check_filament`

Staff, queue operators, or the job's owner. Check if a printer's AMS has the
needed filaments.

```json
{ "printer": "P1S-1" }
```

**Response:**
```json
{
  "ok": true,
  "match": true,
  "details": [
    { "slot": 0, "needed": "PLA", "loaded": "PLA", "ok": true }
  ]
}
```

---

## File Library

Requires login for all endpoints.

### Files

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `/api/library/files` | login | List files and sub-folders. Query: `?folder_id=1` (omit for the top level) |
| GET | `/api/library/files/search?q=benchy` | login | Search files in all folders |
| GET | `/api/library/files/<id>` | login | File metadata |
| GET | `/api/library/files/<id>/thumbnail` | login | Thumbnail image (PNG) |
| GET | `/api/library/files/<id>/toolpath` | login | Toolpath for the 3D viewer. `?format=bin` returns the compact binary form the viewer uses; without it, JSON |
| POST | `/api/library/files/<id>/move` | admin | Move to folder. Body: `{"folder_id": 1}` (null = top level) |
| POST | `/api/library/files/<id>/print` | print access | Create a job from the file (see below) |
| POST | `/api/library/files/bulk-move` | admin | Move several files. Body: `{"file_ids": [1, 2], "folder_id": 3}` |
| POST | `/api/library/files/bulk-delete` | admin | Delete several files. Body: `{"file_ids": [1, 2]}` |
| DELETE | `/api/library/files/<id>` | admin | Delete file (also `POST /api/library/files/<id>/delete`) |

`print` accepts an optional body:

```json
{ "printer_name": "P1S-01", "copies": 2 }
```

Without `printer_name` the job is added to the queue. With it, the printer is
checked like a normal upload (connected, idle, plate cleared, no active job,
not staff-only for students) and the job is sent straight away. `copies` is
1–100. Response: `{"ok": true, "job_id": 42, "printer_name": "P1S-01"}`.

Bulk routes accept 1–500 ids. `bulk-move` returns `{"ok": true, "moved": 2}`.
`bulk-delete` returns `{"ok": true, "deleted": 1, "skipped": [{"id": 2,
"error": "File is referenced by an active or queued job"}]}`; files used by a
queued, uploading or printing job are always kept.

### Folders

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `/api/library/folders/tree` | login | Every folder as a flat list with `parent_id` |
| POST | `/api/library/folders` | admin | Create folder. Body: `{"name": "Parts", "parent_id": null}` |
| POST | `/api/library/folders/<id>/rename` | admin | Rename. Body: `{"name": "New Name"}` |
| POST | `/api/library/folders/<id>/move` | admin | Move. Body: `{"parent_id": 2}` (null = top level). A folder can't move into itself or a sub-folder |
| DELETE | `/api/library/folders/<id>` | admin | Delete folder and its sub-folders; their files move to the top level |

Folder names are trimmed, repeated spaces collapsed, and limited to 100
characters. Names must be unique within a parent folder.

---

## Camera

All camera endpoints require an authenticated session.

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/camera/<name>/start` | Start (or keep alive) the shared camera |
| POST | `/api/camera/<name>/stop` | Stop the camera for everyone (staff only) |
| GET | `/api/camera/<name>/snapshot` | Latest JPEG frame |
| GET | `/api/camera/<name>/stream` | **MJPEG stream** (`multipart/x-mixed-replace`, ~2 FPS) |
| GET | `/api/camera/status` | Status of all cameras |
| GET | `/api/camera/status/details` | Per-camera connection details |

Students cannot use camera routes for staff-only printers. The Camera Wall
page is served at `/cameras`.

Camera rotation is a browser display preference, not a camera API operation.
The dashboard stores `0`, `90`, `180`, or `270` degrees per printer in browser
local storage.

---

## Printer Discovery

Admin only. Used by the Settings UI to find and add printers.

#### `POST /api/discover/scan`

Scan network for printers.

**Body:**
```json
{
  "timeout": 5,
  "port_scan": true,
  "subnet": "192.168.1.0/24"
}
```

**Response:**
```json
{
  "discovered": [
    { "name": "P1S", "host": "192.168.1.57", "type": "bambulab" }
  ],
  "port_scan": [
    { "host": "192.168.1.66", "port": 7125, "type": "klipper" }
  ],
  "subnets": ["192.168.1.0/24"]
}
```

#### `POST /api/discover/test`

Test connection to a printer before adding.

**Body (Bambu):**
```json
{
  "type": "bambulab",
  "host": "192.168.1.57",
  "access_code": "12345678",
  "serial": "01P00C..."
}
```

**Body (Klipper):**
```json
{
  "type": "klipper",
  "host": "192.168.1.66",
  "moonraker_port": 7125,
  "api_key": ""
}
```

#### `POST /api/discover/add`

Add a printer to the farm.

**Body (Klipper):**
```json
{
  "name": "voron",
  "type": "klipper",
  "host": "192.168.1.66",
  "moonraker_port": 7125,
  "api_key": "",
  "camera_url": "http://192.168.1.66/webcam/?action=stream"
}
```

**Body (Bambu):**
```json
{
  "name": "P1S-1",
  "type": "bambulab",
  "host": "192.168.1.57",
  "access_code": "12345678",
  "serial": "01P00C...",
  "mqtt_port": 8883,
  "ftp_port": 990,
  "camera_port": 6000
}
```

#### `POST /api/discover/remove`

Remove printer. Body: `{"name": "voron"}`

#### `POST /api/discover/rename`

Rename printer. Body: `{"old_name": "voron", "new_name": "voron-2.4"}`

---

## Spoolman Integration

### Config (Admin)

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/spoolman/config` | Get Spoolman URL |
| POST | `/api/spoolman/config` | Save URL. Body: `{"url": "http://192.168.1.180:7912"}` |
| POST | `/api/spoolman/test` | Test connection. Body: `{"url": "http://..."}` |

### Spools, Filaments, Vendors (API v1)

See [API v1 — Spoolman](#api-v1--spoolman) section below.

---

## Happy Hare / MMU

For Klipper printers with Happy Hare MMU. See [API v1 — Happy Hare](#api-v1--happy-hare) below.

---

## Klipper Macros

Klipper printers only. Macros starting with `_`, print-control and slicer
macros (`PAUSE`, `RESUME`, `CANCEL_PRINT`, `START_PRINT`, `PRINT_START`,
`END_PRINT`, `PRINT_END`) and Happy Hare `MMU_*` macros are not listed or run
here.

#### `GET /api/printer/<name>/macros`

Printer control. Lists user macros, cached for a minute; `?refresh=1` reloads
from Moonraker.

```json
{
  "ok": true,
  "macros": [
    { "name": "PREHEAT", "description": "Heat nozzle and bed",
      "params": [{ "name": "TEMP", "default": "220" }, { "name": "BED", "default": "" }] }
  ],
  "pinned": ["NOZZLE_WIPE"]
}
```

`description` comes from the macro's `description:` option. `params` are read
from the macro's G-code (`params.TEMP|default(220)` or `params["TEMP"]`).

#### `POST /api/printer/<name>/macros/run`

Printer control.

```json
{ "macro": "PREHEAT", "params": { "TEMP": "240" } }
```

- Only macros the printer defines can be run. Parameter names must be
  upper-case letters, digits and `_`. Values may contain letters, digits and
  `. _ - + # : /` only (up to 128 characters). Empty values are skipped.
- The macro runs in the background. The response is `{"ok": true, "message":
  "PREHEAT started"}` if it is still running after about three seconds, or
  `"PREHEAT finished"` if it completed.
- Errors Klipper reports straight away (e.g. `Must home axis first`) return
  `400` with that message.
- The route does not block macros during a print; the dashboard asks for
  confirmation first.

#### `POST /api/printer/<name>/macros/pinned`

Admin. Sets the macros shown as buttons on the printer card (up to 12), saved
as `pinned_macros` in the printer's config.

```json
{ "pinned": ["NOZZLE_WIPE", "PREHEAT"] }
```

---

## Active Directory Config

Admin only.

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/ad/config` | Get AD config (password masked) |
| POST | `/api/ad/config` | Save AD config |
| POST | `/api/ad/test` | Test AD connection |

**Save Body:**
```json
{
  "enabled": true,
  "server": "ldap://dc.example.com",
  "port": 636,
  "use_ssl": true,
  "base_dn": "DC=example,DC=com",
  "bind_user": "CN=svc,OU=Service,DC=example,DC=com",
  "bind_password": "secret",
  "student_ou": "OU=Students",
  "staff_ou": "OU=Staff"
}
```

---

## Student Print Access

Admin only. Staff can always print. Students must match the allowlist by username or display name, and the ban list wins if a name appears in both lists.

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/student-access/config` | Get student allowlist and ban list |
| POST | `/api/student-access/config` | Save student allowlist and ban list |

**Save Body:**
```json
{
  "allowlist": ["student.username", "Student Display Name"],
  "banlist": ["removed.student"]
}
```

---

## Obico Config

Admin only. Per-printer Obico failure detection config.

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/obico/config/<name>` | Get Obico config for printer |
| POST | `/api/obico/config/<name>` | Save Obico config |
| POST | `/api/obico/test` | Test Obico connection |

**Save Body:**
```json
{
  "enabled": true,
  "server": "http://192.168.1.105:3334",
  "printer_id": 3,
  "username": "user@example.com",
  "password": "secret"
}
```

The password returned by `GET` is masked. `POST /api/obico/test` accepts the
same fields plus `printer_name`; when the password is the mask, the saved secret
is reused only if the server and username are unchanged. The test returns a
non-success response when authentication fails, the printer ID does not exist,
or the printer endpoint is unavailable.

---

## UI Preferences

Controls display settings (timezone, locale) that affect how dates and times are
rendered across the dashboard. GET requires login; POST requires admin.

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/ui/config` | Get current UI preferences |
| POST | `/api/ui/config` | Save UI preferences |

### `GET /api/ui/config`

**Response:**
```json
{
  "timezone": "Australia/Sydney",
  "locale": "en-AU",
  "failed_printer_timeout_minutes": 5
}
```

`timezone` is an IANA timezone string (e.g. `Australia/Sydney`, `Europe/London`,
`America/New_York`). Empty string means auto-detect from the viewer's browser.
`failed_printer_timeout_minutes` controls how long a connected `FAILED` printer
remains blocking before it is presented as Ready. `0` disables automatic
clearing.

### `POST /api/ui/config`

**Body:**
```json
{
  "timezone": "Australia/Sydney",
  "locale": "en-AU",
  "failed_printer_timeout_minutes": 5
}
```

All fields are optional — omit one to leave it unchanged. The timeout accepts
values from 0 to 1440 minutes. Settings are persisted under `ui` and take
effect immediately. No job is ever resent automatically.

**Response:**
```json
{ "ok": true }
```

---

## Software Update

Authenticated staff session only. The OrcaSlicer/API integration key cannot
invoke deployment updates. These endpoints are used by the Settings tab update
controls.

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/update/check` | Fetch origin refs and report pending commits vs current HEAD |
| POST | `/api/update/apply` | Apply a validated fast-forward update and trigger delayed restart of `the-print-farm.service` |

### `GET /api/update/check`

**Response:**
```json
{
  "ok": true,
  "current_commit": "599aae2",
  "updates_available": 0,
  "commits": []
}
```

### `POST /api/update/apply`

**Success Response:**
```json
{
  "ok": true,
  "message": "Already up to date.",
  "restarting": true
}
```

**Notes:**

- Requires git on the host
- `setup.sh` installs a narrow root-owned helper for fetching, fast-forwarding,
  reconciling deployment files, installing dependencies, and restarting
- Deployment reconciliation runs in a transient privileged systemd unit so it
  does not inherit the web service's restricted capability set
- The service restart is queued with systemd without waiting on the process
  that requested its own restart
- Updates are rejected when the working tree contains local changes
- Legacy installs must rerun `sudo bash setup.sh --restart`; do not grant the
  service account direct passwordless access to Git, `systemctl`, or file tools
- A `.git/objects` permission error indicates an old/unreconciled native
  installation; rerun `sudo bash setup.sh --restart` rather than loosening
  repository permissions
- Docker containers must be updated by pulling and recreating the container;
  this endpoint does not replace a running container

---

## OctoPrint-Compatible API

These endpoints mimic OctoPrint's API so OrcaSlicer can connect to The Print Farm as a network printer.

Each per-printer port (5001, 5002, …) is served by a dedicated Apache
VirtualHost that proxies `/api` requests to Flask. The vhosts do not enable
wildcard CORS; the dashboard uses an opaque request when probing whether a port
is reachable. The general queue is served through narrowly matched
OctoPrint-compatible routes on the main port-80 VirtualHost, without claiming
unrelated `/api` routes.

All endpoints in this section require the upload-only Orca key in the
`X-Api-Key` header. The hidden administrator key is not accepted.

### Generic Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/version` | OctoPrint version info |
| GET | `/api/connection` | Connection status |
| GET | `/api/printer` | Printer state |

### Per-Printer Endpoints

When OrcaSlicer is configured to send to a specific printer:

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/version/<printer>` or `/<printer>/api/version` | Printer-specific version |
| GET | `/api/connection/<printer>` or `/<printer>/api/connection` | Printer-specific connection |
| GET | `/api/printer/<printer>` or `/<printer>/api/printer` | Printer-specific state |

### Upload

#### `POST /api/files/local`

Upload gcode from OrcaSlicer. Requires the Orca upload key in the
`X-Api-Key` header.

**Multipart form:**
- `file` — the gcode file
- `print` — `"true"` or `"false"` (auto-send to printer)

**Per-printer upload:**
- `POST /api/files/local/<printer>`
- `POST /<printer>/api/files/local`

Assigns the job directly to the target printer.

**Response (201):**
```json
{
  "files": {
    "local": {
      "name": "benchy.gcode",
      "display": "benchy.gcode",
      "path": "benchy.gcode",
      "origin": "local"
    }
  },
  "done": true
}
```

---

## API v1 Reference

All v1 endpoints are prefixed with `/api/v1/` and require either a valid `X-Api-Key` header or an authenticated session.

### Response Format

**Success:**
```json
{
  "ok": true,
  "data": { ... },
  "meta": { ... }
}
```

**Error:**
```json
{
  "ok": false,
  "error": {
    "message": "Description of the error",
    "code": "ERROR_CODE"
  }
}
```

### Server

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/v1/server` | Server info and capabilities |
| GET | `/api/v1/openapi.json` | OpenAPI 3.0 spec |

### Printers

#### `GET /api/v1/printers`

List all printers with full state.

#### `GET /api/v1/printers/<name>`

Get single printer state.

#### `POST /api/v1/printers/<name>/command`

Send a command to a printer.

**Body:**
```json
{ "command": "pause" }
```

Available commands:

| Command | Extra Params | Description |
|---------|-------------|-------------|
| `pause` | — | Pause print |
| `resume` | — | Resume print |
| `stop` | — | Stop print |
| `emergency_stop` | — | Emergency stop (Klipper) |
| `light` | `state` (bool, optional) | Toggle or set chamber light |
| `set_bed_temp` | `temperature` (int) | Set bed temperature |
| `set_nozzle_temp` | `temperature` (int) | Set nozzle temperature |
| `unload_filament` | — | Unload filament (Bambu Lab) |
| `load_filament` | — | Load from the external spool (Bambu Lab) |

`load_filament` and `unload_filament` return `400` on Klipper printers and
`409` (`PRINTER_BUSY`) while a print is running.

### API v1 — Happy Hare

#### `GET /api/v1/printers/<name>/happyhare/macros`

List available Happy Hare macros. Klipper + MMU only.

**Response:**
```json
{
  "ok": true,
  "data": {
    "printer": "voron",
    "mmu_state": { ... },
    "categories": [
      {
        "name": "Core",
        "macros": [
          {
            "name": "MMU_HOME",
            "category": "Core",
            "description": "Home the MMU selector",
            "params": []
          }
        ]
      }
    ],
    "total_macros": 15
  }
}
```

#### `POST /api/v1/printers/<name>/happyhare/run`

Execute a Happy Hare macro. Only `MMU_*` prefixed macros are allowed.

**Body:**
```json
{
  "macro": "MMU_SELECT",
  "params": { "GATE": "3" }
}
```

### API v1 — Jobs

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/v1/jobs` | List jobs. Query: `?status=queued&limit=50` |
| GET | `/api/v1/jobs/<id>` | Get job |
| POST | `/api/v1/jobs` | Upload file & create job (multipart) |
| DELETE | `/api/v1/jobs/<id>` | Delete job (admin) |
| POST | `/api/v1/jobs/<id>/cancel` | Cancel job |
| POST | `/api/v1/jobs/<id>/requeue` | Requeue job (admin) |
| POST | `/api/v1/jobs/<id>/reprint` | Create queued reprint. Optional body: `{"printer": "voron"}` or `{"printers": ["voron", "P1S-1"]}` to immediately dispatch copies |
| POST | `/api/v1/jobs/<id>/assign` | Assign to printer(s) (admin) |
| GET | `/api/v1/jobs/<id>/filaments` | Filament requirements |

Status filter values: `queued`, `printing`, `active`

### API v1 — File Library

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/v1/library/files` | List files. Query: `?folder_id=1` |
| GET | `/api/v1/library/files/search?q=benchy` | Search files |
| GET | `/api/v1/library/files/<id>` | File metadata |
| PATCH | `/api/v1/library/files/<id>` | Move file. Body: `{"folder_id": 1}` |
| DELETE | `/api/v1/library/files/<id>` | Delete file (admin). `409` (`FILE_IN_USE`) if a queued or active job uses it, `404` if not found |
| POST | `/api/v1/library/files/<id>/print` | Create job from file |
| GET | `/api/v1/library/folders` | List folders. Query: `?parent_id=1` |
| POST | `/api/v1/library/folders` | Create folder. Body: `{"name": "Parts"}` |
| PATCH | `/api/v1/library/folders/<id>` | Rename. Body: `{"name": "New"}` |
| DELETE | `/api/v1/library/folders/<id>` | Delete folder (admin) |

### API v1 — Cameras

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/v1/cameras` | All cameras status |
| GET | `/api/v1/cameras/<name>/snapshot` | JPEG snapshot |

### API v1 — Spoolman

Requires Spoolman configured. Returns 503 if unavailable.

#### Spools

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/v1/spoolman/status` | Connection status, version, health |
| GET | `/api/v1/spoolman/spools` | List spools |
| GET | `/api/v1/spoolman/spools/<id>` | Get spool |
| POST | `/api/v1/spoolman/spools` | Create spool |
| PATCH | `/api/v1/spoolman/spools/<id>` | Update spool |
| DELETE | `/api/v1/spoolman/spools/<id>` | Delete spool (admin) |
| POST | `/api/v1/spoolman/spools/<id>/use` | Consume filament. Body: `{"use_weight": 10.5}` (grams) or `{"use_length": 500}` (mm) |

**Spool list query params:** `filament.material`, `filament.vendor.name`, `filament.name`, `location`, `allow_archived`, `sort`, `limit`, `offset`

#### Filaments

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/v1/spoolman/filaments` | List filament types |
| GET | `/api/v1/spoolman/filaments/<id>` | Get filament type |

**Query params:** `name`, `material`, `vendor.name`, `vendor.id`, `sort`, `limit`, `offset`

#### Vendors

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/v1/spoolman/vendors` | List vendors |
| GET | `/api/v1/spoolman/vendors/<id>` | Get vendor |

**Query params:** `name`, `sort`, `limit`, `offset`

#### Printer-Spool Assignments

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/v1/spoolman/printers/<name>/spools` | Spools located at printer |
| PUT | `/api/v1/spoolman/printers/<name>/spools/<spool_id>` | Assign spool to printer |
| DELETE | `/api/v1/spoolman/printers/<name>/spools/<spool_id>` | Remove spool from printer |

#### Tool And AMS Assignments

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/v1/spoolman/printers/<name>/tools/<tool>/spools` | Spools assigned to a Klipper tool |
| PUT | `/api/v1/spoolman/printers/<name>/tools/<tool>/spools/<spool_id>` | Assign spool to a Klipper tool |
| DELETE | `/api/v1/spoolman/printers/<name>/tools/<tool>/spools/<spool_id>` | Remove tool assignment |
| GET | `/api/v1/printers/<name>/ams/trays` | AMS trays with persisted Spoolman assignments |
| PUT | `/api/v1/printers/<name>/ams/trays/<tray_id>/spool/<spool_id>` | Link a spool to an AMS tray |
| DELETE | `/api/v1/printers/<name>/ams/trays/<tray_id>/spool` | Remove AMS tray assignment |

---

## Notes

- All routes in `web.py` are registered at both `{prefix}/path` and `/path`. The prefix defaults to `/the-print-farm` and is configured at startup.
- MJPEG camera streams at `GET /api/camera/<name>/stream` are long-lived HTTP connections.
- No WebSocket endpoints are exposed — Bambu MQTT and Klipper Moonraker connections are handled internally.
- The dashboard polls `GET /api/farm/status` every 2 seconds for live updates.
