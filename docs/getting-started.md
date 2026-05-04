# Getting Started

## Quick Start

```bash
git clone https://github.com/barnard704344/the-print-farm.git
cd the-print-farm
sudo bash setup.sh
```

The setup script will:

1. Install Python 3, pip, and Apache
2. Create a virtual environment and install dependencies
3. Create an admin user account
4. Detect whether install location is accessible by www-data
5. Configure systemd service and Apache reverse proxy
6. Auto-assign OrcaSlicer ports and create Apache vhosts
7. Start the farm manager

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

Edit config/config.yaml before production use.
