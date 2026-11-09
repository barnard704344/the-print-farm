# Changelog

This file records notable user-facing, security, deployment, and compatibility
changes to The Print Farm.

## Unreleased

### Security

- Strip any client-supplied `X-Remote-User` header in the managed Apache proxy
  block. Previously, SSO identity headers could reach `/api/auth/sso` through
  proxy paths not covered by the Kerberos-protected `<Location>` (for example
  `/the-print-farm/the-print-farm/api/auth/sso`), allowing staff impersonation.
  Existing installations with a hand-written SSO `<Location>` should also add
  `RequestHeader unset X-Remote-User early` at the VirtualHost level.
- Require authentication for `POST /api/jobs/<id>/check_filament`, which
  previously accepted anonymous requests.
- Only staff and approved (allowlisted, not banned) students can fetch the
  OrcaSlicer upload key from the dashboard. Rotate `web.orca_api_key` if
  unapproved students may already hold it.

### Added

- **Reworked File Library.**
  - **Folder tree:** a sidebar shows every folder. Folders expand when you open
    them, or when you hover over one while dragging.
  - **Folder in the address:** the current folder is part of the URL
    (`#library/<id>`), so refresh, the back button and links keep your place.
  - **Bulk actions:** tick files (Shift-click for a range, Ctrl/Cmd-A for all)
    to print, move or delete them together. New staff-only endpoints:
    `POST /api/library/files/bulk-move` and `/bulk-delete`.
  - **Drag and drop:** drag files (or the whole selection) and folders onto
    the tree, folder cards or breadcrumbs. Invalid drops are refused.
  - **Move dialog:** a clickable folder list replaces the dropdown, and invalid
    targets are greyed out.
  - **Print dialog:** shows each printer's state, and you can add to the queue
    or send straight to an idle printer, with a copies count.
  - **Views:** grid or list, sorted by date, name, size, print time, last
    printed or times printed. Remembered per browser.
  - **Feedback:** success and error messages for every action.
- **Single-user mode.** Small farms with one operator can tick **Disable
  login** in Settings (or set `web.auth_enabled: false`). Every visitor is
  then treated as staff, the header shows "Single-user mode", and a warning is
  logged at startup. Turning login back on requires a staff sign-in method; if
  there is none, Settings asks for a username and password and creates a local
  staff account, so you can't lock yourself out. API-key integrations and
  OrcaSlicer upload keys are unaffected.
- **Redesigned printer cards.** Each card now shows nozzle size and type under
  the name, plus status chips for Wi-Fi signal, HMS alerts and print errors
  (codes in the tooltip), the chamber light (a toggle for anyone who can control
  the printer), and staff-only. A now-printing panel combines the job thumbnail
  (from the File Library), progress, time remaining, finish clock time, layers,
  and speed. Temperatures are compact tiles: click one to set a preset or a
  custom value. Pause/Resume and Stop sit beside the tiles, and Stop now asks
  for confirmation. The AMS is split into one block per unit, each with its own
  humidity (as a percentage on newer firmware) and temperature, and a
  remaining-filament bar under each slot. New endpoint:
  `GET /api/printer/<name>/thumbnail`.
- **Solid 3D toolpath preview.** The File Library viewer now draws extrusions
  as solid, lit beads sized from the slicer's line width and layer height, with
  OrcaSlicer-style feature colours, a feature legend with show/hide toggles, a
  layer slider, a printer bed, and Reset/Top view buttons. Touch pinch-zoom is
  supported.
- **Build plate check.** A finished or failed print marks the printer's plate
  as needing clearing, and the printer will not accept another job (queue,
  reprint, API, or pool dispatch) until staff or an approved student presses
  **Plate cleared** on its card. Prints started at the printer count as
  confirmation, the state persists across restarts, and staff can disable the
  check per printer (`require_plate_clear`).
- **Camera Wall** at `/the-print-farm/cameras`: a full-screen grid of every
  visible printer camera with status, progress, time remaining, and plate
  warnings for classroom screens.
- Added a staff-only **Reconnect** workflow for offline printers. It scans for
  a changed IP address, verifies the cached printer identity, updates only the
  host, and preserves credentials, ports, access rules, AMS/MMU assignments,
  OrcaSlicer configuration, and integrations.
- Added bounded `/21` through `/24` scans for routed printer VLANs, including
  configurable default ranges under `discovery.scan_subnets`.
- Allowlisted students can now work on the shared print queue. They see the
  **Queued Jobs** tab and can send or cancel any queued job on printers that are
  not staff-only, including unowned jobs submitted from OrcaSlicer. Jobs already
  printing, delete, bulk actions, and requeue remain staff-only.
- Allowlisted students can use printer card controls (pause/resume/stop,
  temperatures, chamber light, fans and LEDs, filament load/unload, AMS tray
  load and tray configuration) on printers that are not staff-only. Other
  students see printer cards and cameras read-only. Rename, remove, reconnect,
  the staff-only toggle, Klipper e-stop, and Settings remain staff-only.

### Fixed

- **Klipper fixes.**
  - **Layer counts:** the cached layer count for a file name is now cleared on
    every upload. With shorter printer file names, two slices of the same
    model can share a name and would otherwise show the old layer total.
  - **Toolchanger temperatures:** toolhead panels now use the same
    click-to-set popover as the other tiles. The old inputs could switch a
    heater off: an empty box, or a printer name with a space, sent 0 °C, and
    typed values were wiped by every card refresh.
  - **E-Stop:** now asks for confirmation, since Klipper needs a
    FIRMWARE_RESTART afterwards. Printer names are escaped in the E-Stop and
    light buttons.
  - **Temperature input:** the nozzle and bed temperature endpoints return 400
    for non-numeric input instead of a server error.
- **Printers ready for a job now say READY.** Bambu printers stay in FINISH
  until the next job starts, so cards kept saying "Finished" even after the
  plate was cleared. A printer that is idle, or finished with its plate
  confirmed clear (or the plate check off), now shows a green READY with the
  last job underneath. A finished print still on the plate shows an amber
  FINISH. The Camera Wall and the API's `display_status` match.
- **Staff only and Build plate check moved into Details.** They were tick
  boxes on every staff card. They now sit under "Printer settings" in the
  printer's Details panel, with an explanation each. A "Plate check off" chip
  on the card warns staff when the check is disabled, and both settings show
  a confirmation message when changed.
- **Readable job names.** Bambu printers used to receive the stored file
  name, e.g. `f627a845…_cade_pip2_PLA_9m8s.3mf`, so cards, the Camera Wall and
  the printer screen showed a long, unreadable name. Printers now get a short
  name (`cade_pip2.3mf`). The dashboard shows a tidy name ("cade pip2")
  everywhere, with the full file name in the tooltip. The tidy name drops the
  random prefix, the extension and OrcaSlicer's `_PLA_9m8s` filament/time
  suffix, which is already shown separately.
- **Deleting a job no longer deletes a file another queued job needs.**
  Reprints, per-printer copies and repeat OrcaSlicer uploads of the same model
  share one file. Deleting one of those jobs used to remove the file if it
  wasn't in the library, so the remaining job failed when sent. The file is
  now kept while the library or any queued, uploading or printing job uses it.
- **OrcaSlicer virtual-printer uploads go into the File Library straight away**
  instead of only after a service restart. Repeat uploads of the same model are
  added once.
- **Job delete with "also delete from library" reports library files it had to
  keep** because other jobs still use them, instead of silently claiming
  success.
- **API v1 `DELETE /library/files/<id>`** now returns 409 when the file is in
  use and 404 when it doesn't exist, instead of 200 with an error inside.
- **Print from library sent to the chosen printer.** It used to ignore the
  printer you picked and only queued the file.
- **Folder names with quotes** no longer break that folder's buttons. Folder
  names are now limited to 100 characters, with extra spaces collapsed.
- **Students no longer see folder rename/move buttons** that always failed.
- **Filament load/unload now matches Bambu Studio and Bambuddy.**
  - **Printers without an AMS:** "Load Filament" used to send raw G-code that
    homed X and extruded 30 mm at 220 °C, with nothing to stop it during a print.
    It now runs the printer's own external-spool load.
  - **AMS loads:** the command now names the AMS unit and slot, and lets the
    printer pick the filament's temperature instead of forcing 220 °C.
  - **Unloads:** these now use the current nozzle temperature (210 °C when the
    nozzle is cold).
  - **AMS HT slots:** these are now addressed correctly.
  - **During prints:** load and unload are refused while a print is running
    (dashboard, `/api/printer/...` and API v1), but still allowed while paused.
  - **Klipper printers:** these no longer show load/unload buttons that always
    failed.
  - **AMS details window:** this now has a Load button on each slot, so staff
    can load a specific slot.
  - **Feedback:** every load/unload shows a success or error message.
- The 3D toolpath viewer no longer cuts prints off partway up. Arcs were split
  into 0.08 mm pieces, so moderate Bambu prints hit the 200,000-segment limit
  and later layers were silently dropped (for example, a 14 mm print stopped at
  5.8 mm). Arcs now use a 0.01 mm chord tolerance, the limit is 1.5 million
  segments, and truncation is shown in the viewer. Toolpaths are sent in a
  compact binary format and cached.
- The viewer no longer throws errors on window resize after it is closed, and
  orbiting no longer sticks when the mouse is released outside the canvas.
- Open cameras no longer exhaust the web server. The full-screen camera view
  polls snapshots instead of holding an MJPEG stream open, snapshots answer
  `304 Not Modified` when the frame is unchanged, the MJPEG endpoint is capped
  at four viewers for ten minutes each, cameras stop automatically when nobody
  is viewing them, and the web server has 16 worker threads instead of 8.
- **Stop** on a camera feed now hides it for that viewer only instead of
  stopping it for everyone; force-stopping a camera is staff-only. Students can
  no longer view cameras on staff-only printers.
- The service now loads its configuration from `FARM_CONFIG`, the same file the
  web UI saves to. Previously startup read `config/config.yaml` while the UI
  wrote `FARM_CONFIG`, so dashboard changes (student access, bans, Reconnect,
  AD settings) were lost on restart.
- Klipper toolchanger cards no longer lose per-tool temperatures, temperature
  controls, and tool fans when Spoolman is not configured or Happy Hare is
  enabled. Only the per-tool spool row now depends on Spoolman.
- Printer card pause/resume/stop buttons now refresh with status for students
  who own the active job.
- Keep the Reconnect action in sync when an existing dashboard card transitions
  from online to offline.
- Exclude addresses assigned to other configured printers and require cached
  access-code and serial verification before presenting port-scan-only BambuLab
  candidates on networks containing multiple printers.

### Documentation

- Documented the student access levels for viewing, queue work, and printer
  controls, and how allowlist entries are matched.
- Documented IP-change recovery, routed subnet scanning, identity safeguards,
  and the new discovery configuration with an example `/21` printer VLAN.

## v1.0.14 - 2026-07-31 - Reliable updates and integration diagnostics

### Fixed

- Run native dashboard update reconciliation in a transient privileged systemd
  unit. Hardened installations can now complete service-user access checks and
  upgrade fully through the UI without requiring shell access.
- Queue the post-update service restart asynchronously so the updater does not
  wait on the application process that issued its own restart request.
- Preserve the public request host through the managed Apache proxy so CSRF
  origin validation accepts same-origin dashboard actions such as deleting
  queued jobs.
- Report Obico authentication and printer-ID failures in Settings and Printer
  Details, and let connection tests reuse a saved masked password instead of
  submitting the mask as a credential.
- Accept nullable Obico status and image fields returned by self-hosted servers
  without dropping the integration state.

### Changed

- Removed a retired Klipper integration, including its port probe, runtime
  state, API payload, dashboard card, Details row, and external link.
- Replaced the audience-specific dashboard tagline with the concise description
  **Simple print farm management**. Documentation continues to identify schools
  as the primary audience while including homes, makerspaces, and other small
  print farms.

### Documentation

- Updated native upgrade, Obico configuration, API, release-pinned checkout,
  and Docker image instructions for `v1.0.14`.

## v1.0.13 - 2026-07-31 - Zero-touch Orca credential split

### Fixed

- Restored hostname-only General Queue connections on port 80 using narrowly
  matched OctoPrint-compatible routes. Existing OrcaSlicer clients do not need
  a URL path or configuration change, and unrelated `/api` routes remain owned
  by their applications.

### Security

- Split the former shared credential into an upload-only Orca key and a newly
  generated hidden administrator key. Existing Orca installations keep their
  current key unchanged, but that key no longer authorises general or
  administrative API access.
- Made the Orca key available from Connection Details to logged-in students and
  staff while keeping it out of the public dashboard HTML. Anonymous users
  cannot retrieve it.

### Deployment

- Updated native setup, upgrades, manual startup, and Docker startup to migrate
  legacy `web.api_key` configurations automatically. The legacy value becomes
  `web.orca_api_key`; a distinct `web.admin_api_key` is generated server-side.

## v1.0.12 - 2026-07-31 - Queue workflow and Orca connection recovery

### Fixed

- Restored the shared OrcaSlicer/API integration key to the General Queue
  connection table and Settings so staff can configure clients on networks
  where the per-printer ports are blocked.
- Kept the key out of the initial dashboard HTML and exposed it only through a
  non-cacheable endpoint requiring an authenticated staff browser session.
  Anonymous visitors and student sessions cannot retrieve it.

### Changed

- Restored the original green Print Farm colour palette with
  consistent interface icons, solid page backgrounds, flatter cards, and fewer
  nested borders.
- Merged primary navigation into the application header, removing the detached
  floating navigation bar while retaining an integrated scrolling row on
  narrower screens.
- Printer Details now opens in a responsive right-side panel so the printer grid
  remains visible in context.
- Removed the duplicate approval-and-dispatch panel and retained the original
  queue action workflow as the single place staff dispatch jobs.
- Split the Job Queue into Queued Jobs and Job History sub-tabs. Staff can view
  the complete newest-first queue separately from chronological history while
  retaining the original dispatch and job actions.

### Documentation

- Audited the complete documentation set against `v1.0.12`, including
  authentication boundaries, native and Docker updates, failed-printer
  readiness, camera rotation, optional Spoolman behaviour, and AMS controls for
  tagged and generic filament.
- Documented staff-only retrieval of the OrcaSlicer connection key from the
  dashboard.
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
