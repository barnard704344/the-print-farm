# API Reference

This summary covers `v1.0.17` and current `main`. The latest tagged release is
listed in the [README](../README.md); the native Bambu Obico bridge first shipped
in `v1.0.17`.

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

Notable dashboard routes:

- Printer control: temperatures, Bambu filament load/unload (refused while a
  print is running), plate check, and job thumbnails
- Discovery networks: view and save effective/configured VLAN ranges
  (`/api/discovery/config`)
- Integration settings: Obico (`/api/obico/...`) and email/Discord
  notifications (`/api/notifications/...`)
- Klipper macros: list, run and pin (`/api/printer/<name>/macros…`)
- File library: folder tree, bulk move/delete, and printing to a chosen printer
- Login mode: `/api/auth/config` for single-user mode

The Obico routes configure the farm-side bridge/client. They do not install an
Obico server or configure its SMTP service. A successful connection test checks
account/printer access, not AI analysis or email delivery. See the
[Obico API contract](../API.md#obico-config) and
[server prerequisites and live checks](bambu-obico.md).

OrcaSlicer uses a personal upload-only key copied from **OrcaSlicer Setup →
Connection Details** by logged-in approved students and staff. Uploads are
attributed to the key owner. The legacy shared `web.orca_api_key` is still
accepted without attribution. Neither key type can access `/api/v1` or browser
administration routes.

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

### Printer picture metadata

`GET /api/farm/status` includes `printer_model`, `printer_model_name`, and
`printer_image` in each printer state. `printer_image` is a bundled static path
(such as `printers/bambu-p1s.png`) or an empty string for unknown models. These
are display fields and contain no connection credentials.
