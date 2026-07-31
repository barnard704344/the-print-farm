# API Reference

Documentation status: `v1.0.13`.

## REST API v1

A full RESTful API at `/api/v1/` for external integrations:

- **40+ routes** with a consistent JSON envelope (`{ok, data, error, meta}`)
- **Administrator API-key authentication** via `X-Api-Key` header (stored
  privately as `web.admin_api_key`)
- Printers: list, status, commands (pause/resume/stop/temps/filament)
- Jobs: create/upload, list, status, assign, cancel, requeue, reprint, delete
- File library: list, search, organise, print, download, delete
- Cameras: list and authenticated JPEG snapshots
- Spoolman: inventory, printer/tool assignment, and AMS tray linking
- Happy Hare: macro discovery and validated execution
- OpenAPI 3.0 spec available at `/api/v1/openapi.json`

Browser administration also uses authenticated non-v1 routes documented in
[API.md](../API.md), including camera streams, settings, and native software
updates. An integration key cannot invoke deployment updates; those require a
staff browser session.

OrcaSlicer uses a different upload-only key stored as `web.orca_api_key`.
Logged-in students and staff can copy it from **OrcaSlicer Setup → Connection
Details**. It cannot access `/api/v1` or browser administration routes.

### Quick Examples

```bash
# List printers
curl -H "X-Api-Key: YOUR_ADMIN_KEY" http://localhost:5000/the-print-farm/api/v1/printers

# Get printer status
curl -H "X-Api-Key: YOUR_ADMIN_KEY" http://localhost:5000/the-print-farm/api/v1/printers/MyPrinter

# Queue a job
curl -X POST -H "X-Api-Key: YOUR_ADMIN_KEY" -F "file=@model.gcode" \
  http://localhost:5000/the-print-farm/api/v1/jobs

# View full API spec
curl -H "X-Api-Key: YOUR_ADMIN_KEY" http://localhost:5000/the-print-farm/api/v1/openapi.json
```

## Full Reference

Full endpoint documentation: [API.md](../API.md)

Includes:

- Auth/session and API key behavior
- Farm/printer control endpoints
- Job queue and reprint flows
- File library and toolpath endpoints
- Software update endpoints
- API v1 reference and OpenAPI details
