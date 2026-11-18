# Architecture

The Print Farm deliberately uses a small, local-first architecture.

## Components

- **Backend** — Python 3, Flask, and Waitress, with the integration API grouped
  under `/api/v1`
- **Frontend** — One server-rendered dashboard using vanilla JavaScript and
  Three.js for the optional toolpath viewer
- **Storage** — SQLite for jobs, file-library metadata, MMU mappings, and AMS
  spool assignments; files and thumbnails remain on disk
- **Printer protocols** — MQTT, implicit FTPS, and the local camera protocol for
  BambuLab; Moonraker HTTP for Klipper
- **Integrations** — Optional Spoolman, Happy Hare, and Obico connections
- **Native deployment** — Waitress bound to loopback, Apache reverse proxy,
  per-printer OrcaSlicer ports, and `the-print-farm.service`
- **Docker deployment** — Waitress bound to the container interface and exposed
  through the published container port

## Request And Job Flow

1. A browser, OrcaSlicer or virtual-printer upload creates a queue record,
   stores the file, and adds it to the File Library.
2. Staff approve or assign the queued job, unless printer-pool auto-dispatch is
   enabled for a generic-port submission. Printing from the library creates a
   new job for the same stored file.
3. Readiness is determined from connection state, active jobs, printer status,
   staff-only restrictions, the build plate check, and the configured
   failed-printer cooldown.
4. The selected client uploads the file under a short readable name and
   starts it using the printer's local protocol.
5. Completion updates the queue and can trigger notifications and optional
   Spoolman usage deduction. A finished or failed print marks the build plate
   as needing clearing.

## Obico Data And Notification Flow

Obico is a separate optional service. The farm does not host its AI model or
send its AI alerts through the farm's notification manager.

| Stage | Bambu LAN bridge | Klipper integration |
|-------|------------------|---------------------|
| Printer telemetry and images | The farm sends status over a WebSocket and fresh camera snapshots over HTTP to Obico during a print | The printer's separately installed `moonraker-obico` agent sends them |
| AI analysis | Obico's detection backend analyses the images | Obico's detection backend analyses the images |
| Results in the farm | The farm polls the owning Obico account for results | The farm polls the owning Obico account for results |
| AI email alerts | Obico sends them using its own SMTP and recipient preferences | Obico sends them using its own SMTP and recipient preferences |

The farm's job submitted/completed/paused/failed notifications remain separate.
An Obico outage does not stop ordinary farm printing, but detection, results,
and Obico-triggered actions are unavailable while the required services are
down. An authenticated connection alone does not establish that camera uploads,
AI inference, or notification delivery work. See the
[Obico setup and verification guide](bambu-obico.md).

## Stored Files

Jobs and library entries refer to stored files by path. Reprints, per-printer
copies, library prints and repeat OrcaSlicer uploads of the same model share
one file. A file is deleted only when neither the library nor any queued,
uploading or printing job uses it, and the library refuses to delete a file
that such a job still needs.



## Security Boundaries

- Browser administration uses an authenticated staff session.
- The hidden administrator integration key is accepted by supported API routes
  but cannot run deployment updates. The separate Orca upload key is limited to
  OctoPrint-compatible connection and upload routes.
- Native privileged operations are restricted to a root-owned helper installed
  by `setup.sh`; the web service is not given general passwordless shell access.
- Native deployment reconciliation runs in a short-lived privileged systemd
  unit, and post-update restarts are queued without blocking the service that
  requested them.
- Apache preserves the public request host so browser origin validation remains
  correct behind the native reverse proxy.
- Runtime configuration is stored at `/etc/the-print-farm/config.yaml` with
  private permissions on native installations.
