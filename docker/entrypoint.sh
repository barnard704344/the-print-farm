#!/usr/bin/env sh
set -eu

CONFIG_PATH="${FARM_CONFIG:-/app/config/config.yaml}"

if [ ! -f "$CONFIG_PATH" ]; then
  mkdir -p "$(dirname "$CONFIG_PATH")"
  cp /app/config/config.example.yaml "$CONFIG_PATH"
  echo "Created default config at $CONFIG_PATH. Update web.api_key and printer settings before production use."
fi

exec python -m src.main -c "$CONFIG_PATH"
