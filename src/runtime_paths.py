"""Runtime paths shared by the installer and application."""

from pathlib import Path


def obico_state_directory(queue, base_dir=None):
    """Place persistent printer identities beside the database unless overridden."""
    queue = queue or {}
    configured = queue.get("obico_state_dir")
    path = (Path(configured) if configured else
            Path(queue.get("db_path") or "./data/farm.db").parent / "obico")
    path = path.expanduser()
    if not path.is_absolute():
        path = Path(base_dir or Path.cwd()) / path
    return path.resolve()
