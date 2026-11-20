#!/usr/bin/env bash
# Levanta Floci (emulador de AWS) con PERSISTENCIA en disco.
# Los datos (S3 con versiones de tfstate, DynamoDB, CloudWatch) sobreviven
# reinicios del contenedor y del computador gracias al volumen aegisvolt-floci-data.
set -euo pipefail

NAME=floci
IMAGE=floci/floci:latest

if docker ps -a --format '{{.Names}}' | grep -qx "$NAME"; then
  docker start "$NAME" >/dev/null
else
  docker run -d --name "$NAME" \
    -p 4566:4566 \
    -v aegisvolt-floci-data:/app/data \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -e FLOCI_STORAGE_MODE=persistent \
    -e FLOCI_STORAGE_PERSISTENT_PATH=/app/data \
    --add-host host.docker.internal:host-gateway \
    --restart unless-stopped \
    "$IMAGE" >/dev/null
fi

# Si existe la red de kind, conectar Floci para que los pods lo alcancen como http://floci:4566
if docker network ls --format '{{.Name}}' | grep -qx kind; then
  docker network connect kind "$NAME" 2>/dev/null || true
fi

timeout 60 bash -c 'until curl -sf http://localhost:4566/_floci/health >/dev/null; do sleep 1; done'
echo "Floci listo en http://localhost:4566 (persistente)"
