# Getting Started

## Quick Start

```bash
git clone https://github.com/barnard704344/the-print-farm.git
cd the-print-farm
sudo bash setup.sh
```

The setup script will:

1. Install Python 3, pip, Git, Apache, `isc-dhcp-client`, and `openssl`
2. Create a virtual environment and install dependencies
3. Create an admin user account
4. Create a dedicated `print-farm` service account when the install path permits it
5. Move the private runtime configuration to `/etc/the-print-farm/config.yaml`
6. Install a narrow root-owned helper for updates and Apache reconciliation
7. Configure the systemd service, Apache reverse proxy, and OrcaSlicer vhosts
8. Validate Apache and the application health before reporting success

The repository's `config/config.yaml` is a symlink to the private runtime
configuration after setup. Installations below a non-traversable directory such
as `/root` run the service as root and print an explicit warning; use a path such
as `/opt/the-print-farm` to use the dedicated account.

For controlled upgrades:

```bash
sudo bash setup.sh --restart
sudo bash setup.sh --no-restart
```

`--restart` is required for the first hardened upgrade because configuration is
relocated. On later runs, `--no-restart` leaves an active farm process and its
Python environment untouched. Add `--skip-packages` when system dependencies
are already present. A restart is refused when live printer or queue activity is
detected. `--force-restart` is reserved for recovery after checking every
printer manually.

Virtual printers (for OrcaSlicer LAN mode / AMS sync) start automatically — no additional configuration is needed. See [Printers and OrcaSlicer](printers-and-orcaslicer.md#virtual-printer--lan-mode-orcaslicer-ams-sync) for details.

Dashboard URL:

- http://your-server-ip/the-print-farm

## Manual Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp config/config.example.yaml config/config.yaml
python -m src.main
```

Edit `config/config.yaml` before production use. Manual installations do not
install the systemd service, Apache proxy, or privileged helper.

## Requirements

- Python 3.10+
- Apache 2 with `mod_proxy` (installed automatically by `setup.sh`)
- Debian 12+ / Ubuntu 22.04+ / Raspberry Pi OS Bookworm
- [Spoolman](https://github.com/Donkie/Spoolman) — optional, for filament tracking
- Happy Hare — optional, for MMU control on Klipper printers
