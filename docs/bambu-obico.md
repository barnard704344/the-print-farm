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
4. In Obico, add a **separate printer** for this physical Bambu. Obtain its
   printer ID and agent authentication token from manual agent setup. If your
   Obico UI doesn't expose the token, while signed into Obico open
   `/api/v1/printers/<id>/`: the printer owner's response includes `auth_token`.
   Treat that value as a password. Do not reuse a Klipper printer's identity.

## Configure one home printer

Open **Settings → Integrations → Obico AI Failure Detection** in The Print
Farm, select the Bambu printer, and enter:

- Obico server URL and this printer's ID.
- The matching **printer agent token**.
- Your Obico username/email and password (used to retrieve detection results).

Leave **Allow Obico to pause, resume or cancel this printer** unchecked for
the first test. Click **Test Connection**, then enable Obico and save. The
bridge starts immediately; no printer or service restart is needed when
changing these settings.

In Obico, enable failure detection for the printer and choose **Just notify
me** initially. There must also be a functioning Obico ML backend and working
notification delivery. A successful connection test checks credentials and
printer identity, not the ML backend, camera, notifications, or print controls.

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

Create four Obico printer records and configure each work printer separately.
Each needs its own **printer ID and token**. Server/account credentials may be
shared if the account owns those four printers. Enable and test one at a time;
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
