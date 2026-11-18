# Bambu LAN failure detection with Obico

The Print Farm can be the Obico agent for each configured Bambu LAN printer.
Start with one printer, then repeat the same setup with separate identities
for the remaining printers. Home and work installations use their own local
configuration; no printer addresses, access codes, or Obico credentials are
part of the Git repository.

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
configuration.

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

Failure-detection quality still depends on the camera view, lighting and the
Obico model. A working bridge does not by itself prove reliable failure
detection. Test notifications and model performance before relying on it for
unattended prints. This implementation has automated transport/lifecycle tests;
live hardware validation is required for each printer/firmware combination.

## Repeat for the four work P1S printers

The work installation lists its own configured printers. Select each P1S and
click **Create & connect**, using the work Obico server/account. Each receives
its own **printer ID and token** automatically. Enable and test one at a time;
pause on one must not affect any of the others. Home credentials are not
copied to work. No code changes or fixed four-printer limit are required.

## Troubleshooting and disabling

- **Agent token belongs to a different printer:** check the ID/token pairing.
- **Agent token already in use:** stop the other agent or assign a separate
  printer/token. Saving the corrected settings restarts the bridge.
- **Waiting for a fresh camera frame:** check the farm camera view and LAN
  camera access. Stale/duplicate frames are skipped instead of analysed again.
- **Detection results unavailable:** check the account login and printer
  ownership. Status/snapshot transport uses the separate agent token.
- **Monitoring only:** enable the control checkbox in the farm before expecting
  Obico pause/resume/cancel to work. No arbitrary commands or file uploads are
  accepted from Obico.

Disable Obico for that printer in settings to stop its agent. Existing print
control and camera viewing in The Print Farm continue to work.

Protocol references: [Obico agent API](https://github.com/TheSpaghettiDetective/obico-server/blob/release/backend/api/octoprint_views.py),
[Obico WebSocket consumer](https://github.com/TheSpaghettiDetective/obico-server/blob/release/backend/api/consumers.py),
and [moonraker-obico transport](https://github.com/TheSpaghettiDetective/moonraker-obico/blob/master/moonraker_obico/server_conn.py).
