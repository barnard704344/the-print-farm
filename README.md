# The Print Farm

A lightweight, web-based print farm manager for BambuLab and Klipper printers.

Built primarily for primary and secondary schools, The Print Farm keeps things simple: no unnecessary plugins, extensions, or heavy dependencies.

Current release: **[v1.0.12](https://github.com/barnard704344/the-print-farm/releases/tag/v1.0.12)**.

This README acts as a documentation index. Detailed information is split across the docs pages below.

## Documentation Menu

- [Overview](docs/overview.md)
- [Getting Started](docs/getting-started.md)
- [Docker](docs/docker.md)
- [Printers and OrcaSlicer](docs/printers-and-orcaslicer.md)
- [Configuration](docs/configuration.md)
- [Architecture](docs/architecture.md)
- [3D Toolpath Viewer](docs/3d-toolpath-viewer.md)
- [API Reference Summary](docs/api-reference.md)
- [Full API Reference](API.md)
- [Changelog](CHANGELOG.md)

## Requirements

- Python 3.10+
- Apache 2 with `mod_proxy`
- Debian 12+ / Ubuntu 22.04+ / Raspberry Pi OS Bookworm
- `isc-dhcp-client` and `openssl`, installed automatically by `setup.sh` and required for virtual printers
- BambuLab P1/P1S firmware 01.08.02.00+ requires LAN-only mode and Developer Mode for local start/control commands
- Spoolman optional
- Happy Hare optional

## Installation And Updates

For a native Debian, Ubuntu, or Raspberry Pi OS installation:

```bash
git clone https://github.com/barnard704344/the-print-farm.git
cd the-print-farm
sudo bash setup.sh
```

Existing native installations can use **Settings → Software Update** when every
configured printer and active job is in a safe state. Docker installations
should pull and recreate the container instead. See [Getting
Started](docs/getting-started.md) and [Docker](docs/docker.md).

## License

Internal use.
