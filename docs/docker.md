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
docker pull ghcr.io/barnard704344/the-print-farm:v1.0.16
```

`latest` follows tagged releases. For deployments that should only change when
explicitly upgraded, use `v1.0.16` in both the pull and run commands. Images tagged
`main` or `dev` follow main-branch builds and may contain unreleased changes.

The native Bambu Obico bridge is newer than `v1.0.16`; `latest` currently does
not include it. See [Obico in Docker](#obico-in-docker) before choosing an image
for that integration.

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

## Obico in Docker

The Print Farm container includes its bridge dependencies when built from
current `main`. It does not include an Obico server or AI model. Run Obico
separately and verify its web/API, worker, Redis, and detection backend before
enabling the farm integration.

Until a newer tagged release includes the bridge, use a current main-branch
build instead of `v1.0.16`/`latest`:

```bash
docker pull ghcr.io/barnard704344/the-print-farm:main
```

Replace the image tag in the `docker run` examples with `:main` as well, or use
the source build above. Keep `/app/data` mounted: by default the current Obico
job identities live in `/app/data/obico`. If `queue.db_path` or
`queue.obico_state_dir` points elsewhere, persist the resulting state directory
too. Runtime configuration, including integration credentials, must also be
retained privately in `/app/config`.

The farm container must reach the physical printer's MQTT/camera ports and the
Obico HTTP(S)/WebSocket address. `localhost` inside the farm container refers
to that container, not your Docker host or a separate Obico container. Use a
reachable hostname/IP or a service name on a shared Docker network.

Obico sends AI alerts with its own SMTP and email preferences; the farm
container's email configuration does not configure Obico. Follow
[Obico prerequisites, notifications, and live verification](bambu-obico.md).

## GHCR Image Source

Images are published by:

- .github/workflows/docker-publish.yml

Published image path:

- `ghcr.io/barnard704344/the-print-farm:latest`
- `ghcr.io/barnard704344/the-print-farm:v1.0.16`
- `ghcr.io/barnard704344/the-print-farm:main` (main-branch builds)
- `ghcr.io/barnard704344/the-print-farm:dev` (main-branch builds)
