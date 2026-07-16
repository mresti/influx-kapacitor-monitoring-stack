# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Qué es

Observabilidad del stack TICK (InfluxDB OSS 1.8.10 + Kapacitor 1.8.2 + Telegraf + Chronograf) como código: `.conf`, dashboards Chronograf (JSON), Continuous Queries y TICKscripts de alerta. No hay código de aplicación ni build/lint/tests: la "prueba" es levantar el compose y validar.

## Comandos

```bash
docker compose up -d      # stack completo (~90 s hasta healthy); autoprovisiona DB/RP/CQs y dashboards
docker compose down -v    # parar y borrar volúmenes (reset total)
docker compose ps         # comprobar healthchecks

# Validar alertas cargadas en el Kapacitor de alertas
docker compose exec kapacitor-01 kapacitor list tasks
docker compose exec kapacitor-01 kapacitor list topic-handlers infra

# Re-ejecutar provisioners (idempotentes) tras cambiar setup/ o dashboards/
docker compose up influx-setup
docker compose up chronograf-provision   # PUT si el dashboard ya existe (por nombre), no duplica
```

URLs locales: Chronograf :8888, influxdb-01 :8086 (monitorización), influxdb-02 :8087 (datos), kapacitor-01 :9092 (alertas), kapacitor-02..05 :9093..9096.

## Arquitectura

Topología real de instancias independientes: **el Kapacitor de alertas (kapacitor-01) se suscribe al InfluxDB de monitorización (influxdb-01), no al de datos (influxdb-02)**. Telegraf sondea las 7 instancias (`/debug/vars`, `/metrics`, `/ping`) y escribe la DB `telegraf` en influxdb-01; Chronograf lee de influxdb-01. Solo kapacitor-01 carga `tick/` y `tick/handlers/` (el resto arranca con `KAPACITOR_LOAD_ENABLED=false`).

Flujo de alertas: TICKscripts → topic `infra` → handlers (`tick/handlers/log.yaml` activo; slack/smtp como `.example`). Los TICKscripts se cargan por el `[load]` dir de kapacitor.conf (subdirs `tasks/` y `handlers/`).

SLA: `processors.starlark` en Telegraf genera el campo `up` (1/0) desde `http_response.result_code`; las CQs de `setup/sla_retention_and_cq.influxql` preagregan a la RP `sla_long` (400 d) que consume el dashboard 04.

## Convenciones no obvias (romperlas rompe dashboards/alertas)

- **Dos telegraf.conf**: `conf/telegraf.conf` es el canónico (despliegue real, con procstat/tail); `docker/telegraf.conf` es la variante compose (urls a servicios). Cambios de métricas → mantener ambos en sync.
- **Tag `service` (no `server`)** en `http_response`/`prometheus`: el plugin ya fija `server`=URL y pisaría el tag propio. Dashboards, CQs y alertas filtran por `service`.
- **Tag `instance`** (no `url`) identifica cada instancia: las `/` de las URLs rompen el regex `=~ /^:instance:$/` de Chronograf. Una instancia = un bloque input de Telegraf con su tag (no varias urls en un bloque, compartirían tag).
- **Variables `:instance:` de dashboards**: 02/03 usan tipo tagValues (dinámico); 01/04/05 usan csv con `.*` como primer valor seleccionado (equivale a "All", Chronograf no lo ofrece en tagValues). Instancia nueva → añadirla a la lista csv de 01/04/05.
- **Memstats en snake_case** (`heap_inuse`, `pause_total_ns`…): formato de Telegraf ≥1.39. Telegraf antiguos emiten CamelCase; afecta a queries del dashboard 02.
- `kapacitor_alert` no existe en Telegraf 1.39; los eventos por topic se miden con `kapacitor_topics.collected`.
- `setup/sla_retention_and_cq.influxql` no es formato `influx -import` estándar: `docker/setup-influx.sh` lo trocea por `;` y ejecuta sentencia a sentencia (idempotente, tolera "ya existe").
- Secretos por env (p. ej. `KAPACITOR_SLACK_0_URL`), nunca en los `.conf`; bloques auth/TLS comentados a propósito (hardening para producción).

## Limitaciones conocidas en compose

Paneles de uptime de proceso/RSS/FDs vacíos (sin `inputs.procstat`) y "Queries lentas/min" vacío (`inputs.tail` comentado, influxd loguea a stdout). Ambos funcionan en despliegue real; no son bugs.
