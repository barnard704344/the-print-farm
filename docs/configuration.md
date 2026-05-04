# Configuration

## Software Updates

Settings tab provides:

- Check for Updates
- Apply Update and Restart

For one-click restart from the web UI, grant service user permission:

```bash
www-data ALL=(root) NOPASSWD: /usr/bin/systemctl restart the-print-farm.service
```

Add as sudoers drop-in with mode 440.

New installs run this automatically via setup.sh.

Use the manual sudoers rule above only for legacy installs that were set up before this automation was added.

## Printer Pool

```yaml
pool:
  enabled: true
  printers:
    - Voron-01
    - P1S-1
```

Only generic queue jobs are auto-dispatched.

## Spoolman

```yaml
spoolman:
  url: http://localhost:7912
```

## Notifications

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

No required config. Auto-detected on compatible Klipper printers.

## REST API Usage

```bash
curl -H "X-Api-Key: YOUR_KEY" http://localhost:5000/the-print-farm/api/v1/printers
curl -H "X-Api-Key: YOUR_KEY" http://localhost:5000/the-print-farm/api/v1/printers/MyPrinter
curl -X POST -H "X-Api-Key: YOUR_KEY" -F "file=@model.gcode" \
  http://localhost:5000/the-print-farm/api/v1/jobs
curl -H "X-Api-Key: YOUR_KEY" http://localhost:5000/the-print-farm/api/v1/openapi.json
```
