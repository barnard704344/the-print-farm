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

1. A browser or OrcaSlicer upload creates a queue record and stores the file.
2. Staff approve or assign the queued job, unless printer-pool auto-dispatch is
   enabled for a generic-port submission.
3. Readiness is determined from connection state, active jobs, printer status,
   staff-only restrictions, and the configured failed-printer cooldown.
4. The selected client uploads and starts the file using the printer's local
   protocol.
5. Completion updates the queue and can trigger notifications and optional
   Spoolman usage deduction.



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
