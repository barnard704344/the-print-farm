# Configuration

## Display Preferences

Control how dates and times are shown across the dashboard. Configure from **Settings → Display Preferences**.

- **Timezone** — Select any IANA timezone (e.g. `Australia/Sydney`, `Europe/London`, `America/New_York`). Leave blank to auto-detect from each viewer's browser. The selected timezone is persisted server-side so all users see the same zone by default.
- **Locale** — Controls date format. Currently fixed to `en-AU` (DD/MM/YYYY). Configurable via the API if needed.

```yaml
ui:
  timezone: Australia/Sydney
  locale: en-AU
```

Or configure from **Settings → Display Preferences** in the dashboard.

## Software Updates

Settings tab provides:

- Check for Updates
- Apply Update and Restart

`setup.sh` installs a root-owned deployment helper with a fixed configuration.
The service can invoke that helper only for validated update, Apache, OrcaSlicer,
and service-restart operations. Do not grant the service account direct
passwordless access to `systemctl`, file-writing tools, Git, or Apache commands.

Legacy installations should rerun `sudo bash setup.sh --restart` while printers
are idle. The script replaces older broad sudoers rules and validates the new
configuration before restarting. Both setup and the web updater refuse an
ordinary restart while print activity is detected.

The live configuration is stored at `/etc/the-print-farm/config.yaml` with mode
`0600`; `config/config.yaml` remains as a symlink for compatibility.

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
- **Settings UI** — Configure the Spoolman URL and test connectivity from the dashboard Settings tab
- **Graceful fallback** — All Spoolman features are optional; the system works normally without it

```yaml
spoolman:
  url: http://localhost:7912
```

Or configure from Settings in the dashboard. Leave unconfigured to disable Spoolman features.

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
    plate_blocked: true
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
