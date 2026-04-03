#!/bin/bash
# ============================================================
# The Print Farm — Setup Script
# ============================================================
# Supports: Debian 11+, Ubuntu 22.04+, Raspberry Pi OS
# Printers: BambuLab (MQTT/FTPS) and Klipper (Moonraker)
# Run as root or with sudo.
# ============================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# ── Colours ──────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'

info()  { echo -e "${CYAN}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
fail()  { echo -e "${RED}[FAIL]${NC}  $*"; exit 1; }

# ── Root check ───────────────────────────────────────────
if [ "$EUID" -ne 0 ]; then
    fail "Please run as root:  sudo bash setup.sh"
fi

echo ""
echo "=============================================="
echo "  The Print Farm — Setup"
echo "  BambuLab & Klipper Printers"
echo "=============================================="
echo ""

# ── Detect OS ────────────────────────────────────────────
if [ -f /etc/os-release ]; then
    . /etc/os-release
    OS_NAME="$ID"
    OS_VERSION="$VERSION_ID"
else
    fail "Cannot detect OS. This script supports Debian, Ubuntu, and Raspberry Pi OS."
fi

case "$OS_NAME" in
    debian|raspbian) info "Detected: $PRETTY_NAME" ;;
    ubuntu)          info "Detected: $PRETTY_NAME" ;;
    *)               warn "Untested OS: $PRETTY_NAME — proceeding anyway" ;;
esac

# ── System packages ──────────────────────────────────────
info "Installing system dependencies..."
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip apache2 libapache2-mod-proxy-html > /dev/null 2>&1 || \
    apt-get install -y -qq python3 python3-venv python3-pip apache2 > /dev/null 2>&1
ok "System packages installed"

# ── Project directories ──────────────────────────────────
info "Creating directories..."
mkdir -p data uploads uploads/thumbnails logs config
ok "Directories ready"

# ── Python virtual environment ───────────────────────────
if [ ! -d "venv" ]; then
    info "Creating Python virtual environment..."
    python3 -m venv venv
fi

info "Installing Python dependencies..."
./venv/bin/pip install --upgrade pip -q 2>/dev/null
./venv/bin/pip install -r requirements.txt -q 2>/dev/null
ok "Python dependencies installed"

# ── Configuration ────────────────────────────────────────
if [ ! -f "config/config.yaml" ]; then
    echo ""
    echo "─── Initial Configuration ───"
    echo ""

    # Generate a random API key
    API_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(16))")

    # Admin user setup
    echo "Create a local admin account for the dashboard."
    echo ""
    read -rp "  Admin username [admin]: " ADMIN_USER
    ADMIN_USER="${ADMIN_USER:-admin}"

    while true; do
        read -rsp "  Admin password: " ADMIN_PASS; echo
        if [ ${#ADMIN_PASS} -lt 4 ]; then
            warn "Password must be at least 4 characters"
            continue
        fi
        read -rsp "  Confirm password: " ADMIN_PASS2; echo
        if [ "$ADMIN_PASS" != "$ADMIN_PASS2" ]; then
            warn "Passwords do not match"
            continue
        fi
        break
    done

    ADMIN_DISPLAY="$ADMIN_USER"
    read -rp "  Display name [$ADMIN_USER]: " ADMIN_DISPLAY_IN
    ADMIN_DISPLAY="${ADMIN_DISPLAY_IN:-$ADMIN_DISPLAY}"

    # Active Directory
    echo ""
    read -rp "  Enable Active Directory / LDAP? (y/N): " ENABLE_AD
    AD_ENABLED=false
    AD_BLOCK=""
    if [[ "$ENABLE_AD" =~ ^[Yy] ]]; then
        AD_ENABLED=true
        read -rp "    AD server IP/hostname: " AD_SERVER
        read -rp "    AD port [389]: " AD_PORT
        AD_PORT="${AD_PORT:-389}"
        read -rp "    Base DN (e.g. DC=example,DC=local): " AD_BASE_DN
        read -rp "    Bind user DN: " AD_BIND_USER
        read -rsp "    Bind password: " AD_BIND_PASS; echo
        read -rp "    Student OU (e.g. OU=Students,$AD_BASE_DN): " AD_STUDENT_OU
        AD_STUDENT_OU="${AD_STUDENT_OU:-OU=Students,$AD_BASE_DN}"
        read -rp "    Staff OU (e.g. OU=Staff,$AD_BASE_DN): " AD_STAFF_OU
        AD_STAFF_OU="${AD_STAFF_OU:-OU=Staff,$AD_BASE_DN}"
    fi

    # Web port
    read -rp "  Web port [5000]: " WEB_PORT
    WEB_PORT="${WEB_PORT:-5000}"

    # Write config
    cat > config/config.yaml <<CFGEOF
printers: []

web:
  host: 0.0.0.0
  port: ${WEB_PORT}
  api_key: '${API_KEY}'

queue:
  upload_dir: ./uploads
  db_path: ./data/farm.db
  auto_assign: false

local_users:
  - username: '${ADMIN_USER}'
    password: '${ADMIN_PASS}'
    role: staff
    display_name: '${ADMIN_DISPLAY}'

active_directory:
  enabled: ${AD_ENABLED}
CFGEOF

    if [ "$AD_ENABLED" = "true" ]; then
        cat >> config/config.yaml <<ADEOF
  server: '${AD_SERVER}'
  port: ${AD_PORT}
  use_ssl: false
  base_dn: '${AD_BASE_DN}'
  bind_user: '${AD_BIND_USER}'
  bind_password: '${AD_BIND_PASS}'
  student_ou: '${AD_STUDENT_OU}'
  staff_ou: '${AD_STAFF_OU}'
ADEOF
    fi

    cat >> config/config.yaml <<LOGEOF

# ── Spoolman (optional) ──────────────────────────────────
# Set the URL of your Spoolman instance for filament tracking.
# Leave commented out to disable Spoolman integration.
# Can also be configured from the dashboard Settings tab.
#spoolman:
#  url: http://localhost:7912

logging:
  level: INFO
LOGEOF

    ok "Configuration written to config/config.yaml"
    echo ""
    echo "  API key: $API_KEY"
    echo "  Admin login: $ADMIN_USER"
    echo ""
else
    ok "config/config.yaml already exists — skipping configuration"
fi

# ── Determine service user ────────────────────────────────
# www-data cannot chdir into restricted dirs like /root, so
# detect whether the install path is accessible and fall back
# to root when it is not.
SVC_USER="www-data"
SVC_GROUP="www-data"
if ! su -s /bin/sh www-data -c "cd '${SCRIPT_DIR}' 2>/dev/null" 2>/dev/null; then
    PARENT_DIR=$(dirname "$SCRIPT_DIR")
    if ! su -s /bin/sh www-data -c "test -x '${PARENT_DIR}'" 2>/dev/null; then
        warn "www-data cannot access ${SCRIPT_DIR} — service will run as root"
        SVC_USER="root"
        SVC_GROUP="root"
    fi
fi

# ── Systemd service ──────────────────────────────────────
info "Installing systemd service..."

cat > /etc/systemd/system/bambulab-farm.service <<SVCEOF
[Unit]
Description=The Print Farm Manager
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SVC_USER}
Group=${SVC_GROUP}
WorkingDirectory=${SCRIPT_DIR}
ExecStart=${SCRIPT_DIR}/venv/bin/python -m src.main
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SVCEOF

systemctl daemon-reload
systemctl enable bambulab-farm > /dev/null 2>&1
ok "Service installed (bambulab-farm.service)"

# ── Apache reverse proxy ─────────────────────────────────
info "Configuring Apache reverse proxy..."
a2enmod proxy proxy_http proxy_wstunnel > /dev/null 2>&1 || true

# Determine the web port from config
WEB_PORT=$(python3 -c "
import yaml
try:
    c = yaml.safe_load(open('${SCRIPT_DIR}/config/config.yaml'))
    print(c.get('web',{}).get('port', 5000))
except: print(5000)
" 2>/dev/null)

# Find active Apache vhost config
APACHE_CONF=""
for f in /etc/apache2/sites-enabled/*.conf; do
    [ -f "$f" ] && APACHE_CONF="$f" && break
done

if [ -z "$APACHE_CONF" ]; then
    APACHE_CONF="/etc/apache2/sites-enabled/000-default.conf"
    warn "No Apache vhost found — creating default"
    cat > "$APACHE_CONF" <<VHEOF
<VirtualHost *:80>
    ServerAdmin webmaster@localhost
    DocumentRoot /var/www/html
</VirtualHost>
VHEOF
fi

if ! grep -q "bambulab-farm proxy" "$APACHE_CONF" 2>/dev/null; then
    sed -i '/<\/VirtualHost>/i \
\t# bambulab-farm proxy\
\tProxyPreserveHost On\
\tProxyPass /bambulab-farm http://127.0.0.1:'"${WEB_PORT}"'\
\tProxyPassReverse /bambulab-farm http://127.0.0.1:'"${WEB_PORT}"'' "$APACHE_CONF"
    ok "Apache proxy configured at /bambulab-farm"
else
    ok "Apache proxy already configured"
fi

systemctl restart apache2 2>/dev/null || warn "Could not restart Apache — start it manually"

# ── Permissions ──────────────────────────────────────────
info "Setting permissions..."
chown -R "${SVC_USER}:${SVC_GROUP}" "$SCRIPT_DIR"
ok "Ownership set to ${SVC_USER}"

# ── Start service ────────────────────────────────────────
echo ""
read -rp "Start the farm manager now? (Y/n): " START_NOW
if [[ ! "$START_NOW" =~ ^[Nn] ]]; then
    systemctl start bambulab-farm
    sleep 2
    if systemctl is-active --quiet bambulab-farm; then
        ok "Service is running"
    else
        warn "Service may have failed — check: journalctl -u bambulab-farm -n 30"
    fi
fi

# ── Done ─────────────────────────────────────────────────
HOSTNAME=$(hostname -f 2>/dev/null || hostname)
LOCAL_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
LOCAL_IP="${LOCAL_IP:-$HOSTNAME}"
echo ""
echo "=============================================="
echo -e "  ${GREEN}Setup Complete${NC}"
echo "=============================================="
echo ""
echo "  Dashboard:  http://${LOCAL_IP}:${WEB_PORT}/"
echo ""
echo "  Commands:"
echo "    sudo systemctl start bambulab-farm"
echo "    sudo systemctl stop bambulab-farm"
echo "    sudo systemctl restart bambulab-farm"
echo "    sudo systemctl status bambulab-farm"
echo "    journalctl -u bambulab-farm -f"
echo ""
echo "  Next steps:"
echo "    1. Log in with your admin account"
echo "    2. Add printers from the Settings tab (or edit config/config.yaml)"
echo "    3. Set up OrcaSlicer upload script from the OrcaSlicer Setup tab"
echo ""
