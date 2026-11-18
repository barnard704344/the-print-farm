# The Print Farm

A lightweight, web-based print farm manager for BambuLab and Klipper printers.

Built primarily for primary and secondary schools—but equally usable at home,
in makerspaces, and in other small print farms—The Print Farm keeps things
simple: no unnecessary plugins, extensions, or heavy dependencies.

Current release: **[v1.0.16](https://github.com/barnard704344/the-print-farm/releases/tag/v1.0.16)**,
with personal Orca upload attribution, clearer dark mode, preview-led library
cards, and improved Klipper error handling. See the [changelog](CHANGELOG.md).

Current `main` also includes native Bambu Obico monitoring and **Create & connect**;
the `v1.0.16` tag predates those features. New installs from `main` use `setup.sh`
to install all required dependencies.

This README acts as a documentation index. Detailed information is split across the docs pages below.

## Documentation Menu

- [Overview](docs/overview.md)
- [Dashboard Screenshots](docs/screenshots.md)
- [Getting Started](docs/getting-started.md)
- [Docker](docs/docker.md)
- [Printers and OrcaSlicer](docs/printers-and-orcaslicer.md)
- [Configuration](docs/configuration.md)
- [Bambu LAN Obico Setup and Testing](docs/bambu-obico.md)
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
- Self-hosted Obico optional

## Installation And Updates

For a native Debian, Ubuntu, or Raspberry Pi OS installation:

```bash
git clone https://github.com/barnard704344/the-print-farm.git
cd the-print-farm
sudo bash setup.sh
```

Existing native installations can use **Settings → System → Software Update** when every
configured printer and active job is in a safe state. Docker installations
should pull and recreate the container instead. See [Getting
Started](docs/getting-started.md) and [Docker](docs/docker.md).

## License

Internal use.
