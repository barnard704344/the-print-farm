# The Print Farm — API Documentation

Complete reference for all REST API endpoints.

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
- [Active Directory Config](#active-directory-config)
- [Obico Config](#obico-config)
- [UI Preferences](#ui-preferences)
- [Software Update](#software-update)
- [OctoPrint-Compatible API](#octoprint-compatible-api)
- [API v1 Reference](#api-v1-reference)

---

## Authentication

The Print Farm supports two authentication methods:

### API Key

Pass your API key in the `X-Api-Key` header. Configure the key in `config/config.yaml` under `web.api_key`.

```
X-Api-Key: your-api-key-here
```

API key authentication grants full admin access and is intended for external integrations and OrcaSlicer.

### Session (Browser)

Session-based authentication via login. Roles:

| Role | Access |
|------|--------|
| `staff` | Full admin access to all endpoints |
| `student` | Can view status. Uploading, sending, and reprinting require the student to be on the Student Print Access allowlist and not on the ban list. |

Login sources (checked in order):
1. **Local users** — defined in `config.yaml` under `local_users`
2. **Active Directory** — LDAP lookup against configured AD server
3. **Legacy password** — single `admin_password` in config (deprecated)

### Auth Endpoints

#### `GET /api/auth/status`

Returns current session status. No auth required.

**Response:**
```json
{
  "admin": true,
  "authenticated": true,
  "role": "staff",
  "display_name": "Admin User",
  "username": "admin",
  "ad_enabled": false,
  "has_local_users": true,
  "print_allowed": true,
  "print_denied_reason": ""
}
```

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

#### `POST /api/auth/sso`

SSO login for Apache GSSAPI integration. Requires AD enabled.

**Body:**
```json
{
  "username": "user@DOMAIN"
}
```

---

## Farm Status

#### `GET /api/farm/status`

Full status of all printers and farm summary. No auth required.

**Response:**
```json
{
  "summary": {
    "total": 2,
    "connected": 2,
    "printing": 1,
    "idle": 1
  },
  "printers": {
    "voron": {
      "name": "voron",
      "type": "klipper",
      "host": "192.168.1.66",
      "connected": true,
      "status": "RUNNING",
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

#### `GET /api/farm/summary`

Farm summary counts only. No auth required.

**Response:**
```json
{
  "total": 2,
  "connected": 2,
  "printing": 1,
  "idle": 1
}
```

---

## Printer Control

All control endpoints are `POST` and require admin role unless noted.

### Status

#### `GET /api/printer/<name>/status`

Returns full state for a single printer. No auth required.

### Print Control

These require admin or job owner.

| Endpoint | Description |
|----------|-------------|
| `POST /api/printer/<name>/pause` | Pause current print |
| `POST /api/printer/<name>/resume` | Resume paused print |
| `POST /api/printer/<name>/stop` | Stop/cancel current print |

### Temperature

Admin only.

#### `POST /api/printer/<name>/bed_temp`

```json
{ "temp": 60 }
```
Range: 0–120°C.

#### `POST /api/printer/<name>/nozzle_temp`

```json
{ "temp": 215 }
```
Range: 0–300°C.

### Light Control

#### `POST /api/printer/<name>/light`

Toggle chamber light (all printer types). Returns:
```json
{ "ok": true, "light": true }
```

#### `POST /api/printer/<name>/led`

Toggle a specific LED or output pin. **Klipper only.**

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

Set speed of a `fan_generic` object. **Klipper only.**

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

Immediately halt printer. **Klipper only.** Admin required.

### Filament

| Endpoint | Auth | Description |
|----------|------|-------------|
| `POST /api/printer/<name>/unload_filament` | admin | Unload filament |
| `POST /api/printer/<name>/load_filament` | admin | Load filament |
| `POST /api/printer/<name>/ams_load` | admin | Load from AMS tray (Bambu). Body: `{"tray_id": 0}` |
| `POST /api/printer/<name>/tray_config` | admin | Set AMS tray type/color (Bambu). Body: `{"tray_id": 0, "type": "PLA", "color": "FF0000"}` |

### Printer Settings

#### `POST /api/printer/<name>/staff_only`

Restrict printer to staff only.

```json
{ "staff_only": true }
```

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

Requires login. Multipart form upload.

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

### Filament Check

#### `GET /api/jobs/<id>/filaments`

Parse filament requirements from gcode.

#### `POST /api/jobs/<id>/check_filament`

Check if a printer's AMS has the needed filaments.

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
| GET | `/api/library/files` | login | List files. Query: `?folder_id=1` |
| GET | `/api/library/files/search?q=benchy` | login | Search files |
| GET | `/api/library/files/<id>` | login | File metadata |
| GET | `/api/library/files/<id>/thumbnail` | login | Thumbnail image (PNG) |
| GET | `/api/library/files/<id>/toolpath` | login | Parsed toolpath payload for 3D viewer (positions, feature indices, feature names, bounds, count) |
| POST | `/api/library/files/<id>/move` | login | Move to folder. Body: `{"folder_id": 1}` (null = root) |
| POST | `/api/library/files/<id>/print` | login | Create job from file |
| DELETE | `/api/library/files/<id>` | admin | Delete file |

### Folders

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| POST | `/api/library/folders` | login | Create folder. Body: `{"name": "Parts", "parent_id": null}` |
| POST | `/api/library/folders/<id>/rename` | login | Rename. Body: `{"name": "New Name"}` |
| DELETE | `/api/library/folders/<id>` | admin | Delete folder |

---

## Camera

All camera endpoints are open (no auth required).

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/camera/<name>/start` | Start camera stream |
| POST | `/api/camera/<name>/stop` | Stop camera stream |
| GET | `/api/camera/<name>/snapshot` | Latest JPEG frame |
| GET | `/api/camera/<name>/stream` | **MJPEG stream** (`multipart/x-mixed-replace`, ~2 FPS) |
| GET | `/api/camera/status` | Status of all cameras |

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

## Build Plate Detection

Admin only. Per-printer empty build plate detection compares the current camera snapshot against empty-plate reference images inside a configurable ROI. BambuLab printers can use a two-stage check: compare the resting-bed view first, then move the bed to a raised inspection height and compare again only if the resting view looks clear. When enabled, jobs are blocked before upload/start if the plate appears occupied.

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/plate-detection/config/<name>` | Get detection settings and references |
| POST | `/api/plate-detection/config/<name>` | Save enabled flag, threshold, and ROI |
| POST | `/api/plate-detection/capture/<name>` | Capture current camera frame as an empty reference. Body may include `phase: "rest"` or `phase: "inspection"` |
| POST | `/api/plate-detection/prepare/<name>` | Bambu only. Optionally home Z, then move bed to raised inspection position |
| POST | `/api/plate-detection/jog/<name>` | Bambu only. Home Z or manually jog the bed up/down during calibration |
| GET | `/api/plate-detection/reference/<name>/<ref>` | View a reference image |
| DELETE | `/api/plate-detection/reference/<name>/<ref>` | Delete a reference image |
| POST | `/api/plate-detection/test/<name>` | Test the current camera frame against rest and raised references without moving the bed. Send `{"full_check": true}` to run the full pre-print motion check |
| POST | `/api/plate-detection/test-references/<name>` | Test the saved Rest/Raised reference images without using the live camera |

**Save Body:**
```json
{
  "enabled": true,
  "threshold": 12,
  "roi": { "x": 8, "y": 12, "w": 84, "h": 70 },
  "prepare_before_check": true,
  "inspection_z": 0,
  "settle_seconds": 2
}
```

For BambuLab printers, `prepare_before_check` moves the bed to `inspection_z`
before capturing the raised inspection frame so the plate is visible from the
built-in camera. The resting-bed reference is checked first; if that view looks
occupied, the bed is not moved. On P/X-series Bambu printers, `inspection_z: 0`
raises the bed to nozzle/camera inspection height. A1-style printers default to
the normal single-stage camera check.

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
  "locale": "en-AU"
}
```

`timezone` is an IANA timezone string (e.g. `Australia/Sydney`, `Europe/London`,
`America/New_York`). Empty string means auto-detect from the viewer's browser.

### `POST /api/ui/config`

**Body:**
```json
{
  "timezone": "Australia/Sydney",
  "locale": "en-AU"
}
```

Both fields are optional — omit one to leave it unchanged. Settings are persisted
to `config.yaml` under `ui.timezone` and `ui.locale` and take effect immediately
on next page load.

**Response:**
```json
{ "ok": true }
```

---

## Software Update

Admin only. Used by the Settings tab update controls.

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/update/check` | Fetch origin refs and report pending commits vs current HEAD |
| POST | `/api/update/apply` | Run git pull and trigger delayed restart of `the-print-farm.service` |

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
- New installs configure service-user restart permission automatically via setup.sh
- Legacy installs may require a manual sudoers rule; if missing, the endpoint can still pull code but restart will fail

---

## OctoPrint-Compatible API

These endpoints mimic OctoPrint's API so OrcaSlicer can connect to The Print Farm as a network printer.

Each per-printer port (5001, 5002, …) is served by a dedicated Apache VirtualHost that proxies `/api` requests to Flask. These vhosts include `Header always set Access-Control-Allow-Origin "*"` so the dashboard's cross-origin port-reachability probes succeed from any network. The general queue on port 80 is always reachable without this header.

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

Upload gcode from OrcaSlicer. Requires `X-Api-Key` header.

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
| `unload_filament` | — | Unload filament |
| `load_filament` | — | Load filament |

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
| DELETE | `/api/v1/library/files/<id>` | Delete file (admin) |
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

---

## Notes

- All routes in `web.py` are registered at both `{prefix}/path` and `/path`. The prefix defaults to `/the-print-farm` and is configured at startup.
- MJPEG camera streams at `GET /api/camera/<name>/stream` are long-lived HTTP connections.
- No WebSocket endpoints are exposed — Bambu MQTT and Klipper Moonraker connections are handled internally.
- The dashboard polls `GET /api/farm/status` every 2 seconds for live updates.
