#!/bin/bash
# ============================================================
# BambuLab Print Farm — Setup Script
# ============================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== BambuLab Print Farm Setup ==="
echo ""

# Create directories
echo "Creating directories..."
mkdir -p data uploads logs config

# Python venv
if [ ! -d "venv" ]; then
    echo "Creating Python virtual environment..."
    python3 -m venv venv
fi

echo "Installing Python dependencies..."
./venv/bin/pip install --upgrade pip -q
./venv/bin/pip install -r requirements.txt -q

# Install systemd service
echo "Installing systemd service..."
sudo cp bambulab-farm.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable bambulab-farm

# Apache reverse proxy
echo "Configuring Apache reverse proxy at /bambulab-farm ..."
sudo a2enmod proxy proxy_http proxy_wstunnel 2>/dev/null || true

APACHE_CONF="/etc/apache2/sites-enabled/000-default.conf"
if ! grep -q "bambulab-farm proxy" "$APACHE_CONF" 2>/dev/null; then
    sudo sed -i '/<\/VirtualHost>/i \
\t# bambulab-farm proxy\
\tProxyPreserveHost On\
\tProxyPass /bambulab-farm http://127.0.0.1:5000\
\tProxyPassReverse /bambulab-farm http://127.0.0.1:5000' "$APACHE_CONF"
fi
sudo systemctl restart apache2

# Ensure www-data owns the project
sudo chown -R www-data:www-data "$SCRIPT_DIR"

echo ""
echo "=== Setup Complete ==="
echo ""
echo "The dashboard will auto-start on boot (even with no printers configured)."
echo ""
echo "Commands:"
echo "  sudo systemctl start bambulab-farm     # start now"
echo "  sudo systemctl stop bambulab-farm      # stop"
echo "  sudo systemctl restart bambulab-farm   # restart"
echo "  sudo systemctl status bambulab-farm    # check status"
echo "  journalctl -u bambulab-farm -f         # follow logs"
echo ""
echo "Next steps:"
echo "  1. Edit config/config.yaml with your printer IPs, access codes, and serials"
echo "  2. Start:  sudo systemctl start bambulab-farm"
echo ""
echo "Dashboard: http://$(hostname -f 2>/dev/null || hostname)/bambulab-farm"
