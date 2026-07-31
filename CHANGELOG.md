# Changelog

This file records notable user-facing, security, deployment, and compatibility
changes to The Print Farm.

### Documentation

- Audited the complete documentation set against `v1.0.11`, including
  authentication boundaries, native and Docker updates, failed-printer
  readiness, camera rotation, optional Spoolman behaviour, and AMS controls for
  tagged and generic filament.
- Added the missing `v1.0.10` history and current release/image references.

## v1.0.11 - 2026-07-31 - Lightweight printer readiness

Commit: `6d2a829`

### Changed

- Removed build-plate camera detection, calibration controls, Bambu bed-motion
  helpers, and pre-print blocking. Printer readiness now relies on normal printer
  state and the configurable failed-print timeout.
- Setup upgrades now remove retired plate-detection configuration and notification
  settings from existing installations.

### Fixed

- Corrected printer-name escaping in generated dashboard actions so Details,
  AMS popups, tray configuration, and manual AMS colour selection are clickable.

## v1.0.10 - 2026-07-31 - Readiness and integration fixes

Commit: `f8eb611`

### Changed

- Made Spoolman genuinely optional: saving an empty address disables its
  printer-card assignment controls without affecting normal filament, AMS, or
  MMU operation.
- Read Bambu AMS tag data when supplied by the printer while allowing staff to
  override tray type, colour, and temperatures for both RFID-tagged and generic
  filament.
- Added the configurable failed-printer cooldown. A connected failed printer
  returns to Ready after the delay but jobs are never resent automatically.
- Excluded failed printers from manual job targets until their cooldown expires
  or the printer reports a non-failed state.

### Fixed

- Scoped Apache API proxy rules to The Print Farm and its dedicated OrcaSlicer
  ports so unrelated applications on the same web server keep ownership of
  their own `/api` routes.
- Reconciled the native setup/update path with the privileged helper and
  fast-forward-only Git workflow.

## v1.0.9 - 2026-07-30 - Security and deployment hardening

Commit: `c47ca23`

### Security

- Made API authentication fail closed when no integration key or authenticated
  session is present.
- Restricted deployment updates and printer administration to authenticated
  staff sessions. Integration API keys can no longer invoke privileged updates.
- Added same-origin checks for state-changing browser requests, constant-time
  API-key comparisons, secure response headers, login throttling, and private
  persistent Flask session keys.
- Migrated plaintext local and legacy administrator passwords to Werkzeug
  password hashes.
- Redacted credentials, internal file paths, and other privileged fields from
  student and unauthenticated responses.
- Added live TLS certificate validation and persistent certificate pinning for
  Bambu MQTT, FTPS, and camera connections. LDAPS now validates certificates by
  default and supports a configured CA bundle.
- Added strict validation and resource limits for uploads, archives, images,
  thumbnails, printer names, ports, macros, and privileged helper arguments.

### Deployment

- Reworked `setup.sh` as an idempotent installer and upgrade reconciler with a
  process lock, strict shell handling, private file modes, atomic writes, and
  recoverable configuration migration.
- Moved runtime configuration to `/etc/the-print-farm/config.yaml`; the
  repository path remains a symlink for compatibility. Pre-migration backups
  and runtime secrets are ignored by Git.
- Bound the Python backend to `127.0.0.1` by default and exposed the dashboard
  through Apache at `/the-print-farm/`.
- Kept native installs loopback-only while allowing the official Docker image
  to opt into container-reachable binding through explicit environment
  variables.
- Added a dedicated, root-owned privileged helper for narrowly scoped Apache,
  OrcaSlicer, update, and service operations. Generic passwordless shell
  commands are no longer granted.
- Added a hardened systemd unit and dedicated `print-farm` service account.
  Installations below an inaccessible home directory, such as `/root`, fall
  back to root with an explicit warning.
- Setup now uses Apache configuration tests and graceful reloads instead of
  restarting Apache.
- Restart and update operations fail closed unless every configured printer is
  connected in `IDLE`, `FINISH`, or `FAILED` state and the queue has no assigned,
  uploading, printing, or paused jobs. `--force-restart` remains an explicit
  operator override.
- Added `--start`, `--restart`, `--no-restart`, `--force-restart`, and
  `--skip-packages` setup modes, backend health checks, and no-restart
  dependency deferral.
- Dashboard updates now require a clean worktree and use fetch plus fast-forward
  merge only.

### Reliability And Performance

- Replaced the Flask development server with Waitress and kept it behind the
  Apache loopback proxy.
- Added bounded upload, archive, image, camera, worker, and request behavior to
  reduce memory, disk, and thread exhaustion risks.
- Improved asynchronous printer-pool work and stopped masking failed printer
  states as idle.
- Hardened camera frame reads against fragmented network packets and stale
  frames.
- Repaired camera click, modal, start/stop, and rotation controls after
  authenticated dashboard rendering. Rotation is saved per printer.
- Removed wildcard CORS from OrcaSlicer proxy sites and made generated Apache
  site identifiers collision resistant.

### Documentation And Validation

- Updated setup, configuration, API, and OrcaSlicer documentation for the new
  proxy, authentication, TLS, and runtime configuration behavior.
- Added regression coverage for authentication boundaries, student data
  ownership, update safety, TLS pinning, archive and image validation, camera
  snapshots, Apache reconciliation, and privileged helper input validation.
- Verified the live migration and repeat setup behavior on Debian 12 with Bambu
  and Klipper printers. Repeat `--no-restart` runs preserved the application PID
  and all managed configuration hashes.
