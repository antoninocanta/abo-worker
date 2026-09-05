#!/usr/bin/env sh
# Un serveur de moteur derriere le proxy PyWorker de Vast.
set -eu

LOG_FILE="${ABO_ENGINE_LOG_FILE:-/var/log/abo-engine.log}"
touch "$LOG_FILE"

if [ -z "${ABO_SERVERLESS_ROUTE:-}" ]; then
  echo "ABO_SERVERLESS_ROUTE absent" >> "$LOG_FILE"
  exit 1
fi

# Le worker Vast sert en HTTPS avec un certificat signe pour cette instance.
# On reprend le protocole deja eprouve par l'image Qwen du depot.
if [ "${USE_SSL:-true}" = "true" ]; then
  if [ -z "${CONTAINER_ID:-}" ]; then
    echo "CONTAINER_ID absent : impossible de faire signer le certificat" >> "$LOG_FILE"
    exit 1
  fi

  cat > /etc/openssl-san.cnf <<'CNF'
[req]
default_bits       = 2048
distinguished_name = req_distinguished_name
req_extensions     = v3_req

[req_distinguished_name]
countryName         = US
stateOrProvinceName = CA
organizationName    = Vast.ai Inc.
commonName          = vast.ai

[v3_req]
basicConstraints = CA:FALSE
keyUsage         = nonRepudiation, digitalSignature, keyEncipherment
subjectAltName   = @alt_names

[alt_names]
IP.1   = 0.0.0.0
CNF

  openssl req -newkey rsa:2048 -subj "/C=US/ST=CA/CN=pyworker.vast.ai/" \
    -nodes -sha256 -keyout /etc/instance.key -out /etc/instance.csr \
    -config /etc/openssl-san.cnf >> "$LOG_FILE" 2>&1

  signed=0
  delay=2
  for attempt in 1 2 3 4 5; do
    code=$(curl -sS -o /etc/instance.crt -w '%{http_code}' \
      --header 'Content-Type: application/octet-stream' \
      --data-binary @/etc/instance.csr \
      -X POST "https://console.vast.ai/api/v0/sign_cert/?instance_id=${CONTAINER_ID}" || echo 000)
    if [ "$code" -ge 200 ] && [ "$code" -lt 300 ]; then
      signed=1
      break
    fi
    echo "signature du certificat : tentative $attempt refusee (HTTP $code)" >> "$LOG_FILE"
    sleep "$delay"
    delay=$((delay * 2))
  done
  if [ "$signed" -ne 1 ]; then
    echo "certificat non signe apres 5 tentatives" >> "$LOG_FILE"
    exit 1
  fi
fi

uvicorn server:app --app-dir /opt/abo --host 127.0.0.1 --port 18100 \
  >> "$LOG_FILE" 2>&1 &
ENGINE_PID=$!
trap 'kill -TERM "$ENGINE_PID" 2>/dev/null || true' EXIT

exec python /opt/abo/serverless-worker.py
