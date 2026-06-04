# The Print Farm

A lightweight, web-based print farm manager for BambuLab and Klipper printers.

Built primarily for primary and secondary schools, The Print Farm keeps things simple: no unnecessary plugins, extensions, or heavy dependencies.

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

## Requirements

- Python 3.9+
- Apache 2 with `mod_proxy`
- Debian 11+ / Ubuntu 22.04+ / Raspberry Pi OS
- `isc-dhcp-client` and `openssl`, installed automatically by `setup.sh` and required for virtual printers
- Spoolman optional
- Happy Hare optional

## License

Internal use.
