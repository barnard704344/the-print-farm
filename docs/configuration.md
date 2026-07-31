# Configuration

## Display Preferences

Control how dates and times are shown across the dashboard. Configure from **Settings → Display Preferences**.

- **Timezone** — Select any IANA timezone (e.g. `Australia/Sydney`, `Europe/London`, `America/New_York`). Leave blank to auto-detect from each viewer's browser. The selected timezone is persisted server-side so all users see the same zone by default.
- **Locale** — Controls date format. Currently fixed to `en-AU` (DD/MM/YYYY). Configurable via the API if needed.
- **Failed printer cooldown** — After this many minutes, a connected printer
  that still reports `FAILED` is presented as `READY` and may receive a new
  staff-approved job. This never resends a job automatically. Set it to `0` to
  keep failed printers blocked until their reported state changes. A real
  non-failed state clears the timer.

```yaml
ui:
  timezone: Australia/Sydney
  locale: en-AU
  failed_printer_timeout_minutes: 5
```

Or configure from **Settings → Display Preferences** in the dashboard.

## Software Updates

Settings tab provides:

- Check for Updates
- Apply Update and Restart

These controls are for native Git checkouts installed with `setup.sh`. They use
`git fetch` plus a fast-forward-only update and refuse to run when the worktree
has local changes. Docker deployments must pull and recreate the container as
described in [Docker](docker.md).

`setup.sh` installs a root-owned deployment helper with a fixed configuration.
The service can invoke that helper only for validated update, Apache, OrcaSlicer,
and service-restart operations. Do not grant the service account direct
passwordless access to `systemctl`, file-writing tools, Git, or Apache commands.
Deployment reconciliation runs in a short-lived root systemd unit so its
service-user access checks do not inherit the web application's restricted
capability set. This also lets an existing installation bootstrap the updater
fix entirely through the dashboard.
The managed Apache proxy preserves the browser-facing host so same-origin
validation continues to work for dashboard actions routed through Apache.

Legacy installations should rerun `sudo bash setup.sh --restart` while printers
are idle. The script replaces older broad sudoers rules and validates the new
configuration before restarting. Both setup and the web updater refuse an
ordinary restart while print activity is detected.

If an older install reports `.git/objects` permission errors from the update
button, rerun:

```bash
sudo bash setup.sh --restart
```

This reconciles repository ownership and reinstalls the helper. Do not work
around the error by making `.git` or the application tree world-writable.

The live configuration is stored at `/etc/the-print-farm/config.yaml` with mode
`0600`; `config/config.yaml` remains as a symlink for compatibility.

## API Credentials

The server maintains two separate credentials:

```yaml
web:
  orca_api_key: generated-upload-key
  admin_api_key: generated-hidden-administrator-key
```

- `orca_api_key` is shown in **OrcaSlicer Setup → Connection Details** to
  logged-in students and staff. It authorises only OctoPrint-compatible
  connection checks and uploads.
- `admin_api_key` is never displayed in the dashboard. It is reserved for
  trusted server integrations and privileged API endpoints.

When upgrading an older installation, its existing `web.api_key` is preserved
unchanged as `orca_api_key`, keeping all existing OrcaSlicer clients connected.
Setup generates a new, distinct `admin_api_key`.

## Printer Pool

Auto-dispatch jobs from the generic OrcaSlicer port to idle printers:

- **Configurable pool** — Choose which printers participate in auto-dispatch from Settings → Printer Pool
- **Toggle on/off** — Enable or disable pool dispatch without removing the printer list
- **Generic port only** — Only affects jobs submitted without a printer target; per-printer port jobs are unaffected
- **Hot-reloadable** — Pool config changes take effect immediately, no restart required
- **Smart filtering** — Only dispatches to pool printers that are connected and idle

```yaml
pool:
  enabled: true
  printers:
    - Voron-01
    - P1S-1
```

Or configure from Settings → Printer Pool in the dashboard.

## Spoolman

Optional integration with [Spoolman](https://github.com/Donkie/Spoolman) filament tracking:

- **Spool management** — View, search, and manage spools via proxied API endpoints
- **Auto-deduction** — Filament usage is automatically deducted from matched spools when print jobs complete
- **Gate linking** — Assign Spoolman spools to Happy Hare MMU gates for per-gate filament tracking
- **AMS linking** — Assign a Spoolman spool to each BambuLab AMS tray
- **Settings UI** — Configure the Spoolman URL and test connectivity from the dashboard Settings tab
- **Graceful fallback** — All Spoolman features are optional; the system works normally without it

```yaml
spoolman:
  url: http://localhost:7912
```

Or configure from Settings in the dashboard. Saving a blank URL disables the
integration and removes optional spool assignment controls after restart; AMS,
MMU, and generic filament controls continue to work.

## Camera Display Rotation

Printer-card and full-screen camera controls rotate the displayed image in
90-degree steps. The selected value is saved per printer in browser local
storage, not in `config.yaml`, so different workstations may use different
orientations.

## Retired Build-Plate Detection

Camera-based build-plate detection was removed in `v1.0.11` to keep dispatch
lightweight and predictable. Current setup upgrades remove the retired
`plate_detection` configuration and `plate_blocked` notification event while
creating a private pre-migration configuration backup.

## Notifications

Email and Discord alerts for print events:

- **Email (SMTP)** — Configurable SMTP host, port, TLS, authentication, and recipient list
- **Discord webhook** — Sends rich embed messages to any Discord channel
- **Four events** — Job submitted, print completed, print paused, and print failed — each independently toggleable
- **Error context** — Failed and paused notifications include the reason (error code, HMS messages, filament runout) in the subject line
- **Smart deduplication** — Pause notifications only fire on state transition (RUNNING → PAUSED), not on every poll cycle
- **Test buttons** — Send a test email or Discord message from the Settings UI to verify your setup
- **Hot-reloadable** — Config changes take effect immediately, no restart required

```yaml
notifications:
  enabled: true
  events:
    job_submitted: true
    print_completed: true
    print_paused: true
    print_failed: true
  email:
    enabled: true
    smtp_host: smtp.gmail.com
    smtp_port: 587
    use_tls: true
    username: you@gmail.com
    password: app-password
    from_address: you@gmail.com
    to_addresses:
      - recipient@example.com
  discord:
    enabled: true
    webhook_url: https://discord.com/api/webhooks/...
```

## Happy Hare

No configuration needed — Happy Hare MMU is auto-detected on Klipper printers that have it installed. The MMU section appears on printer cards automatically with gate status, active tool, and filament state. See [Printers and OrcaSlicer](printers-and-orcaslicer.md) for full Happy Hare feature details.

## REST API

See [API Reference](api-reference.md) for the full endpoint list, authentication details, and usage examples.
