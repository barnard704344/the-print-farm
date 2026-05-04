# API Reference

## REST API v1

A full RESTful API at `/api/v1/` for external integrations:

- **30+ endpoints** with consistent JSON envelope (`{ok, data, error, meta}`)
- **API key authentication** via `X-Api-Key` header (configured in `config.yaml` → `web.api_key`)
- Printers: list, status, commands (pause/resume/stop/temps/filament)
- Jobs: create, list, status, assign, cancel, delete
- File library: list, upload, download, delete
- Cameras: snapshot, streaming control
- Software updates: check and apply
- OpenAPI 3.0 spec available at `/api/v1/openapi.json`

### Quick Examples

```bash
# List printers
curl -H "X-Api-Key: YOUR_KEY" http://localhost:5000/the-print-farm/api/v1/printers

# Get printer status
curl -H "X-Api-Key: YOUR_KEY" http://localhost:5000/the-print-farm/api/v1/printers/MyPrinter

# Queue a job
curl -X POST -H "X-Api-Key: YOUR_KEY" -F "file=@model.gcode" \
  http://localhost:5000/the-print-farm/api/v1/jobs

# View full API spec
curl -H "X-Api-Key: YOUR_KEY" http://localhost:5000/the-print-farm/api/v1/openapi.json
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
