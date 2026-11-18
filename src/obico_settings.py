"""Validate per-printer Obico settings without exposing saved credentials."""

from .bambu_obico import MASK, server_url


def prepare_obico_config(data, saved, printer_type):
    server = server_url(data.get("server"))
    try:
        printer_id = int(data.get("printer_id", 0))
    except (ValueError, TypeError):
        printer_id = 0
    if printer_id < 1 or isinstance(data.get("printer_id"), bool):
        raise ValueError("Obico requires a positive printer ID")
    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "")
    if password == MASK:
        if server != saved.get("server") or username != saved.get("username"):
            raise ValueError("Re-enter the Obico password after changing the server or username")
        password = saved.get("password", "")
    if not username or not password:
        raise ValueError("Obico requires username and password")
    config = dict(server=server, printer_id=printer_id, username=username, password=password)
    if printer_type != "klipper":
        token = str(data.get("auth_token") or "").strip()
        if token == MASK:
            if server != saved.get("server") or printer_id != saved.get("printer_id"):
                raise ValueError("Re-enter the agent token after changing the server or printer ID")
            token = saved.get("auth_token", "")
        if not token or token == MASK or len(token) > 512 or any(ord(c) < 33 or ord(c) > 126 for c in token):
            raise ValueError("Bambu Obico requires a valid printer agent token")
        allow = data.get("allow_remote_control", False)
        if not isinstance(allow, bool):
            raise ValueError("Remote control must be enabled or disabled")
        config.update(auth_token=token, allow_remote_control=allow)
        registration_key = (saved.get("registration_key")
                            if server == saved.get("server") and printer_id == saved.get("printer_id")
                            else data.get("registration_key"))
        if registration_key:
            if not isinstance(registration_key, str) or len(registration_key) != 24 or any(c not in "0123456789abcdef" for c in registration_key):
                raise ValueError("Invalid printer registration identity")
            config["registration_key"] = registration_key
    return config


def validate_obico_assignment(name, candidate, printers):
    for p in printers:
        other = p.get("obico") or {}
        if p.get("name") == name or other.get("server", "").rstrip("/") != candidate["server"]:
            continue
        if str(other.get("printer_id")) == str(candidate["printer_id"]):
            raise ValueError("This Obico printer ID is already assigned to another farm printer")
        if candidate.get("auth_token") and other.get("auth_token") == candidate["auth_token"]:
            raise ValueError("Each Bambu printer needs its own Obico agent token")
