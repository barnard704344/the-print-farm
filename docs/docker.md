# Docker

Run The Print Farm with Docker on Linux or macOS.

## Prerequisites

You need:

- Docker installed
- Docker running

If you want to use the published image from GitHub Container Registry, you do not need to clone the repository or build the image locally.

## Option 1: Run the Prebuilt Image from GHCR

Use this if you already have Docker installed and want to run The Print Farm without building it from source.

### Create persistent folders

```bash
mkdir -p config data uploads logs
```

### Pull the latest image

```bash
docker pull ghcr.io/barnard704344/the-print-farm:latest
```

For reproducible deployments, pin this release instead:

```bash
docker pull ghcr.io/barnard704344/the-print-farm:v1.0.11
```

### Run the container

```bash
docker run -d --name the-print-farm \
  -p 5000:5000 \
  -v "$(pwd)/config:/app/config" \
  -v "$(pwd)/data:/app/data" \
  -v "$(pwd)/uploads:/app/uploads" \
  -v "$(pwd)/logs:/app/logs" \
  ghcr.io/barnard704344/the-print-farm:latest
```

### Dashboard URL

- http://host-ip:5000/the-print-farm

### Container networking

The official image explicitly sets `FARM_WEB_HOST=0.0.0.0` and
`FARM_ALLOW_REMOTE_BACKEND=true` so Docker port publishing can reach the
Waitress server. Native setup does not set these variables and remains bound to
`127.0.0.1` behind Apache.

Only publish port 5000 on a trusted network. When a host reverse proxy is the
only intended client, bind the published port to loopback:

```bash
-p 127.0.0.1:5000:5000
```

### Stop the container

```bash
docker stop the-print-farm
docker rm the-print-farm
```

### Persistence

These mounted paths keep your data on the host machine:

- /app/config
- /app/data
- /app/uploads
- /app/logs

Because these directories are mounted as volumes, your configuration, data, uploads, and logs persist across container restarts, container replacement, and image updates.

### Update to the latest image

```bash
docker pull ghcr.io/barnard704344/the-print-farm:latest
docker stop the-print-farm
docker rm the-print-farm
docker run -d --name the-print-farm \
  -p 5000:5000 \
  -v "$(pwd)/config:/app/config" \
  -v "$(pwd)/data:/app/data" \
  -v "$(pwd)/uploads:/app/uploads" \
  -v "$(pwd)/logs:/app/logs" \
  ghcr.io/barnard704344/the-print-farm:latest
```

This replaces the container with the newest published image while keeping your persistent data.

The dashboard's **Apply Update and Restart** button is for native installations
managed by `setup.sh`; it cannot replace its own Docker container. Always update
a container by pulling and recreating it as shown above.

## Option 2: Build and Run Locally from Source

Use this if you want to build the image on your machine, test local changes, or run directly from the repository source.

### Clone the repository

```bash
git clone https://github.com/barnard704344/the-print-farm.git
cd the-print-farm
```

### Create local folders

```bash
mkdir -p config data uploads logs
```

### Create your config file

```bash
cp -n config/config.example.yaml config/config.yaml
```

### Build and start with Docker Compose

```bash
docker compose up -d --build
```

### Dashboard URL

- http://host-ip:5000/the-print-farm

### Stop the stack

```bash
docker compose down
```

## Which Option Should I Use?

Use GHCR if:

- you want the fastest setup
- you do not need to modify the source code
- you want to run the published image

Use the local source build if:

- you are developing or debugging
- you want to test changes that are not yet published
- you want to build the image yourself from the repository

## Discovery Notes

- Linux: host networking can improve broadcast and scan discovery behavior
- macOS with Docker Desktop: direct printer IP configuration is recommended
- The published container does not install the native host's Apache,
  root-owned update helper, systemd service, DHCP client, or privileged macvlan
  virtual-printer layer. Use direct printer IPs for Docker, and set
  `virtual_printer: false` on BambuLab entries when LAN-mode emulation is not
  provided separately by the host.

## GHCR Image Source

Images are published by:

- .github/workflows/docker-publish.yml

Published image path:

- `ghcr.io/barnard704344/the-print-farm:latest`
- `ghcr.io/barnard704344/the-print-farm:v1.0.11`
