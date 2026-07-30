"""Thread-safe, atomic YAML configuration persistence."""

import os
import tempfile
import threading

import yaml

_CONFIG_LOCK = threading.RLock()


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
