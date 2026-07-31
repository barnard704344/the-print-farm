"""Thread-safe, atomic YAML configuration persistence."""

import os
import secrets
import tempfile
import threading

import yaml

_CONFIG_LOCK = threading.RLock()
MIN_API_KEY_LENGTH = 16


def load_config(path):
    """Load a YAML mapping while serialising access with local writers."""
    with _CONFIG_LOCK, open(path, "r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def save_config(path, config):
    """Atomically replace a YAML config and restrict it to its owner."""
    absolute_path = os.path.realpath(path)
    directory = os.path.dirname(absolute_path)
    os.makedirs(directory, exist_ok=True)

    with _CONFIG_LOCK:
        fd, temp_path = tempfile.mkstemp(prefix=".config-", suffix=".yaml", dir=directory)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                yaml.safe_dump(config, handle, default_flow_style=False, sort_keys=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, absolute_path)
            os.chmod(absolute_path, 0o600)
            directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            raise


def migrate_api_keys(config):
    """Split the legacy shared key into restricted Orca and admin credentials.

    The legacy key is deliberately preserved as the Orca key so existing
    OrcaSlicer clients continue working. A new administrator key is generated
    and is never returned to dashboard users.
    """
    web = config.setdefault("web", {})
    changed = False

    legacy_web_key = str(web.pop("api_key", "") or "")
    legacy_root_key = str(config.pop("api_key", "") or "")
    legacy_key = legacy_web_key or legacy_root_key
    if legacy_web_key or legacy_root_key:
        changed = True

    orca_key = str(web.get("orca_api_key") or "")
    if not orca_key or orca_key == "CHANGE_ME":
        orca_key = legacy_key if legacy_key and legacy_key != "CHANGE_ME" else secrets.token_urlsafe(24)
        web["orca_api_key"] = orca_key
        changed = True
    if len(orca_key) < MIN_API_KEY_LENGTH:
        raise ValueError("web.orca_api_key must be at least 16 characters")

    admin_key = str(web.get("admin_api_key") or "")
    if not admin_key or admin_key == "CHANGE_ME" or secrets.compare_digest(admin_key, orca_key):
        admin_key = secrets.token_urlsafe(24)
        while secrets.compare_digest(admin_key, orca_key):
            admin_key = secrets.token_urlsafe(24)
        web["admin_api_key"] = admin_key
        changed = True
    if len(admin_key) < MIN_API_KEY_LENGTH:
        raise ValueError("web.admin_api_key must be at least 16 characters")

    return changed
