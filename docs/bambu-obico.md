# Bambu LAN failure detection with Obico

The Print Farm can be the Obico agent for each configured Bambu LAN printer.
Start with one printer, then repeat the same setup with separate identities
for the remaining printers. Home and work installations use their own local
configuration; no printer addresses, access codes, or Obico credentials are
part of the Git repository.

## Obico Server Prerequisites

**You need a working Obico server before this integration can detect print
failures.** The Print Farm's installer installs the bridge and its Python
dependencies; it does not install Obico, download its detection models, or
configure Obico email delivery. Ordinary farm printing works without Obico.

Install and configure your server using the
[upstream Obico server guide](https://www.obico.io/docs/server-guides/), then check:

- The web/API service is reachable from the farm. Set Obico's Django Site domain
  to the address and port used by clients, as described in the upstream guide.
  Configure trusted origins and HTTPS appropriately if using a reverse proxy.
- The background worker, Redis, and ML/detection service are running. The model
  must load and process images; a healthy login page alone does not prove this.
  GPU acceleration is optional if the detection backend supports CPU inference.
- Your Obico account can sign in and owns the selected printer records.
- The farm can reach the server's HTTP(S) and WebSocket endpoints. Reverse
  proxies must forward WebSocket upgrades, and network access must not be
  redirected to a captive portal.
- Printer telemetry and the farm's camera view work. Detection needs fresh
  images during an active print, with enough light and a clear view of the bed.

For an upstream Docker Compose installation, check `docker compose ps` and the
`web`, `tasks`, `redis`, and `ml_api` logs in the Obico server directory. Resolve
model-loading, worker, or connection errors before treating detection as ready.

SMTP is an additional requirement for email alerts. Successful printer linking,
an agent's **Connected** state, or **Test Connection** in The Print Farm does
not verify the model or prove an email reached its recipients.

## Before enabling

1. Enable LAN-only mode on the printer. On firmware requiring it, also enable
   Developer Mode for third-party LAN control. The farm must already show the
   correct printer online and its camera must work in the dashboard.
2. Ensure the farm can reach that printer's MQTT (normally 8883) and P1 camera
   (normally 6000) ports, and the Obico server's HTTP(S)/WebSocket endpoint.
3. Update the farm and its Python requirements, then restart the farm service
   once to load the new bridge. `websocket-client` is the new dependency.
   Native installations can use their existing `setup.sh` update workflow;
   Docker installations must rebuild/use an image containing this change.

The bridge is included in `v1.0.17` and newer farm releases. Use the `v1.0.17`
or corresponding `latest` Docker image, or build current source. Earlier
releases through `v1.0.16` predate the bridge. See
[Docker](docker.md#obico-in-docker).
## Connect from The Print Farm

No browser developer tools or Obico linking codes are needed.

1. Open **Settings → Integrations → Obico AI Failure Detection** in The Print
   Farm. The printer dropdown comes from this installation's configured
   printers; it has no hard-coded names, IP addresses or printer-count limit.
2. Select the Bambu printer you want to connect.
3. Enter your **Obico server address, username/email and password**.
4. Click **Create & connect**.

The farm creates a matching Obico printer record, saves its ID and agent token
privately, and starts monitoring automatically. The token is never returned to
browser code; the settings display only a mask. Registration uses this
installation and physical printer identity, so identical names at different
sites do not share a token. Repeating setup reuses the same registration
instead of creating duplicates. Conflicting registration records are reported
for manual review.

Leave **Allow Obico to pause, resume or cancel this printer** unchecked for
initial tests. New Obico records use **Just notify me**. Failure detection and
notification delivery still require a working Obico ML backend and notification
configuration. See [email notifications](#email-notifications) below.

For another printer, select it and repeat. Every printer gets an independent
Obico identity and connection. The server/account may be shared if it owns the
printers. No home credentials or device addresses are embedded in the software.

If you already created an Obico record under a different name, expand
**Existing printer / advanced settings**, enter its printer ID and agent token,
then use **Test Connection**, enable Obico and save. Klipper printers continue
using their existing Obico agent and printer ID. To retrieve an existing token,
while signed into Obico as its owner open `/api/v1/printers/<id>/` and find
`auth_token`; treat it as a password.

The normal Obico add-printer wizard expects an OctoPrint/Klipper linking code.
The **Create & connect** button handles registration automatically. It also
supports older Obico servers by obtaining and consuming a new unassigned
linking code internally; no manual codes or developer tools are required.

## Email Notifications

There are two independent notification systems:

| Notification | Sends it | Configure it in |
|--------------|----------|-----------------|
| Camera-detected print-failure alerts | Obico | Obico's SMTP configuration and account email notification preferences |
| Farm job submitted/completed/paused/failed alerts | The Print Farm | Settings → Integrations → Notifications in The Print Farm |

The bridge sends status and snapshots to Obico and reads detection results. It
does not forward Obico alerts through the farm's email settings or copy the
farm's recipients into Obico. Both systems can notify about the same print if
you enable overlapping events.

To receive **only camera-detected print-failure emails from Obico**:

1. Configure SMTP on the Obico server, including its sender address and the
   authentication/TLS settings required by your mail server or approved relay.
2. Enable email notifications and failure alerts in Obico's account preferences
   (typically `/user_preferences/notification_email/`). Disable its other email
   events, such as print start, completion, cancellation, pause/resume, filament
   change, and heater events, if listed by your server version.
3. Add any additional addresses under Obico's **E-mail Addresses** page
   (`/accounts/email/`). Supported self-hosted versions send alerts to the
   account's associated addresses, subject to that server's verification
   policy. Complete verification when required. Keep the intended login address
   primary; these are account email addresses, not a separate farm mailing list.
4. Use Obico's email test facility, if available, and confirm receipt at every
   address. An SMTP connection or a message accepted by a relay does not prove
   final mailbox delivery; inspect the mail queue/logs when troubleshooting.

These alerts mean the AI suspects a print failure from the camera images.
They do not promise detection of every printer fault or notify specifically
that a camera has disconnected. Without fresh images and working inference,
camera-based failure detection cannot operate. Notification delivery also
depends on the worker and mail service. Do not create a hazardous print fault
just to test delivery.

## Test with a normal short print

1. Start a short print through your usual slicer. Check that Obico shows the
   correct printer, filename, progress and temperatures.
2. Check that new snapshots continue to arrive after closing the farm
   dashboard. The bridge uses the existing shared camera, not a second camera
   connection. It retries until a fresh frame is available.
   Obico receives periodic snapshots, not a Janus/WebRTC video stream; use
   the farm dashboard for the existing live camera view.
3. Pause/resume locally and check the state in Obico. At completion, check that
   Obico ends the print. For an idle printer no new AI snapshots are uploaded.
4. During a suitable test print, restart the farm service and confirm Obico
   resumes the same print record. Retain the `data/obico` state directory.
   If both Bambu task IDs are unavailable, job continuity falls back to the
   filename and observed states; a complete same-file job change while the farm
   is offline cannot be distinguished reliably.
5. When ready to test controls, enable the farm's Obico control checkbox and
   save. Use Obico to pause and resume **only the selected printer**. Afterwards
   you can choose automatic pause in Obico if desired. The farm does not
   execute commands for an idle, disconnected or stale printer.
6. Confirm the Obico AI service processes the new snapshots and that detection
   results update for the correct printer. A zero failure score alone is not
   proof of inference; check the Obico service logs/results as well.
7. Test email delivery separately and confirm the chosen recipients receive it.
   A normal successful print does not exercise the AI failure-alert email path.

Failure-detection quality still depends on the camera view, lighting and the
Obico model. A working bridge does not by itself prove reliable failure
detection. Test notifications and model performance before relying on it for
unattended prints. This implementation has automated transport/lifecycle tests;
live hardware validation is required for each printer/firmware combination.
Connection, fresh camera uploads, inference, email delivery, and optional
controls are separate checks; complete each before describing an installation
as fully verified.

## Repeat for Additional Printers

The installation lists its own configured printers. Select each supported
Bambu printer and click **Create & connect**, using its Obico server/account. Each receives
its own **printer ID and token** automatically. Enable and test one at a time;
pause on one must not affect any of the others. Home credentials are not
copied to other installations. No code changes or fixed printer-count limit are
required.

## Troubleshooting and disabling

- **Agent token belongs to a different printer:** check the ID/token pairing.
- **Agent token already in use:** stop the other agent or assign a separate
  printer/token. Saving the corrected settings restarts the bridge.
- **Waiting for a fresh camera frame:** check the farm camera view and LAN
  camera access. Stale/duplicate frames are skipped instead of analysed again.
- **Detection results unavailable:** check the account login and printer
  ownership. Status/snapshot transport uses the separate agent token.
- **Connected but no detection:** confirm an active print, recent camera uploads,
  and a working Obico ML backend/model and task worker. A connection test checks
  account/printer access, not AI processing. Idle printers do not upload AI frames.
- **Obico email not arriving:** check Obico's own SMTP, sender, account addresses,
  verification policy, enabled events, worker logs, and relay/mail queue. The
  farm's successful email test does not test Obico.
- **Old failed job shown as an Obico error:** the bridge currently derives its
  error flag from Bambu's raw `FAILED` state. The farm may already show Ready
  after its failed-printer cooldown, while Obico retains that flag. Inspect the
  printer's current `print_error`/HMS details and build-plate state before treating
  it as a live hardware fault. This flag alone is not an AI failure alert.
- **Monitoring only:** enable the control checkbox in the farm before expecting
  Obico pause/resume/cancel to work. No arbitrary commands or file uploads are
  accepted from Obico.

Disable Obico for that printer in settings to stop its agent. Existing print
control and camera viewing in The Print Farm continue to work.

Protocol references: [Obico agent API](https://github.com/TheSpaghettiDetective/obico-server/blob/release/backend/api/octoprint_views.py),
[Obico WebSocket consumer](https://github.com/TheSpaghettiDetective/obico-server/blob/release/backend/api/consumers.py),
and [moonraker-obico transport](https://github.com/TheSpaghettiDetective/moonraker-obico/blob/master/moonraker_obico/server_conn.py).
