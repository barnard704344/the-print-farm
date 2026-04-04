# The Print Farm – REST API Documentation

## Table of Contents

1. [Overview](#overview)
2. [Authentication](#authentication)
3. [Response Formats](#response-formats)
4. [HTTP Status Codes](#http-status-codes)
5. [Legacy API](#legacy-api)
   - [Auth Endpoints](#auth-endpoints)
   - [Farm Status Endpoints](#farm-status-endpoints)
   - [Printer Control Endpoints](#printer-control-endpoints)
   - [Job Queue Endpoints](#job-queue-endpoints)
   - [File Library Endpoints](#file-library-endpoints)
   - [Discovery & Printer Management Endpoints](#discovery--printer-management-endpoints)
6. [REST API v1](#rest-api-v1)
   - [Server Info](#server-info-v1)
   - [Printers](#printers-v1)
   - [Jobs](#jobs-v1)
   - [File Library](#file-library-v1)
   - [Cameras](#cameras-v1)
   - [Spoolman Integration](#spoolman-integration-v1)
   - [OpenAPI Spec](#openapi-spec-v1)

---

## Overview

| Property | Value |
|---|---|
| **Framework** | Flask (Python) |
| **Default Port** | `5000` (configurable in `config/config.yaml`) |
| **Default Host** | `0.0.0.0` |
| **Base Path** | `/` or `/the-print-farm/` (set via `APP_PREFIX` env var for reverse proxy) |
| **Max Upload Size** | 500 MB |

---

## Authentication

### Methods

| Method | How to use | Scope |
|---|---|---|
| **Session cookie** | Log in via `POST /api/auth/login`; cookie is set automatically | Browser / legacy endpoints |
| **API key** | Pass `X-Api-Key: <key>` request header | All `/api/v1/` endpoints |
| **SSO (GSSAPI)** | Apache GSSAPI; trigger via `POST /api/auth/sso` | Enterprise deployments |

### Roles & Permission Decorators

| Decorator | Who can access |
|---|---|
| *(none)* | Anyone (public) |
| `login_required` | Any authenticated user **or** valid API key |
| `owner_or_admin_required` | The job owner **or** a staff/admin user |
| `printer_owner_or_admin_required` | Admin **or** a user whose job is currently printing on that printer |
| `admin_required` | Staff role or legacy admin password |

---

## Response Formats

### Legacy endpoints

Return a plain JSON object. Errors are signalled via HTTP status code.

```json
{ "ok": true, ... }
```

### V1 API envelope

All `/api/v1/` endpoints return a standardised envelope:

```json
{
  "ok": true,
  "data": { },
  "meta": { },
  "status": 200
}
```

**Error response:**

```json
{
  "ok": false,
  "error": {
    "message": "Human-readable description",
    "code": "ERROR_CODE"
  }
}
```

---

## HTTP Status Codes

| Code | Meaning |
|---|---|
| `200` | OK – successful GET / POST / PATCH |
| `201` | Created – new resource was created |
| `204` | No Content – successful DELETE |
| `400` | Bad Request – missing or invalid parameters |
| `401` | Unauthorized – not authenticated / invalid API key |
| `403` | Forbidden – insufficient permissions |
| `404` | Not Found – resource does not exist |
| `409` | Conflict – invalid state transition |
| `500` | Internal Server Error |
| `502` | Bad Gateway – printer unreachable |
| `503` | Service Unavailable – optional service (e.g. Spoolman) not configured |

---

## Legacy API

### Auth Endpoints

#### `GET /api/auth/status`

Returns the current authentication state for the calling client.

**Auth:** none

**Response `200`:**

```json
{
  "admin": true,
  "authenticated": true,
  "role": "staff",
  "display_name": "Ada Lovelace",
  "username": "alovelace",
  "ad_enabled": false,
  "has_local_users": true
}
```

---

#### `POST /api/auth/login`

Log in with a username and password.

**Auth:** none

**Request body (JSON):**

| Field | Type | Required | Description |
|---|---|---|---|
| `username` | string | ✔ | Local or AD username |
| `password` | string | ✔ | Password |

**Response `200`:**

```json
{ "ok": true, "role": "staff", "display_name": "Ada Lovelace" }
```

**Response `401`:** invalid credentials.

---

#### `POST /api/auth/logout`

Destroy the current session.

**Auth:** any authenticated user

**Response `200`:**

```json
{ "ok": true }
```

---

#### `POST /api/auth/sso`

Authenticate via Apache GSSAPI / SSO (enterprise deployments only).

**Auth:** Active Directory must be configured

**Request body (JSON):**

| Field | Type | Required | Description |
|---|---|---|---|
| `username` | string | ✔ | AD username supplied by Apache |

**Response `200`:**

```json
{ "ok": true, "role": "staff", "display_name": "Ada Lovelace" }
```

**Response `401`:** user not found / not authorised.

---

### Farm Status Endpoints

#### `GET /api/farm/status`

Return all printer states and a summary of the whole farm.

**Auth:** none

**Response `200`:**

```json
{
  "summary": {
    "total": 4,
    "connected": 4,
    "printing": 2,
    "idle": 2
  },
  "printers": {
    "Printer1": { },
    "Printer2": { }
  }
}
```

---

#### `GET /api/farm/summary`

Return only the farm-level summary counters.

**Auth:** none

**Response `200`:**

```json
{
  "total": 4,
  "connected": 4,
  "printing": 2,
  "idle": 2
}
```

---

### Printer Control Endpoints

URL parameter `<name>` is the printer's configured name.

#### `GET /api/printer/<name>/status`

Return the full state of a single printer.

**Auth:** none

**Response `200`:** printer state object (temperatures, progress, filament state, AMS trays, etc.)

---

#### `POST /api/printer/<name>/pause`

Pause the current print.

**Auth:** `printer_owner_or_admin_required`

**Response `200`:**

```json
{ "ok": true }
```

---

#### `POST /api/printer/<name>/resume`

Resume a paused print.

**Auth:** `printer_owner_or_admin_required`

**Response `200`:**

```json
{ "ok": true }
```

---

#### `POST /api/printer/<name>/stop`

Stop the current print.

**Auth:** `printer_owner_or_admin_required`

**Response `200`:**

```json
{ "ok": true }
```

---

#### `POST /api/printer/<name>/light`

Toggle the chamber light.

**Auth:** `admin_required`

**Response `200`:**

```json
{ "ok": true, "light": true }
```

---

#### `POST /api/printer/<name>/emergency_stop`

Trigger an emergency stop (Klipper/Moonraker printers only).

**Auth:** `admin_required`

**Response `200`:**

```json
{ "ok": true }
```

---

#### `POST /api/printer/<name>/bed_temp`

Set the bed target temperature.

**Auth:** `admin_required`

**Request body (JSON):**

| Field | Type | Required | Description |
|---|---|---|---|
| `temp` | integer | ✔ | Target temperature in °C |

**Response `200`:**

```json
{ "ok": true, "temp": 60 }
```

---

#### `POST /api/printer/<name>/nozzle_temp`

Set the nozzle target temperature.

**Auth:** `admin_required`

**Request body (JSON):**

| Field | Type | Required | Description |
|---|---|---|---|
| `temp` | integer | ✔ | Target temperature in °C |

**Response `200`:**

```json
{ "ok": true, "temp": 215 }
```

---

#### `POST /api/printer/<name>/unload_filament`

Unload filament from the printer.

**Auth:** `admin_required`

**Response `200`:**

```json
{ "ok": true }
```

---

#### `POST /api/printer/<name>/load_filament`

Load filament into the printer.

**Auth:** `admin_required`

**Response `200`:**

```json
{ "ok": true }
```

---

#### `POST /api/printer/<name>/ams_load`

Load a specific AMS tray into the printer.

**Auth:** `admin_required`

**Request body (JSON):**

| Field | Type | Required | Description |
|---|---|---|---|
| `tray_id` | integer | ✔ | Zero-based AMS tray index |

**Response `200`:**

```json
{ "ok": true }
```

---

#### `POST /api/printer/<name>/tray_config`

Configure an AMS tray's filament settings.

**Auth:** `admin_required`

**Request body (JSON):**

| Field | Type | Required | Description |
|---|---|---|---|
| `tray_id` | integer | ✔ | Zero-based AMS tray index |
| `type` | string | ✔ | Material type (e.g. `"PLA"`) |
| `color` | string | ✔ | Hex colour string (e.g. `"FF5733"`) |
| `nozzle_temp_min` | integer | ✔ | Minimum nozzle temperature |
| `nozzle_temp_max` | integer | ✔ | Maximum nozzle temperature |

**Response `200`:**

```json
{ "ok": true }
```

---

### Job Queue Endpoints

#### `GET /api/jobs`

List all jobs with statistics.

**Auth:** none

**Response `200`:**

```json
{
  "jobs": [ { } ],
  "stats": {
    "queued": 3,
    "printing": 1,
    "completed": 10,
    "failed": 0,
    "cancelled": 2
  }
}
```

---

#### `GET /api/jobs/queued`

Return only jobs with status `queued`.

**Auth:** none

**Response `200`:** array of job objects.

---

#### `GET /api/jobs/active`

Return only jobs currently printing.

**Auth:** none

**Response `200`:** array of job objects.

---

#### `GET /api/jobs/history`

Return completed, failed, and cancelled jobs.

**Auth:** none

**Response `200`:** array of job objects.

---

#### `GET /api/jobs/<job_id>`

Return full details for a single job.

**Auth:** none

**Response `200`:** job object.

---

#### `POST /api/jobs/upload`

Upload a `.gcode` or `.3mf` file and create a new print job.

**Auth:** `login_required`

**Request:** `multipart/form-data`

| Field | Type | Required | Description |
|---|---|---|---|
| `file` | file | ✔ | Gcode / 3MF file |
| `copies` | integer | – | Number of copies (default `1`) |
| `priority` | integer | – | Lower is higher priority (default `5`) |
| `notes` | string | – | Optional notes |
| `printer` | string | – | Pre-assign to a named printer |

**Response `200`:**

```json
{ "ok": true, "job_id": 42 }
```

---

#### `GET /api/jobs/<job_id>/filaments`

Parse gcode and return the filament slots required for the job.

**Auth:** none

**Response `200`:**

```json
{
  "filaments": [ { "slot": 0, "type": "PLA", "color": "FF5733" } ],
  "used_slots": [0],
  "used_filaments": [ { "type": "PLA", "color": "FF5733" } ]
}
```

---

#### `POST /api/jobs/<job_id>/check_filament`

Check whether a printer's AMS configuration satisfies the job's filament requirements.

**Auth:** none

**Request body (JSON):**

| Field | Type | Required | Description |
|---|---|---|---|
| `printer` | string | ✔ | Printer name to check against |

**Response `200`:**

```json
{
  "ok": true,
  "match": true,
  "details": [ { "slot": 0, "match": true, "message": "OK" } ],
  "message": "All filaments match"
}
```

---

#### `POST /api/jobs/<job_id>/assign`

Assign a queued job to one or more printers and start printing.

**Auth:** `owner_or_admin_required`

**Request body (JSON):** one of:

```json
{ "printer": "Printer1" }
```

```json
{ "printers": ["Printer1", "Printer2"] }
```

**Response `200`:**

```json
{
  "ok": true,
  "results": [ { "printer": "Printer1", "ok": true } ]
}
```

---

#### `POST /api/jobs/<job_id>/reprint`

Create a new copy of an existing job (re-uses the same file).

**Auth:** `owner_or_admin_required`

**Response `200`:**

```json
{ "ok": true, "job_id": 43 }
```

---

#### `POST /api/jobs/<job_id>/cancel`

Cancel a queued or active job.

**Auth:** `owner_or_admin_required`

**Response `200`:**

```json
{ "ok": true }
```

---

#### `POST /api/jobs/<job_id>/requeue`

Move a failed job back to the queue.

**Auth:** `admin_required`

**Response `200`:**

```json
{ "ok": true }
```

---

#### `DELETE /api/jobs/<job_id>`

Permanently delete a job record (and optionally its library entry).

**Auth:** `admin_required`

**Query parameters:**

| Parameter | Type | Description |
|---|---|---|
| `delete_library` | boolean | Also delete the file from the library (`true` / `false`) |

**Response `200`:**

```json
{ "ok": true }
```

---

#### `POST /api/jobs/<job_id>/delete`

Alias for `DELETE /api/jobs/<job_id>` for clients that do not support the DELETE method.

**Auth:** `admin_required`

**Response `200`:**

```json
{ "ok": true }
```

---

### File Library Endpoints

#### `GET /api/library/files`

List files in the library, optionally filtered by folder.

**Auth:** `login_required`

**Query parameters:**

| Parameter | Type | Description |
|---|---|---|
| `folder_id` | integer | Filter to files inside this folder |

**Response `200`:**

```json
{
  "files": [ { "id": 1, "original_name": "benchy.gcode", "file_size": 1234567 } ],
  "folders": [ { "id": 1, "name": "Test prints" } ]
}
```

---

#### `GET /api/library/files/search`

Search library files by name.

**Auth:** `login_required`

**Query parameters:**

| Parameter | Type | Description |
|---|---|---|
| `q` | string | Search term |

**Response `200`:**

```json
{ "files": [ { } ] }
```

---

#### `GET /api/library/files/<file_id>`

Return metadata for a single library file.

**Auth:** `login_required`

**Response `200`:** file object.

---

#### `GET /api/library/files/<file_id>/thumbnail`

Download the thumbnail image for a library file.

**Auth:** `login_required`

**Response `200`:** PNG image (`Content-Type: image/png`).

---

#### `GET /api/library/files/<file_id>/toolpath`

Return toolpath visualisation data for a file (for display in the UI).

**Auth:** `login_required`

**Response `200`:**

```json
{ "toolpath_data": { } }
```

---

#### `POST /api/library/files/<file_id>/move`

Move a file into a different folder (or to the root).

**Auth:** `login_required`

**Request body (JSON):**

| Field | Type | Required | Description |
|---|---|---|---|
| `folder_id` | integer \| null | ✔ | Destination folder ID, or `null` for root |

**Response `200`:**

```json
{ "ok": true }
```

---

#### `DELETE /api/library/files/<file_id>`

Delete a file from the library.

**Auth:** `admin_required`

**Response `200`:**

```json
{ "ok": true }
```

---

#### `POST /api/library/files/<file_id>/delete`

Alias for `DELETE /api/library/files/<file_id>`.

**Auth:** `admin_required`

**Response `200`:**

```json
{ "ok": true }
```

---

#### `POST /api/library/files/<file_id>/print`

Create a new job from a library file (without uploading again).

**Auth:** `login_required`

**Response `200`:**

```json
{ "ok": true, "job_id": 44 }
```

---

#### `POST /api/library/folders`

Create a new folder in the library.

**Auth:** `login_required`

**Request body (JSON):**

| Field | Type | Required | Description |
|---|---|---|---|
| `name` | string | ✔ | Folder name |
| `parent_id` | integer \| null | – | Parent folder ID, or `null` for root |

**Response `200`:** folder object with `id`, `name`, etc.

---

#### `POST /api/library/folders/<folder_id>/rename`

Rename an existing folder.

**Auth:** `login_required`

**Request body (JSON):**

| Field | Type | Required | Description |
|---|---|---|---|
| `name` | string | ✔ | New folder name |

**Response `200`:**

```json
{ "ok": true }
```

---

#### `DELETE /api/library/folders/<folder_id>`

Delete a folder and its contents.

**Auth:** `admin_required`

**Response `200`:**

```json
{ "ok": true }
```

---

#### `POST /api/library/folders/<folder_id>/delete`

Alias for `DELETE /api/library/folders/<folder_id>`.

**Auth:** `admin_required`

**Response `200`:**

```json
{ "ok": true }
```

---

### Discovery & Printer Management Endpoints

All endpoints in this section require `admin_required`.

#### `POST /api/discover/scan`

Scan the local network for printers using UDP broadcast and optional TCP port scanning.

**Request body (JSON):**

| Field | Type | Required | Description |
|---|---|---|---|
| `timeout` | float | – | Scan timeout in seconds (default `5.0`) |
| `port_scan` | boolean | – | Also perform TCP port scan |
| `subnet` | string | – | Subnet to scan, e.g. `"192.168.1.0/24"` |

**Response `200`:**

```json
{
  "discovered": [ { "host": "192.168.1.10", "name": "Bambu X1" } ],
  "port_scan": [ ],
  "subnets": ["192.168.1.0/24"]
}
```

---

#### `POST /api/discover/test`

Test the connection to a printer before adding it.

**Request body (JSON):**

| Field | Type | Required | Description |
|---|---|---|---|
| `type` | string | ✔ | `"bambulab"` or `"klipper"` |
| `host` | string | ✔ | IP address or hostname |
| `access_code` | string | – | BambuLab access code |
| `serial` | string | – | BambuLab serial number |
| `moonraker_port` | integer | – | Moonraker port (default `7125`) |
| `api_key` | string | – | Moonraker API key |

**Response `200`:**

```json
{ "ok": true, "message": "Connection successful" }
```

---

#### `POST /api/discover/add`

Add a printer to the configuration and connect to it.

**Request body (JSON):**

| Field | Type | Required | Description |
|---|---|---|---|
| `name` | string | ✔ | Display name |
| `host` | string | ✔ | IP address or hostname |
| `type` | string | ✔ | `"bambulab"` or `"klipper"` |
| `access_code` | string | – | BambuLab access code |
| `serial` | string | – | BambuLab serial number |
| `moonraker_port` | integer | – | Moonraker port |
| `api_key` | string | – | Moonraker API key |
| `camera_url` | string | – | MJPEG or RTSP camera URL |

**Response `200`:**

```json
{ "ok": true, "connected": true, "message": "Printer added" }
```

---

#### `POST /api/discover/remove`

Remove a printer from the configuration.

**Request body (JSON):**

| Field | Type | Required | Description |
|---|---|---|---|
| `name` | string | ✔ | Printer name to remove |

**Response `200`:**

```json
{ "ok": true, "message": "Printer removed" }
```

---

#### `POST /api/discover/rename`

Rename an existing printer.

**Request body (JSON):**

| Field | Type | Required | Description |
|---|---|---|---|
| `old_name` | string | ✔ | Current printer name |
| `new_name` | string | ✔ | New printer name |

**Response `200`:**

```json
{ "ok": true, "message": "Printer renamed" }
```

---

## REST API v1

All v1 endpoints are under the `/api/v1/` prefix and **require** a valid API key passed as the `X-Api-Key` request header.

### Server Info (v1)

#### `GET /api/v1/server`

Return basic server and API metadata.

**Response `200`:**

```json
{
  "ok": true,
  "data": {
    "name": "The Print Farm",
    "api_version": "1",
    "capabilities": ["printers", "jobs", "library", "cameras", "spoolman"]
  }
}
```

---

### Printers (v1)

#### `GET /api/v1/printers`

List all printers with their current state.

**Response `200`:**

```json
{
  "ok": true,
  "data": [ { "name": "Printer1", "status": "idle" } ],
  "meta": {
    "summary": { "total": 2, "printing": 1, "idle": 1 }
  }
}
```

---

#### `GET /api/v1/printers/<name>`

Return the full state of a single printer.

**Response `200`:**

```json
{
  "ok": true,
  "data": {
    "name": "Printer1",
    "status": "printing",
    "nozzle_temp": 215,
    "bed_temp": 60,
    "progress": 42
  }
}
```

---

#### `POST /api/v1/printers/<name>/command`

Send a control command to a printer.

**Request body (JSON):**

| Field | Type | Required | Description |
|---|---|---|---|
| `command` | string | ✔ | One of the supported commands (see below) |
| `state` | boolean | – | Required for `light` command |
| `temperature` | integer | – | Required for `set_bed_temp` / `set_nozzle_temp` |

**Supported commands:**

| Command | Extra params | Description |
|---|---|---|
| `pause` | – | Pause the current print |
| `resume` | – | Resume a paused print |
| `stop` | – | Stop the current print |
| `emergency_stop` | – | Emergency stop (Klipper only) |
| `light` | `state: bool` | Turn chamber light on/off |
| `set_bed_temp` | `temperature: int` | Set bed temperature (°C) |
| `set_nozzle_temp` | `temperature: int` | Set nozzle temperature (°C) |
| `unload_filament` | – | Unload filament |
| `load_filament` | – | Load filament |

**Response `200`:**

```json
{ "ok": true, "data": { "command": "pause" } }
```

---

### Happy Hare / MMU (Klipper, v1)

#### `GET /api/v1/printers/<name>/happyhare/macros`

List all available Happy Hare MMU macros grouped by category.

**Response `200`:**

```json
{
  "ok": true,
  "data": {
    "printer": "Printer1",
    "mmu_state": { },
    "categories": [
      {
        "name": "Homing",
        "macros": [ { "name": "MMU_HOME", "description": "Home the MMU" } ]
      }
    ]
  }
}
```

---

#### `POST /api/v1/printers/<name>/happyhare/run`

Execute a Happy Hare macro on the printer.

**Request body (JSON):**

| Field | Type | Required | Description |
|---|---|---|---|
| `macro` | string | ✔ | Macro name (must start with `MMU_`) |
| `params` | object | – | Key/value macro parameters |

**Example:**

```json
{ "macro": "MMU_SELECT", "params": { "GATE": "2" } }
```

**Response `200`:**

```json
{ "ok": true, "data": { "executed": "MMU_SELECT GATE=2", "printer": "Printer1" } }
```

---

### Jobs (v1)

#### `GET /api/v1/jobs`

List jobs, with optional filtering.

**Query parameters:**

| Parameter | Type | Description |
|---|---|---|
| `status` | string | `queued`, `printing`, `completed`, `failed`, or `cancelled` |
| `limit` | integer | Maximum results to return (default `100`) |

**Response `200`:**

```json
{
  "ok": true,
  "data": [ { } ],
  "meta": { "stats": { }, "count": 5 }
}
```

---

#### `GET /api/v1/jobs/<job_id>`

Return details for a single job.

**Response `200`:**

```json
{ "ok": true, "data": { "id": 1, "filename": "benchy.gcode", "status": "queued" } }
```

---

#### `POST /api/v1/jobs`

Upload a file and create a new job.

**Request:** `multipart/form-data`

| Field | Type | Required | Description |
|---|---|---|---|
| `file` | file | ✔ | Gcode / 3MF file |
| `copies` | integer | – | Number of copies |
| `priority` | integer | – | Job priority |
| `notes` | string | – | Optional notes |
| `printer` | string | – | Pre-assign to a printer |

**Response `201`:**

```json
{ "ok": true, "data": { "id": 42 }, "status": 201 }
```

---

#### `DELETE /api/v1/jobs/<job_id>`

Permanently delete a job.

**Response `204`:** no body.

---

#### `POST /api/v1/jobs/<job_id>/cancel`

Cancel a job.

**Response `200`:**

```json
{ "ok": true, "data": { "cancelled": true } }
```

---

#### `POST /api/v1/jobs/<job_id>/requeue`

Requeue a failed job.

**Response `200`:**

```json
{ "ok": true, "data": { "id": 42, "status": "queued" } }
```

---

#### `POST /api/v1/jobs/<job_id>/reprint`

Create a new job from an existing one's file.

**Response `201`:**

```json
{ "ok": true, "data": { "id": 43 }, "status": 201 }
```

---

#### `POST /api/v1/jobs/<job_id>/assign`

Assign a job to one or more printers.

**Request body (JSON):** one of:

```json
{ "printer": "Printer1" }
```

```json
{ "printers": ["Printer1", "Printer2"] }
```

**Response `200`:**

```json
{
  "ok": true,
  "data": [ { "printer": "Printer1", "job_id": 42, "ok": true } ]
}
```

---

#### `GET /api/v1/jobs/<job_id>/filaments`

Return filament requirements parsed from the job's gcode.

**Response `200`:**

```json
{
  "ok": true,
  "data": {
    "filaments": [ { "slot": 0, "type": "PLA", "color": "FF5733" } ],
    "used_slots": [0],
    "used_filaments": [ { "type": "PLA", "color": "FF5733" } ]
  }
}
```

---

### File Library (v1)

#### `GET /api/v1/library/files`

List library files.

**Query parameters:**

| Parameter | Type | Description |
|---|---|---|
| `folder_id` | integer | Filter by folder |

**Response `200`:**

```json
{ "ok": true, "data": { "files": [ ], "folders": [ ] } }
```

---

#### `GET /api/v1/library/files/search`

Search for library files by name.

**Query parameters:**

| Parameter | Type | Description |
|---|---|---|
| `q` | string | Search term |

**Response `200`:**

```json
{ "ok": true, "data": { "files": [ ] } }
```

---

#### `GET /api/v1/library/files/<file_id>`

Return metadata for a library file.

**Response `200`:**

```json
{ "ok": true, "data": { "id": 1, "original_name": "benchy.gcode" } }
```

---

#### `PATCH /api/v1/library/files/<file_id>`

Move a file to a different folder.

**Request body (JSON):**

| Field | Type | Required | Description |
|---|---|---|---|
| `folder_id` | integer \| null | ✔ | Destination folder, or `null` for root |

**Response `200`:**

```json
{ "ok": true, "data": { } }
```

---

#### `DELETE /api/v1/library/files/<file_id>`

Delete a library file.

**Response `200`:**

```json
{ "ok": true, "data": { } }
```

---

#### `POST /api/v1/library/files/<file_id>/print`

Create a new print job from a library file.

**Response `201`:**

```json
{ "ok": true, "data": { "id": 44 }, "status": 201 }
```

---

#### `GET /api/v1/library/folders`

List library folders.

**Query parameters:**

| Parameter | Type | Description |
|---|---|---|
| `parent_id` | integer | List only children of this folder |

**Response `200`:**

```json
{ "ok": true, "data": { "folders": [ ] } }
```

---

#### `POST /api/v1/library/folders`

Create a folder.

**Request body (JSON):**

| Field | Type | Required | Description |
|---|---|---|---|
| `name` | string | ✔ | Folder name |
| `parent_id` | integer \| null | – | Parent folder, or `null` for root |

**Response `201`:**

```json
{ "ok": true, "data": { "id": 5, "name": "PLA prints" }, "status": 201 }
```

---

#### `PATCH /api/v1/library/folders/<folder_id>`

Rename a folder.

**Request body (JSON):**

| Field | Type | Required | Description |
|---|---|---|---|
| `name` | string | ✔ | New folder name |

**Response `200`:**

```json
{ "ok": true, "data": { } }
```

---

#### `DELETE /api/v1/library/folders/<folder_id>`

Delete a folder and its contents.

**Response `200`:**

```json
{ "ok": true, "data": { } }
```

---

### Cameras (v1)

#### `GET /api/v1/cameras`

Return streaming status for all cameras.

**Response `200`:**

```json
{
  "ok": true,
  "data": {
    "cameras": {
      "Printer1": { "streaming": true }
    }
  }
}
```

---

#### `GET /api/v1/cameras/<name>/snapshot`

Download the latest snapshot from a camera.

**Response `200`:** JPEG image (`Content-Type: image/jpeg`).

---

### Spoolman Integration (v1)

Requires Spoolman to be configured. Returns `503` if not available.

#### `GET /api/v1/spoolman/status`

Check Spoolman connectivity.

**Response `200`:**

```json
{
  "ok": true,
  "data": {
    "connected": true,
    "info": { "version": "0.17.0" },
    "health": "healthy"
  }
}
```

---

#### Spools

##### `GET /api/v1/spoolman/spools`

List spools with optional filters.

**Query parameters:**

| Parameter | Type | Description |
|---|---|---|
| `filament.material` | string | Filter by material, e.g. `PLA` |
| `location` | string | Filter by location / printer name |
| `limit` | integer | Maximum results |

**Response `200`:**

```json
{ "ok": true, "data": [ { } ], "meta": { "count": 3 } }
```

---

##### `GET /api/v1/spoolman/spools/<spool_id>`

Return a single spool.

**Response `200`:**

```json
{ "ok": true, "data": { "id": 1, "location": "Printer1" } }
```

---

##### `POST /api/v1/spoolman/spools`

Create a new spool.

**Request body (JSON):** Spoolman spool schema fields.

**Response `201`:**

```json
{ "ok": true, "data": { "id": 5 }, "status": 201 }
```

---

##### `PATCH /api/v1/spoolman/spools/<spool_id>`

Update spool fields.

**Request body (JSON):** fields to update.

**Response `200`:**

```json
{ "ok": true, "data": { } }
```

---

##### `DELETE /api/v1/spoolman/spools/<spool_id>`

Delete a spool.

**Response `204`:** no body.

---

##### `POST /api/v1/spoolman/spools/<spool_id>/use`

Record filament consumption on a spool.

**Request body (JSON):** one of:

| Field | Type | Description |
|---|---|---|
| `use_weight` | float | Grams consumed |
| `use_length` | float | Millimetres consumed |

**Response `200`:**

```json
{ "ok": true, "data": { "id": 1, "remaining_weight": 820.5 } }
```

---

#### Filaments

##### `GET /api/v1/spoolman/filaments`

List filament types.

**Query parameters:**

| Parameter | Type | Description |
|---|---|---|
| `material` | string | e.g. `PLA` |
| `vendor.name` | string | Filter by vendor name |

**Response `200`:**

```json
{ "ok": true, "data": [ { } ], "meta": { "count": 12 } }
```

---

##### `GET /api/v1/spoolman/filaments/<filament_id>`

Return a single filament type.

**Response `200`:**

```json
{ "ok": true, "data": { "id": 1, "material": "PLA" } }
```

---

#### Vendors

##### `GET /api/v1/spoolman/vendors`

List vendors.

**Query parameters:**

| Parameter | Type | Description |
|---|---|---|
| `name` | string | Filter by vendor name |

**Response `200`:**

```json
{ "ok": true, "data": [ { } ], "meta": { "count": 4 } }
```

---

##### `GET /api/v1/spoolman/vendors/<vendor_id>`

Return a single vendor.

**Response `200`:**

```json
{ "ok": true, "data": { "id": 1, "name": "Prusament" } }
```

---

#### Printer–Spool Mapping

##### `GET /api/v1/spoolman/printers/<name>/spools`

Return spools currently assigned to a printer.

**Response `200`:**

```json
{ "ok": true, "data": [ { "id": 1, "location": "Printer1" } ] }
```

---

##### `PUT /api/v1/spoolman/printers/<name>/spools/<spool_id>`

Assign a spool to a printer (set its location).

**Response `200`:**

```json
{ "ok": true, "data": { "id": 1, "location": "Printer1" } }
```

---

##### `DELETE /api/v1/spoolman/printers/<name>/spools/<spool_id>`

Remove a spool from a printer (clear its location).

**Response `200`:**

```json
{ "ok": true, "data": { "id": 1, "location": null } }
```

---

### OpenAPI Spec (v1)

#### `GET /api/v1/openapi.json`

Return the OpenAPI 3.0 specification for the v1 API. Useful for code generation and API exploration tools (e.g. Swagger UI, Postman).

**Auth:** `X-Api-Key` header required

**Response `200`:** OpenAPI 3.0 JSON document.
