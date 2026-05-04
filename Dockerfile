FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FARM_CONFIG=/app/config/config.yaml

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY src /app/src
COPY templates /app/templates
COPY static /app/static
COPY config/config.example.yaml /app/config/config.example.yaml
COPY docker/entrypoint.sh /entrypoint.sh

RUN chmod +x /entrypoint.sh \
    && mkdir -p /app/config /app/data /app/uploads /app/logs

EXPOSE 5000

ENTRYPOINT ["/entrypoint.sh"]
