# Docker

Run The Print Farm with Docker on Linux or macOS.

## Local Run with Docker Compose

```bash
git clone https://github.com/barnard704344/the-print-farm.git
cd the-print-farm
mkdir -p config data uploads logs
cp -n config/config.example.yaml config/config.yaml
docker compose up -d --build
```

Dashboard URL:

- http://host-ip:5000/the-print-farm

Stop:

```bash
docker compose down
```

## Persistence

Persist these paths with volumes:

- /app/config
- /app/data
- /app/uploads
- /app/logs

## Discovery Notes

- Linux: host networking can improve broadcast/scan discovery behavior
- macOS (Docker Desktop): direct printer IP configuration is recommended

## GHCR Images

Images are published by workflow:

- .github/workflows/docker-publish.yml

Image path:

- ghcr.io/<owner>/the-print-farm

Example:

```bash
docker pull ghcr.io/<owner>/the-print-farm:latest
docker run -d --name the-print-farm \
  -p 5000:5000 \
  -v $(pwd)/config:/app/config \
  -v $(pwd)/data:/app/data \
  -v $(pwd)/uploads:/app/uploads \
  -v $(pwd)/logs:/app/logs \
  ghcr.io/<owner>/the-print-farm:latest
```
