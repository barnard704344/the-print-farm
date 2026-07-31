#!/bin/bash
# The Print Farm installer and upgrade reconciler.
# Supports Debian, Ubuntu, and Raspberry Pi OS. Run as root.

set -Eeuo pipefail
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_CONFIG_PATH="${SCRIPT_DIR}/config/config.yaml"
RUNTIME_CONFIG_DIR="/etc/the-print-farm"
RUNTIME_CONFIG_PATH="${RUNTIME_CONFIG_DIR}/config.yaml"
CONFIG_PATH="$REPO_CONFIG_PATH"
SERVICE_NAME="the-print-farm"
SERVICE_PATH="/etc/systemd/system/${SERVICE_NAME}.service"
HELPER_SOURCE="${SCRIPT_DIR}/scripts/the-print-farm-helper"
HELPER_PATH="/usr/local/sbin/the-print-farm-helper"
HELPER_CONFIG="/etc/the-print-farm-helper.json"
SUDOERS_PATH="/etc/sudoers.d/the-print-farm"
UNIT_TEMP=""
UNIT_TEMP_DIR=""
HELPER_TEMP=""
SUDOERS_TEMP=""
SYNC_FILE=""
CONFIG_LINK_DIR=""

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info() { printf "${CYAN}[INFO]${NC}  %s\n" "$*"; }
ok() { printf "${GREEN}[OK]${NC}    %s\n" "$*"; }
warn() { printf "${YELLOW}[WARN]${NC}  %s\n" "$*"; }
fail() { printf "${RED}[FAIL]${NC}  %s\n" "$*" >&2; exit 1; }

on_error() {
    local status=$?
    printf "${RED}[FAIL]${NC}  setup.sh failed at line %s (status %s)\n" \
        "${BASH_LINENO[0]}" "$status" >&2
    exit "$status"
}
trap on_error ERR

cleanup() {
    [[ -z "$UNIT_TEMP" ]] || rm -f -- "$UNIT_TEMP"
    [[ -z "$UNIT_TEMP_DIR" ]] || rmdir --ignore-fail-on-non-empty -- "$UNIT_TEMP_DIR"
    [[ -z "$HELPER_TEMP" ]] || rm -f -- "$HELPER_TEMP"
    [[ -z "$SUDOERS_TEMP" ]] || rm -f -- "$SUDOERS_TEMP"
    [[ -z "$SYNC_FILE" ]] || rm -f -- "$SYNC_FILE"
    if [[ -n "$CONFIG_LINK_DIR" ]]; then
        rm -f -- "${CONFIG_LINK_DIR}/config.yaml"
        rmdir --ignore-fail-on-non-empty -- "$CONFIG_LINK_DIR"
    fi
}
trap cleanup EXIT

usage() {
    cat <<'EOF'
Usage: sudo bash setup.sh [options]

Options:
  --start           Start an inactive service after setup.
  --restart         Restart an active service (or start an inactive one).
  --force-restart   Permit --restart even if print activity cannot be ruled out.
  --no-restart      Never start or restart the service.
  --skip-packages   Skip apt package installation.
  -h, --help        Show this help.

Without a service option, setup asks interactively. In a non-interactive shell
it leaves the service state unchanged.
EOF
}

SERVICE_ACTION="ask"
SERVICE_OPTION_COUNT=0
FORCE_RESTART=false
SKIP_PACKAGES=false
while (($#)); do
    case "$1" in
        --start) SERVICE_ACTION="start"; ((SERVICE_OPTION_COUNT += 1)) ;;
        --restart) SERVICE_ACTION="restart"; ((SERVICE_OPTION_COUNT += 1)) ;;
        --force-restart) FORCE_RESTART=true ;;
        --no-restart) SERVICE_ACTION="none"; ((SERVICE_OPTION_COUNT += 1)) ;;
        --skip-packages) SKIP_PACKAGES=true ;;
        -h|--help) usage; exit 0 ;;
        *) fail "Unknown option: $1" ;;
    esac
    shift
done
((SERVICE_OPTION_COUNT <= 1)) || \
    fail "Choose only one of --start, --restart, or --no-restart"

if ((EUID != 0)); then
    fail "Run this installer as root: sudo bash setup.sh"
fi
case "$SCRIPT_DIR" in
    *$'\n'*|*$'\r'*|*\"*|*\\*|*%*)
        fail "The installation path contains characters that systemd cannot safely encode"
        ;;
esac
[[ -f "$HELPER_SOURCE" ]] || fail "Missing helper source: ${HELPER_SOURCE}"
[[ -f "${SCRIPT_DIR}/requirements.txt" ]] || fail "Run setup.sh from a complete repository checkout"
cd "$SCRIPT_DIR"
for runtime_directory in config data uploads logs venv; do
    [[ ! -L "${SCRIPT_DIR}/${runtime_directory}" ]] || \
        fail "${SCRIPT_DIR}/${runtime_directory} must be a real directory, not a symlink"
done
[[ ! -L "$RUNTIME_CONFIG_DIR" ]] || \
    fail "${RUNTIME_CONFIG_DIR} must be a real directory, not a symlink"
[[ ! -L "$RUNTIME_CONFIG_PATH" ]] || \
    fail "${RUNTIME_CONFIG_PATH} must be a regular file, not a symlink"
if [[ ! -e "$REPO_CONFIG_PATH" && -f "$RUNTIME_CONFIG_PATH" ]]; then
    ln -s "$RUNTIME_CONFIG_PATH" "$REPO_CONFIG_PATH"
    info "Recovered the repository configuration symlink"
fi
if [[ -L "$REPO_CONFIG_PATH" ]]; then
    CONFIG_PATH="$(readlink -f -- "$REPO_CONFIG_PATH")"
    [[ "$CONFIG_PATH" == "$RUNTIME_CONFIG_PATH" ]] || \
        fail "Repository config symlink must point to ${RUNTIME_CONFIG_PATH}"
fi

mkdir -p /run/lock
exec 9>/run/lock/the-print-farm-setup.lock
flock -n 9 || fail "Another setup process is already running"

SERVICE_WAS_ACTIVE=false
if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
    SERVICE_WAS_ACTIVE=true
fi

echo
echo "=============================================="
echo "  The Print Farm Setup"
echo "=============================================="
echo

if [[ "$SERVICE_ACTION" == "ask" ]]; then
    if [[ -t 0 ]]; then
        if $SERVICE_WAS_ACTIVE; then
            read -r -p "Restart the running farm manager after setup? (y/N): " ANSWER
            [[ "$ANSWER" =~ ^[Yy]$ ]] && SERVICE_ACTION="restart" || SERVICE_ACTION="none"
        else
            read -r -p "Start the farm manager after setup? (Y/n): " ANSWER
            [[ "$ANSWER" =~ ^[Nn]$ ]] && SERVICE_ACTION="none" || SERVICE_ACTION="start"
        fi
    else
        SERVICE_ACTION="none"
        warn "Non-interactive setup will leave the service state unchanged"
    fi
fi
if $SERVICE_WAS_ACTIVE && [[ "$SERVICE_ACTION" == "start" ]]; then
    SERVICE_ACTION="none"
    warn "--start does not restart an active service; dependency updates will be deferred"
fi
if $FORCE_RESTART && [[ "$SERVICE_ACTION" != "restart" ]]; then
    fail "--force-restart must be used together with --restart"
fi
if $SERVICE_WAS_ACTIVE && [[ "$SERVICE_ACTION" == "none" ]]; then
    if [[ ! -L "$REPO_CONFIG_PATH" ||
          "$(readlink -f -- "$REPO_CONFIG_PATH" 2>/dev/null || true)" != "$RUNTIME_CONFIG_PATH" ]]; then
        fail "This upgrade must relocate live configuration; rerun with --restart when printers are idle"
    fi
fi
if $SERVICE_WAS_ACTIVE && [[ "$SERVICE_ACTION" == "restart" ]] && ! $FORCE_RESTART; then
    [[ -x "${SCRIPT_DIR}/venv/bin/python" ]] || \
        fail "Cannot verify print activity because the active service virtual environment is missing"
    info "Checking for active prints before permitting a restart..."
    FARM_CONFIG_PATH="$CONFIG_PATH" FARM_REPO_DIR="$SCRIPT_DIR" \
        "${SCRIPT_DIR}/venv/bin/python" <<'PY'
import json
import os
import sqlite3
import urllib.request

import yaml

config_path = os.environ["FARM_CONFIG_PATH"]
with open(config_path, "r", encoding="utf-8") as handle:
    config = yaml.safe_load(handle) or {}

web = config.get("web") or {}
admin_api_key = str(
    web.get("admin_api_key")
    or web.get("api_key")
    or config.get("api_key")
    or ""
)
if not admin_api_key:
    raise SystemExit(
        "Cannot verify print activity without an administrator API key; use --force-restart "
        "only after checking every printer"
    )

port = int(web.get("port", 5000))
request = urllib.request.Request(
    f"http://127.0.0.1:{port}/api/farm/status",
    headers={"X-Api-Key": admin_api_key},
)
try:
    with urllib.request.urlopen(request, timeout=5) as response:
        payload = json.load(response)
except Exception as exc:
    raise SystemExit(
        "Cannot verify live printer state; use --force-restart only after "
        f"checking every printer ({exc})"
    ) from exc

states = payload.get("printers")
if not isinstance(states, dict):
    raise SystemExit(
        "Cannot verify live printer state because the API response is incomplete; "
        "use --force-restart only after checking every printer"
    )

configured_names = {
    str(printer.get("name", "")).strip()
    for printer in (config.get("printers") or [])
    if isinstance(printer, dict) and str(printer.get("name", "")).strip()
}
missing_names = sorted(configured_names - set(states))
if missing_names:
    raise SystemExit(
        "Restart refused because configured printers are missing from live status: "
        + ", ".join(missing_names)
    )

busy_statuses = {"RUNNING", "PAUSED", "PAUSE_FILAMENT"}
busy = [
    f"{name} ({str(state.get('status', 'UNKNOWN')).upper()})"
    for name, state in states.items()
    if str(state.get("status", "")).upper() in busy_statuses
]
if busy:
    raise SystemExit("Restart refused while print activity exists: " + ", ".join(busy))

safe_statuses = {"IDLE", "FINISH", "FAILED"}
unverified = [
    f"{name} ({'offline' if not state.get('connected') else str(state.get('status', 'UNKNOWN')).upper()})"
    for name, state in states.items()
    if not state.get("connected")
    or str(state.get("status", "")).upper() not in safe_statuses
]
if unverified:
    raise SystemExit(
        "Restart refused because printer state is not confirmed safe: "
        + ", ".join(unverified)
        + ". Use --force-restart only after checking every printer"
    )

db_path = str((config.get("queue") or {}).get("db_path", "./data/farm.db"))
if not os.path.isabs(db_path):
    db_path = os.path.join(os.environ["FARM_REPO_DIR"], db_path)
try:
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
    rows = connection.execute(
        "SELECT id, status FROM jobs "
        "WHERE lower(status) IN ('assigned', 'uploading', 'printing', 'paused')"
    ).fetchall()
    connection.close()
except sqlite3.Error as exc:
    raise SystemExit(
        "Cannot verify the live print queue; use --force-restart only after "
        f"checking every printer ({exc})"
    ) from exc

if rows:
    raise SystemExit(
        "Restart refused while print activity exists: "
        + ", ".join(f"job #{job_id} ({status})" for job_id, status in rows)
    )
PY
    ok "No active print activity detected"
fi

if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    . /etc/os-release
else
    fail "Cannot identify this operating system"
fi
case "${ID:-}" in
    debian|raspbian|ubuntu) info "Detected: ${PRETTY_NAME:-$ID}" ;;
    *) warn "Untested OS: ${PRETTY_NAME:-unknown}; continuing with Debian-compatible commands" ;;
esac

if ! $SKIP_PACKAGES; then
    info "Installing system dependencies..."
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    if ! apt-get install -y -qq \
        python3 python3-venv python3-pip apache2 sudo git \
        libapache2-mod-proxy-html isc-dhcp-client openssl >/dev/null 2>&1; then
        apt-get install -y -qq \
            python3 python3-venv python3-pip apache2 sudo git \
            isc-dhcp-client openssl >/dev/null
    fi
    unset DEBIAN_FRONTEND
    ok "System dependencies installed"
else
    info "Skipping system package installation"
fi

for command in python3 apache2ctl systemctl systemd-analyze git flock runuser visudo; do
    command -v "$command" >/dev/null || fail "Required command not found: ${command}"
done
python3 - <<'PY'
import sys

if sys.version_info < (3, 10):
    raise SystemExit(
        f"Python 3.10 or newer is required; found {sys.version.split()[0]}"
    )
PY

info "Preparing Python environment..."
if [[ ! -x "${SCRIPT_DIR}/venv/bin/python" ]]; then
    $SERVICE_WAS_ACTIVE && fail "Active service has no usable virtual environment"
    python3 -m venv "${SCRIPT_DIR}/venv"
fi
"${SCRIPT_DIR}/venv/bin/python" - <<'PY'
import sys

if sys.version_info < (3, 10):
    raise SystemExit(
        "The existing virtual environment uses Python "
        f"{sys.version.split()[0]}; recreate it with Python 3.10 or newer "
        "while the service is stopped"
    )
PY
if $SERVICE_WAS_ACTIVE && [[ "$SERVICE_ACTION" == "none" ]]; then
    "${SCRIPT_DIR}/venv/bin/pip" check
    warn "Dependency installation deferred because the running service will not restart"
else
    "${SCRIPT_DIR}/venv/bin/python" -m pip install --upgrade pip -q
    "${SCRIPT_DIR}/venv/bin/pip" install -r "${SCRIPT_DIR}/requirements.txt" -q
    "${SCRIPT_DIR}/venv/bin/pip" check
    ok "Python environment ready"
fi

mkdir -p \
    "${SCRIPT_DIR}/config/certs" \
    "${SCRIPT_DIR}/data/dhcp" \
    "${SCRIPT_DIR}/uploads/thumbnails" \
    "${SCRIPT_DIR}/logs"

validate_port() {
    local value=$1
    local label=$2
    [[ "$value" =~ ^[0-9]+$ ]] || fail "${label} must be an integer"
    ((value >= 1 && value <= 65535)) || fail "${label} must be between 1 and 65535"
}

publish_config_link() {
    CONFIG_LINK_DIR="$(mktemp -d "${SCRIPT_DIR}/config/.config-link.XXXXXX")"
    ln -s "$RUNTIME_CONFIG_PATH" "${CONFIG_LINK_DIR}/config.yaml"
    mv -Tf "${CONFIG_LINK_DIR}/config.yaml" "$REPO_CONFIG_PATH"
    rmdir "$CONFIG_LINK_DIR"
    CONFIG_LINK_DIR=""
}

NEW_ORCA_API_KEY=""
NEW_ADMIN_API_KEY=""
if [[ ! -f "$CONFIG_PATH" ]]; then
    [[ -t 0 ]] || fail \
        "Initial configuration is interactive; run setup.sh from a terminal"

    echo
    echo "Initial Configuration"
    echo
    read -r -p "  Admin username [admin]: " ADMIN_USER
    ADMIN_USER="${ADMIN_USER:-admin}"
    [[ "$ADMIN_USER" =~ ^[A-Za-z0-9._-]{1,64}$ ]] || \
        fail "Admin username may contain letters, numbers, dot, underscore, and hyphen"

    while true; do
        read -r -s -p "  Admin password: " ADMIN_PASS
        echo
        if ((${#ADMIN_PASS} < 12)); then
            warn "Password must be at least 12 characters"
            continue
        fi
        read -r -s -p "  Confirm password: " ADMIN_PASS_CONFIRM
        echo
        if [[ "$ADMIN_PASS" != "$ADMIN_PASS_CONFIRM" ]]; then
            warn "Passwords do not match"
            continue
        fi
        break
    done

    read -r -p "  Display name [${ADMIN_USER}]: " ADMIN_DISPLAY
    ADMIN_DISPLAY="${ADMIN_DISPLAY:-$ADMIN_USER}"

    read -r -p "  Enable Active Directory / LDAP? (y/N): " ENABLE_AD
    AD_ENABLED=false
    AD_SERVER=""
    AD_PORT="636"
    AD_BASE_DN=""
    AD_BIND_USER=""
    AD_BIND_PASS=""
    AD_STUDENT_OU=""
    AD_STAFF_OU=""
    AD_CA_FILE=""
    if [[ "$ENABLE_AD" =~ ^[Yy]$ ]]; then
        AD_ENABLED=true
        read -r -p "    AD server IP/hostname: " AD_SERVER
        [[ -n "$AD_SERVER" ]] || fail "AD server is required"
        read -r -p "    LDAPS port [636]: " AD_PORT
        AD_PORT="${AD_PORT:-636}"
        validate_port "$AD_PORT" "LDAPS port"
        read -r -p "    Base DN (e.g. DC=example,DC=local): " AD_BASE_DN
        [[ -n "$AD_BASE_DN" ]] || fail "Base DN is required"
        read -r -p "    Bind user DN: " AD_BIND_USER
        read -r -s -p "    Bind password: " AD_BIND_PASS
        echo
        read -r -p "    Student OU [OU=Students,${AD_BASE_DN}]: " AD_STUDENT_OU
        AD_STUDENT_OU="${AD_STUDENT_OU:-OU=Students,$AD_BASE_DN}"
        read -r -p "    Staff OU [OU=Staff,${AD_BASE_DN}]: " AD_STAFF_OU
        AD_STAFF_OU="${AD_STAFF_OU:-OU=Staff,$AD_BASE_DN}"
        read -r -p "    CA certificate path [system trust store]: " AD_CA_FILE
        if [[ -n "$AD_CA_FILE" && ! -r "$AD_CA_FILE" ]]; then
            fail "CA certificate is not readable: ${AD_CA_FILE}"
        fi
    fi

    read -r -p "  Backend web port [5000]: " WEB_PORT
    WEB_PORT="${WEB_PORT:-5000}"
    validate_port "$WEB_PORT" "Backend web port"
    ((WEB_PORT >= 1024)) || fail "Backend web port must be 1024 or greater"

    NEW_ORCA_API_KEY="$("${SCRIPT_DIR}/venv/bin/python" -c \
        'import secrets; print(secrets.token_urlsafe(24))')"
    NEW_ADMIN_API_KEY="$("${SCRIPT_DIR}/venv/bin/python" -c \
        'import secrets; print(secrets.token_urlsafe(24))')"

    export FARM_SETUP_ADMIN_USER="$ADMIN_USER"
    export FARM_SETUP_ADMIN_PASS="$ADMIN_PASS"
    export FARM_SETUP_ADMIN_DISPLAY="$ADMIN_DISPLAY"
    export FARM_SETUP_ORCA_API_KEY="$NEW_ORCA_API_KEY"
    export FARM_SETUP_ADMIN_API_KEY="$NEW_ADMIN_API_KEY"
    export FARM_SETUP_WEB_PORT="$WEB_PORT"
    export FARM_SETUP_AD_ENABLED="$AD_ENABLED"
    export FARM_SETUP_AD_SERVER="$AD_SERVER"
    export FARM_SETUP_AD_PORT="$AD_PORT"
    export FARM_SETUP_AD_BASE_DN="$AD_BASE_DN"
    export FARM_SETUP_AD_BIND_USER="$AD_BIND_USER"
    export FARM_SETUP_AD_BIND_PASS="$AD_BIND_PASS"
    export FARM_SETUP_AD_STUDENT_OU="$AD_STUDENT_OU"
    export FARM_SETUP_AD_STAFF_OU="$AD_STAFF_OU"
    export FARM_SETUP_AD_CA_FILE="$AD_CA_FILE"

    FARM_CONFIG_PATH="$CONFIG_PATH" "${SCRIPT_DIR}/venv/bin/python" <<'PY'
import os
from werkzeug.security import generate_password_hash
from src.config_store import save_config

ad_enabled = os.environ["FARM_SETUP_AD_ENABLED"] == "true"
config = {
    "printers": [],
    "web": {
        "host": "127.0.0.1",
        "port": int(os.environ["FARM_SETUP_WEB_PORT"]),
        "orca_api_key": os.environ["FARM_SETUP_ORCA_API_KEY"],
        "admin_api_key": os.environ["FARM_SETUP_ADMIN_API_KEY"],
        "max_upload_mb": 1024,
        "session_cookie_secure": False,
    },
    "queue": {
        "upload_dir": "./uploads",
        "db_path": "./data/farm.db",
        "auto_assign": False,
    },
    "local_users": [{
        "username": os.environ["FARM_SETUP_ADMIN_USER"],
        "password_hash": generate_password_hash(os.environ["FARM_SETUP_ADMIN_PASS"]),
        "role": "staff",
        "display_name": os.environ["FARM_SETUP_ADMIN_DISPLAY"],
    }],
    "active_directory": {"enabled": ad_enabled},
    "logging": {"level": "INFO"},
}
if ad_enabled:
    ad = config["active_directory"]
    ad.update({
        "server": os.environ["FARM_SETUP_AD_SERVER"],
        "port": int(os.environ["FARM_SETUP_AD_PORT"]),
        "use_ssl": True,
        "tls_validate": True,
        "base_dn": os.environ["FARM_SETUP_AD_BASE_DN"],
        "bind_user": os.environ["FARM_SETUP_AD_BIND_USER"],
        "bind_password": os.environ["FARM_SETUP_AD_BIND_PASS"],
        "student_ou": os.environ["FARM_SETUP_AD_STUDENT_OU"],
        "staff_ou": os.environ["FARM_SETUP_AD_STAFF_OU"],
    })
    if os.environ["FARM_SETUP_AD_CA_FILE"]:
        ad["ca_certs_file"] = os.environ["FARM_SETUP_AD_CA_FILE"]
save_config(os.environ["FARM_CONFIG_PATH"], config)
PY

    unset \
        ADMIN_PASS ADMIN_PASS_CONFIRM AD_BIND_PASS \
        FARM_SETUP_ADMIN_USER FARM_SETUP_ADMIN_PASS FARM_SETUP_ADMIN_DISPLAY \
        FARM_SETUP_ORCA_API_KEY FARM_SETUP_ADMIN_API_KEY \
        FARM_SETUP_WEB_PORT FARM_SETUP_AD_ENABLED \
        FARM_SETUP_AD_SERVER FARM_SETUP_AD_PORT FARM_SETUP_AD_BASE_DN \
        FARM_SETUP_AD_BIND_USER FARM_SETUP_AD_BIND_PASS FARM_SETUP_AD_STUDENT_OU \
        FARM_SETUP_AD_STAFF_OU FARM_SETUP_AD_CA_FILE
    ok "Created private configuration"
else
    info "Validating and migrating existing configuration..."
    FARM_CONFIG_PATH="$CONFIG_PATH" "${SCRIPT_DIR}/venv/bin/python" <<'PY'
import copy
import os
import re
import secrets
import shutil
import time
from werkzeug.security import generate_password_hash
from src.config_store import load_config, migrate_api_keys, save_config

path = os.environ["FARM_CONFIG_PATH"]
config = load_config(path)
if not isinstance(config, dict):
    raise SystemExit("Configuration root must be a YAML mapping")
original = copy.deepcopy(config)

web = config.setdefault("web", {})
port = int(web.get("port", 5000))
if not 1024 <= port <= 65535:
    raise SystemExit("web.port must be between 1024 and 65535")
web["port"] = port
host = str(web.get("host") or "127.0.0.1").strip()
allow_remote = bool(web.get("allow_remote_backend", False))
if not allow_remote:
    web["host"] = "127.0.0.1"
elif host not in ("127.0.0.1", "localhost", "0.0.0.0"):
    raise SystemExit(
        "web.host must be 0.0.0.0 when remote backend access is enabled; "
        "otherwise remove web.allow_remote_backend"
    )
else:
    web["host"] = host
max_upload_mb = int(web.get("max_upload_mb", 1024))
if not 1 <= max_upload_mb <= 4096:
    raise SystemExit("web.max_upload_mb must be between 1 and 4096")
web["max_upload_mb"] = max_upload_mb
web.setdefault("session_cookie_secure", False)
try:
    migrate_api_keys(config)
except ValueError as exc:
    raise SystemExit(str(exc)) from exc

queue = config.setdefault("queue", {})
queue.setdefault("upload_dir", "./uploads")
queue.setdefault("db_path", "./data/farm.db")
queue.setdefault("auto_assign", False)

# Build-plate camera detection was removed in v1.0.11. Drop its retired
# configuration during upgrades so existing installations converge on the
# current example configuration.
config.pop("plate_detection", None)
notification_events = (config.get("notifications") or {}).get("events")
if isinstance(notification_events, dict):
    notification_events.pop("plate_blocked", None)

printers = config.get("printers") or []
if not isinstance(printers, list):
    raise SystemExit("printers must be a YAML list")
printer_names = set()
orca_ports = {port}
for printer in printers:
    if not isinstance(printer, dict):
        raise SystemExit("Every printer entry must be a mapping")
    name = str(printer.get("name", "")).strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._()-]{0,63}", name):
        raise SystemExit(f"Invalid printer name: {name!r}")
    if name in printer_names:
        raise SystemExit(f"Duplicate printer name: {name}")
    printer_names.add(name)
    printer["name"] = name
    if printer.get("orca_port") is not None:
        orca_port = int(printer["orca_port"])
        if not 1024 <= orca_port <= 65535 or orca_port in orca_ports:
            raise SystemExit(f"Invalid or duplicate Orca port for {name}: {orca_port}")
        orca_ports.add(orca_port)
        printer["orca_port"] = orca_port

ad = config.get("active_directory") or {}
if not isinstance(ad, dict):
    raise SystemExit("active_directory must be a YAML mapping")
if ad.get("enabled"):
    if not str(ad.get("server") or "").strip():
        raise SystemExit("active_directory.server is required when AD is enabled")
    use_ssl = bool(ad.get("use_ssl", True))
    if not use_ssl and not ad.get("allow_insecure", False):
        raise SystemExit(
            "Plaintext LDAP is disabled; configure LDAPS or explicitly set "
            "active_directory.allow_insecure"
        )
    ad_port = int(ad.get("port", 636 if use_ssl else 389))
    if not 1 <= ad_port <= 65535:
        raise SystemExit("active_directory.port must be between 1 and 65535")
    ad["use_ssl"] = use_ssl
    ad["port"] = ad_port
    if use_ssl:
        ad.setdefault("tls_validate", True)
    if ad.get("tls_ca_file") and not ad.get("ca_certs_file"):
        ad["ca_certs_file"] = ad.pop("tls_ca_file")
    config["active_directory"] = ad

local_users = config.get("local_users") or []
if not isinstance(local_users, list):
    raise SystemExit("local_users must be a YAML list")
for user in local_users:
    if not isinstance(user, dict):
        raise SystemExit("Every local_users entry must be a mapping")
    if "password" in user:
        user["password_hash"] = generate_password_hash(str(user.pop("password")))
if "admin_password" in web:
    web["admin_password_hash"] = generate_password_hash(str(web.pop("admin_password")))

if config != original:
    backup = f"{path}.pre-setup-{time.time_ns()}.bak"
    shutil.copy2(path, backup)
    os.chmod(backup, 0o600)
    save_config(path, config)
    print(f"  Migrated configuration; backup: {backup}")
else:
    os.chmod(path, 0o600)
PY
    ok "Existing configuration is valid"
fi

WEB_PORT="$("${SCRIPT_DIR}/venv/bin/python" - <<PY
from src.config_store import load_config
config = load_config(${CONFIG_PATH@Q})
print(int(config.get("web", {}).get("port", 5000)))
PY
)"
validate_port "$WEB_PORT" "Configured backend web port"

SERVICE_USER="print-farm"
SERVICE_GROUP="print-farm"
if ! getent group "$SERVICE_GROUP" >/dev/null; then
    groupadd --system "$SERVICE_GROUP"
fi
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --gid "$SERVICE_GROUP" --home-dir /nonexistent \
        --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
fi
# shellcheck disable=SC2016
if ! runuser -u "$SERVICE_USER" -- \
    sh -c 'cd "$1" && test -r requirements.txt' sh "$SCRIPT_DIR" >/dev/null 2>&1; then
    warn "Dedicated service user cannot access ${SCRIPT_DIR}; using root for this installation"
    SERVICE_USER="root"
    SERVICE_GROUP="root"
fi

info "Applying scoped ownership and permissions..."
install -d -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 0700 "$RUNTIME_CONFIG_DIR"
if [[ -L "$REPO_CONFIG_PATH" ]]; then
    CURRENT_TARGET="$(readlink -f -- "$REPO_CONFIG_PATH")"
    [[ "$CURRENT_TARGET" == "$RUNTIME_CONFIG_PATH" ]] || \
        fail "Repository config symlink points to an unexpected path: ${CURRENT_TARGET}"
    [[ -f "$RUNTIME_CONFIG_PATH" ]] || fail "Runtime configuration is missing: ${RUNTIME_CONFIG_PATH}"
elif [[ ! -e "$RUNTIME_CONFIG_PATH" ]]; then
    install -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 0600 \
        "$REPO_CONFIG_PATH" "$RUNTIME_CONFIG_PATH"
    publish_config_link
elif [[ ! -e "$REPO_CONFIG_PATH" ]]; then
    ln -s "$RUNTIME_CONFIG_PATH" "$REPO_CONFIG_PATH"
else
    cmp -s "$REPO_CONFIG_PATH" "$RUNTIME_CONFIG_PATH" || \
        fail "Both repository and runtime configs exist and differ; reconcile them manually"
    publish_config_link
fi
CONFIG_PATH="$RUNTIME_CONFIG_PATH"

chown -R --no-dereference root:root "$SCRIPT_DIR"
find "$SCRIPT_DIR" \
    \( -path "${SCRIPT_DIR}/config" -o -path "${SCRIPT_DIR}/data" \
       -o -path "${SCRIPT_DIR}/uploads" -o -path "${SCRIPT_DIR}/logs" \
       -o -path "${SCRIPT_DIR}/.git" \) -prune -o \
    -type d -exec chmod 0755 {} +
find "$SCRIPT_DIR" \
    \( -path "${SCRIPT_DIR}/config" -o -path "${SCRIPT_DIR}/data" \
       -o -path "${SCRIPT_DIR}/uploads" -o -path "${SCRIPT_DIR}/logs" \
       -o -path "${SCRIPT_DIR}/.git" \) -prune -o \
    -type f -exec chmod a+r,go-w {} +
find "${SCRIPT_DIR}/venv/bin" "${SCRIPT_DIR}/scripts" \
    -type f -perm /0111 -exec chmod a+rx,go-w {} +
if [[ -d "${SCRIPT_DIR}/.git" ]]; then
    find "${SCRIPT_DIR}/.git" -type d -exec chmod 0700 {} +
    find "${SCRIPT_DIR}/.git" -type f -exec chmod 0600 {} +
fi
chmod 0755 "$SCRIPT_DIR"
chmod 0755 "${SCRIPT_DIR}/config"
for directory in data uploads logs; do
    chown -R --no-dereference "${SERVICE_USER}:${SERVICE_GROUP}" "${SCRIPT_DIR}/${directory}"
    find "${SCRIPT_DIR}/${directory}" -type d -exec chmod 0700 {} +
done
chown -R --no-dereference "${SERVICE_USER}:${SERVICE_GROUP}" "${SCRIPT_DIR}/config/certs"
find "${SCRIPT_DIR}/config/certs" -type d -exec chmod 0700 {} +
find "${SCRIPT_DIR}/config/certs" -type f -exec chmod 0600 {} +
find "${SCRIPT_DIR}/data" "${SCRIPT_DIR}/uploads" "${SCRIPT_DIR}/logs" \
    -type f -exec chmod 0600 {} +
chown "${SERVICE_USER}:${SERVICE_GROUP}" "$RUNTIME_CONFIG_DIR" "$RUNTIME_CONFIG_PATH"
chmod 0700 "$RUNTIME_CONFIG_DIR"
chmod 0600 "$CONFIG_PATH"
ok "Code is root-owned; runtime directories are writable by ${SERVICE_USER}"

info "Verifying service-user access to configured runtime paths..."
runuser -u "$SERVICE_USER" -- env \
    FARM_CONFIG_PATH="$CONFIG_PATH" \
    FARM_REPO_DIR="$SCRIPT_DIR" \
    "${SCRIPT_DIR}/venv/bin/python" <<'PY'
import os

import yaml

with open(os.environ["FARM_CONFIG_PATH"], "r", encoding="utf-8") as handle:
    config = yaml.safe_load(handle) or {}

repo = os.environ["FARM_REPO_DIR"]
queue = config.get("queue") or {}
logging_config = config.get("logging") or {}


def absolute(path):
    path = os.path.expanduser(str(path))
    return path if os.path.isabs(path) else os.path.abspath(os.path.join(repo, path))


upload_dir = absolute(queue.get("upload_dir", "./uploads"))
db_path = absolute(queue.get("db_path", "./data/farm.db"))
log_path = absolute(logging_config.get("file", "./logs/farm.log"))

for label, directory in (
    ("queue.upload_dir", upload_dir),
    ("queue.db_path parent", os.path.dirname(db_path)),
    ("logging.file parent", os.path.dirname(log_path)),
):
    if not os.path.isdir(directory):
        raise SystemExit(f"{label} directory does not exist: {directory}")
    if not os.access(directory, os.W_OK | os.X_OK):
        raise SystemExit(f"{label} directory is not writable by the service user: {directory}")

if os.path.exists(db_path) and not os.access(db_path, os.W_OK):
    raise SystemExit(f"queue.db_path is not writable by the service user: {db_path}")
PY
ok "Configured runtime paths are writable by ${SERVICE_USER}"

info "Selecting the Apache vhost..."
a2enmod proxy proxy_http proxy_wstunnel rewrite headers >/dev/null
APACHE_CONF="${FARM_APACHE_CONF:-}"
if [[ -n "$APACHE_CONF" ]]; then
    [[ -f "$APACHE_CONF" ]] || fail "FARM_APACHE_CONF is not a file: ${APACHE_CONF}"
else
    mapfile -t MARKED_VHOSTS < <(
        grep -l -E 'the-print-farm proxy|BEGIN THE PRINT FARM MANAGED PROXY' \
            /etc/apache2/sites-enabled/*.conf 2>/dev/null || true
    )
    if ((${#MARKED_VHOSTS[@]} == 1)); then
        APACHE_CONF="${MARKED_VHOSTS[0]}"
    elif ((${#MARKED_VHOSTS[@]} > 1)); then
        fail "Multiple Apache vhosts contain print-farm proxy blocks; set FARM_APACHE_CONF"
    elif [[ -f /etc/apache2/sites-enabled/000-default.conf ]]; then
        APACHE_CONF="/etc/apache2/sites-enabled/000-default.conf"
    else
        mapfile -t ACTIVE_VHOSTS < <(
            find -L /etc/apache2/sites-enabled -maxdepth 1 -type f -name '*.conf' -print
        )
        if ((${#ACTIVE_VHOSTS[@]} != 1)); then
            fail "Cannot choose an Apache vhost safely; set FARM_APACHE_CONF"
        fi
        APACHE_CONF="${ACTIVE_VHOSTS[0]}"
    fi
fi
APACHE_CONF="$(readlink -f -- "$APACHE_CONF")"
[[ "$APACHE_CONF" == /etc/apache2/* ]] || fail "Apache vhost must be under /etc/apache2"
ok "Using Apache vhost: ${APACHE_CONF}"

info "Installing the privileged deployment helper..."
HELPER_TEMP="$(mktemp /usr/local/sbin/.the-print-farm-helper.XXXXXX)"
install -o root -g root -m 0755 "$HELPER_SOURCE" "$HELPER_TEMP"
mv -f "$HELPER_TEMP" "$HELPER_PATH"
HELPER_TEMP=""
HELPER_REPO="$SCRIPT_DIR" \
HELPER_WEB_PORT="$WEB_PORT" \
HELPER_APACHE_VHOST="$APACHE_CONF" \
HELPER_CONFIG_PATH="$HELPER_CONFIG" \
python3 <<'PY'
import json
import os
import tempfile

path = os.environ["HELPER_CONFIG_PATH"]
config = {
    "repo_dir": os.environ["HELPER_REPO"],
    "service_name": "the-print-farm",
    "web_port": int(os.environ["HELPER_WEB_PORT"]),
    "apache_vhost": os.environ["HELPER_APACHE_VHOST"],
}
fd, temporary = tempfile.mkstemp(prefix=".the-print-farm-helper.", dir="/etc")
try:
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    os.chmod(path, 0o600)
except Exception:
    try:
        os.unlink(temporary)
    except OSError:
        pass
    raise
PY

if [[ "$SERVICE_USER" != "root" ]]; then
    SUDOERS_TEMP="$(mktemp /etc/sudoers.d/.the-print-farm.XXXXXX)"
    cat >"$SUDOERS_TEMP" <<EOF
# The helper validates every operation and uses only root-owned configuration.
${SERVICE_USER} ALL=(root) NOPASSWD: ${HELPER_PATH}
EOF
    chmod 0440 "$SUDOERS_TEMP"
    visudo -cf "$SUDOERS_TEMP" >/dev/null
    mv -f "$SUDOERS_TEMP" "$SUDOERS_PATH"
    SUDOERS_TEMP=""
else
    rm -f "$SUDOERS_PATH"
fi
ok "Privileged helper installed"

info "Installing the systemd unit..."
UNIT_TEMP_DIR="$(mktemp -d /etc/systemd/system/.the-print-farm-setup.XXXXXX)"
UNIT_TEMP="${UNIT_TEMP_DIR}/${SERVICE_NAME}.service"
cat >"$UNIT_TEMP" <<EOF
[Unit]
Description=The Print Farm Manager
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_GROUP}
WorkingDirectory=${SCRIPT_DIR}
ExecStart="${SCRIPT_DIR}/venv/bin/python" -u -m src.main
Environment="PYTHONUNBUFFERED=1"
Environment="PYTHONDONTWRITEBYTECODE=1"
Environment="FARM_CONFIG=${CONFIG_PATH}"
Environment="FARM_PRIVILEGED_HELPER=${HELPER_PATH}"
Restart=on-failure
RestartSec=10
UMask=0077
AmbientCapabilities=CAP_NET_ADMIN CAP_NET_RAW CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_RAW CAP_NET_BIND_SERVICE
PrivateTmp=true
ProtectClock=true
ProtectControlGroups=true
ProtectKernelModules=true
ProtectKernelTunables=true
RestrictRealtime=true
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
chmod 0644 "$UNIT_TEMP"
systemd-analyze verify "$UNIT_TEMP"
mv -f "$UNIT_TEMP" "$SERVICE_PATH"
UNIT_TEMP=""
rmdir "$UNIT_TEMP_DIR"
UNIT_TEMP_DIR=""
systemctl daemon-reload
systemctl enable "$SERVICE_NAME" >/dev/null
ok "Systemd unit installed for ${SERVICE_USER}:${SERVICE_GROUP}"

info "Reconciling Apache proxy configuration..."
"$HELPER_PATH" configure-proxy

SYNC_DIR="/run/the-print-farm"
install -d -o root -g root -m 0700 "$SYNC_DIR"
SYNC_FILE="$(mktemp "${SYNC_DIR}/orca-sync.XXXXXX.json")"
FARM_CONFIG_PATH="$CONFIG_PATH" FARM_SYNC_PATH="$SYNC_FILE" \
    "${SCRIPT_DIR}/venv/bin/python" <<'PY'
import json
import os
import re
import socket
import subprocess
from src.config_store import load_config, save_config

config_path = os.environ["FARM_CONFIG_PATH"]
config = load_config(config_path)
printers = config.get("printers") or []
if not isinstance(printers, list):
    raise SystemExit("printers must be a YAML list")

used = {int(config.get("web", {}).get("port", 5000))}
for printer in printers:
    if not isinstance(printer, dict):
        raise SystemExit("Every printer entry must be a mapping")
    name = str(printer.get("name", "")).strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._()-]{0,63}", name):
        raise SystemExit(f"Invalid printer name: {name!r}")
    printer["name"] = name
    port = printer.get("orca_port")
    if port is not None:
        port = int(port)
        if not 1024 <= port <= 65535 or port in used:
            raise SystemExit(f"Invalid or duplicate Orca port for {name}: {port}")
        printer["orca_port"] = port
        used.add(port)

listening = set()
try:
    output = subprocess.run(
        ["ss", "-H", "-ltn"], capture_output=True, text=True, timeout=5, check=True
    ).stdout
    for match in re.finditer(r":(\d+)\s", output):
        listening.add(int(match.group(1)))
except (OSError, subprocess.SubprocessError):
    pass

changed = False
for printer in printers:
    if not printer.get("orca_port"):
        port = 5001
        while port in used or port in listening:
            port += 1
        if port > 65535:
            raise SystemExit("No free Orca port is available")
        printer["orca_port"] = port
        used.add(port)
        changed = True

if changed:
    save_config(config_path, config)

payload = [
    {"name": printer["name"], "port": int(printer["orca_port"])}
    for printer in printers
    if printer.get("orca_port")
]
with open(os.environ["FARM_SYNC_PATH"], "w", encoding="utf-8") as handle:
    json.dump(payload, handle)
    handle.flush()
    os.fsync(handle.fileno())
os.chmod(os.environ["FARM_SYNC_PATH"], 0o600)
PY
"$HELPER_PATH" orca-sync "$SYNC_FILE"
SYNC_FILE=""
ok "Apache configuration validated and gracefully reloaded"

perform_service_action() {
    local action=$1
    case "$action" in
        none)
            info "Leaving ${SERVICE_NAME} service state unchanged"
            ;;
        start)
            if $SERVICE_WAS_ACTIVE; then
                info "Service is already active; leaving the running process unchanged"
            else
                systemctl start "$SERVICE_NAME"
            fi
            ;;
        restart)
            systemctl restart "$SERVICE_NAME"
            ;;
    esac
}

perform_service_action "$SERVICE_ACTION"

if [[ "$SERVICE_ACTION" != "none" ]]; then
    info "Waiting for the backend health check..."
    FARM_HEALTH_PORT="$WEB_PORT" python3 <<'PY'
import os
import socket
import subprocess
import time

port = int(os.environ["FARM_HEALTH_PORT"])
deadline = time.monotonic() + 45
last_error = "backend did not accept a connection"
while time.monotonic() < deadline:
    active = subprocess.run(
        ["systemctl", "is-active", "--quiet", "the-print-farm"],
        check=False,
    ).returncode == 0
    if active:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                break
        except OSError as exc:
            last_error = str(exc)
    time.sleep(1)
else:
    raise SystemExit(f"Service health check failed: {last_error}")
PY
    ok "Service is active"
elif $SERVICE_WAS_ACTIVE; then
    warn "The service is still running its previous process; restart it when printers are idle"
fi

LOCAL_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
LOCAL_IP="${LOCAL_IP:-$(hostname -f 2>/dev/null || hostname)}"

echo
echo "=============================================="
printf "  %bSetup Complete%b\n" "$GREEN" "$NC"
echo "=============================================="
echo
echo "  Dashboard: http://${LOCAL_IP}/the-print-farm/"
if [[ -n "$NEW_ORCA_API_KEY" ]]; then
    echo "  Orca upload key: ${NEW_ORCA_API_KEY}"
    echo "  Share this key with users who connect OrcaSlicer to the farm."
fi
echo
echo "  Service commands:"
echo "    sudo systemctl restart ${SERVICE_NAME}"
echo "    sudo systemctl status ${SERVICE_NAME}"
echo "    journalctl -u ${SERVICE_NAME} -f"
echo
