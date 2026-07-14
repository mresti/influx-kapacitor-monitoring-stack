#!/bin/sh
# =============================================================================
# One-shot: crea la DB "telegraf" y aplica setup/sla_retention_and_cq.influxql
# (RP sla_long + Continuous Queries). Idempotente: los errores de "ya existe"
# no abortan el resto de sentencias.
#
# No se usa `influx -import` porque exige el formato "# DDL / # DML"; en su lugar
# se lanza cada sentencia con `influx -host influxdb -execute '...'`.
# =============================================================================
set -u

# InfluxDB de monitorización (recibe la DB telegraf + RP/CQs). Override por env.
INFLUX_HOST="${INFLUX_HOST:-influxdb-01}"
PING_URL="http://${INFLUX_HOST}:8086/ping"
SETUP_FILE="/setup/sla_retention_and_cq.influxql"

echo "setup-influx: esperando a InfluxDB (${PING_URL})..."
until curl -sf "$PING_URL" >/dev/null 2>&1; do
  sleep 2
done
echo "setup-influx: InfluxDB disponible"

influx -host "$INFLUX_HOST" -execute 'CREATE DATABASE telegraf'
echo "setup-influx: base de datos telegraf asegurada"

# Quita comentarios (--), colapsa saltos de linea y separa por ';' para que cada
# Continuous Query multi-linea llegue como una sola sentencia a -execute.
grep -v '^[[:space:]]*--' "$SETUP_FILE" \
  | tr '\n' ' ' \
  | awk 'BEGIN{RS=";"}{gsub(/^[ \t]+|[ \t]+$/,""); if(length($0)>0) print $0}' \
  | while IFS= read -r stmt; do
      echo "setup-influx: >> ${stmt}"
      influx -host "$INFLUX_HOST" -execute "$stmt" || \
        echo "setup-influx: (aviso) sentencia fallida (probablemente ya existe)"
    done

echo "setup-influx: completado"
