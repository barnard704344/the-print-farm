"""Decode the AMS recovery messages carried by Bambu LAN telemetry.

Codes are from Bambu Studio's resources/hms/hms_en_094.json. Unknown
messages remain visible as codes; informational runout/purge notices alone
must not interrupt a normal automatic filament change.
"""
from copy import deepcopy


def merge_report(previous, update):
    result = deepcopy(previous)
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_report(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def number(value, default=0):
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


PRINT_ERRORS = {
    0x8003: "Filament could not be extracted from the toolhead. Check for stuck filament.",
    0x8004: "Filament could not retract to the AMS. Check the spool and filament path.",
    0x8005: "Filament could not feed out of the AMS. Check the spool and PTFE path.",
    0x8006: "Filament could not reach the toolhead. Check for a broken or blocked filament path.",
    0x8007: "Filament extrusion needs checking. Only confirm continuation if filament is visibly extruding.",
    0x8010: "AMS motor overloaded. Check the spool and filament path before retrying.",
    0x8011: "Filament has run out. Refill the indicated AMS slot, then retry AMS.",
    0x8012: "The AMS filament mapping could not be read. Check the selected filament source.",
    0x8013: "Old filament purge timed out. Check for filament stuck in the toolhead or a clogged nozzle, then retry AMS.",
}
SLOT_ERRORS = {
    0x20001: "Filament has run out. Refill this slot, then retry AMS.",
    0x20002: "The slot is empty. Insert filament before retrying AMS.",
    0x20003: "Filament may be broken inside the AMS. Check the filament path.",
    0x20004: "Filament may be broken in the toolhead. Check the filament path.",
    0x20005: "Filament ran out and the old filament could not be purged. Check for filament stuck in the toolhead, then retry AMS.",
    0x20006: "The PTFE tube may be disconnected. Check the tube between the AMS and extruder.",
    0x20009: "Filament could not extrude. Check for a clog or slipping filament.",
    0x2000A: "Filament position could not be adjusted. Check the filament and buffer for a blockage.",
    0x30001: "Filament ran out; the printer is purging the old filament. Wait for recovery to complete.",
    0x30002: "Filament ran out and the printer switched to a matching spool.",
}


def recovery_state(data):
    errors = []
    source = None
    print_error = number(data.get("print_error"))
    ams_error = (print_error >> 24) in (0x07, 0x12, 0x18) and (print_error & 0xFFFF) in PRINT_ERRORS
    if print_error:
        errors.append({"code": f"0x{print_error:08X}", "message": PRINT_ERRORS.get(print_error & 0xFFFF, f"Printer error 0x{print_error:08X}") if ams_error else f"Printer error 0x{print_error:08X}", "blocking": True, "filament": ams_error})
    for item in data.get("hms") or []:
        attr, code = number(item.get("attr")), number(item.get("code"))
        label = f"HMS_{attr >> 16:04X}_{attr & 0xFFFF:04X}_{code >> 16:04X}_{code & 0xFFFF:04X}"
        slot_message = (attr >> 24) == 7 and (attr & 0xF000) == 0x2000 and code in SLOT_ERRORS
        if slot_message:
            unit, slot = (attr >> 16) & 0xFF, (attr >> 8) & 0x0F
            candidate = {"tray": unit * 4 + slot, "label": f"AMS {chr(65 + unit)} slot {slot + 1}", "blocking": (code >> 16) == 2}
            if source is None or (candidate["blocking"] and not source["blocking"]):
                source = candidate
            message = f"{candidate['label']}: {SLOT_ERRORS[code]}"
        else:
            message = str(item.get("msg") or item.get("description") or label)
        errors.append({"code": label, "message": message, "blocking": slot_message and (code >> 16) == 2, "filament": slot_message})
    status = number(data.get("ams_status"))
    wire_state = str(data.get("gcode_state", "")).upper()
    busy = (status >> 8) == 1 and wire_state not in ("FINISH", "FAILED")
    ams = data.get("ams") or {}
    if source is None:
        for key in ("tray_now", "tray_pre"):
            tray = number(ams.get(key), 255)
            if 0 <= tray < 16:
                source = {"tray": tray, "label": f"AMS {chr(65 + tray // 4)} slot {tray % 4 + 1}"}
                break
    target = number(ams.get("tray_tar"), 255)
    sub = status & 0xFF
    phases = {2: "Heating nozzle", 3: "Cutting filament", 4: "Retracting filament", 5: "Feeding filament", 6: "Checking filament position", 7: "Purging old filament", 8: "Checking filament position", 9: "Waiting for AMS", 11: "Checking filament position"}
    phase = phases.get(sub, "Changing filament") if busy else ""
    if busy and target == 255 and sub in (4, 5, 6, 7):
        phase = "Unloading / purging filament"
    active_job = wire_state in ("RUNNING", "PAUSE")
    blocking_filament = any(e["blocking"] and e["filament"] for e in errors)
    active = active_job and blocking_filament
    confirm = (active_job or (wire_state == "IDLE" and busy)) and (print_error in (0x07FF8007, 0x12FF8007) or (busy and sub == 6 and target == 254))
    messages = [e["message"] for e in errors if e["blocking"]]
    return {
        "active": active, "busy": busy, "phase": phase,
        "message": " ".join(dict.fromkeys(messages)),
        "can_retry": active or (busy and (wire_state == "PAUSE" or (wire_state == "IDLE" and blocking_filament))),
        "can_confirm_extrusion": bool(confirm),
        "source_tray": source["tray"] if source else None,
        "source_label": source["label"] if source else "",
        "errors": errors,
    }
