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

# Validar dashboards (estático, sin red, stdlib): rawText==query + filtro :instance:
python3 scripts/validate-dashboards.py

# Validar dashboards EN VIVO (stack levantado): ejecuta cada query contra influxdb-01
# con las template vars sustituidas. Detecta lo que el estático no ve: InfluxQL
# inválido, field inexistente (panel vacío) y tag inexistente en GROUP BY (serie falsa).
python3 scripts/validate-dashboards-live.py
python3 scripts/validate-dashboards-live.py --instance kapacitor-02 --task 01_cardinality --start 'now() - 6h'
```

URLs locales: Chronograf :8888, influxdb-01 :8086 (monitorización), influxdb-02 :8087 (datos), kapacitor-01 :9092 (alertas), kapacitor-02..05 :9093..9096.

## Arquitectura

Topología real de instancias independientes: **el Kapacitor de alertas (kapacitor-01) se suscribe al InfluxDB de monitorización (influxdb-01), no al de datos (influxdb-02)**. Solo kapacitor-01 carga `tick/` y `tick/handlers/` (el resto arranca con `KAPACITOR_LOAD_ENABLED=false`).

**Telegraf: uno por host (no un sondeo central).** Cada host corre su propio Telegraf que sondea solo su localhost (`/debug/vars`, `/metrics`, `/ping`) y marca todas sus métricas con un `[global_tags] instance = "${INSTANCE_NAME:-unset-instance}"`; la salida va al InfluxDB de monitorización vía `[[outputs.influxdb]] urls = ["${MONITOR_INFLUX_URL}"]` (DB `telegraf` en influxdb-01). Así **cada measurement** (host, `internal_*` y TICK) lleva el tag `instance` y los dashboards filtran todo con una sola variable `:instance:`. Chronograf lee de influxdb-01. En compose se aproxima con UN Telegraf central (ver Limitaciones).

Flujo de alertas: TICKscripts → topic `infra` → handlers (`tick/handlers/log.yaml` activo; slack/smtp como `.example`). Los TICKscripts se cargan por el `[load]` dir de kapacitor.conf (subdirs `tasks/` y `handlers/`).

SLA: `processors.starlark` en Telegraf genera el campo `up` (1/0) desde `http_response.result_code`; las CQs de `setup/sla_retention_and_cq.influxql` preagregan a la RP `sla_long` (400 d) que consume el dashboard 04.

## Convenciones no obvias (romperlas rompe dashboards/alertas)

- **Tres confs de Telegraf**: `conf/telegraf-influxdb.conf` y `conf/telegraf-kapacitor.conf` son los canónicos per-host (cada rol sondea su localhost, con procstat/tail); `docker/telegraf.conf` es la variante compose (un Telegraf central con urls a los servicios). Cambios de métricas → mantener los **tres** en sync.
- **Tag `instance` por env `INSTANCE_NAME`** (no `url`): se fija en `[global_tags]`, así lo llevan todos los measurements. Charset **solo `[A-Za-z0-9_-]`**: se interpola en el regex `=~ /^:instance:$/` de Chronograf; las `/` de las URLs (o cualquier metacaracter) lo rompen. Sin `INSTANCE_NAME` cae a `unset-instance` (fallo visible en el dropdown).
- **Tag `service` (no `server`)** en `http_response`/`prometheus`: el plugin ya fija `server`=URL y pisaría el tag propio. Dashboards, CQs y alertas filtran por `service`.
- **Variables `:instance:` de dashboards**: 02/03/04/05 usan tipo tagValues (dinámico; descubren instancias solas): 02 desde `influxdb_httpd`, 03 desde `kapacitor`, 04/05 desde `http_response` (cubre las 7 instancias). Chronograf no ofrece opción "All" en tagValues, por eso el dashboard 01 (overview) no tiene variable ni filtro `instance` y muestra siempre todas vía `GROUP BY "instance"`.
- **Nada de notación científica en InfluxQL**: `mean("x")/1e6` falla con `error parsing query: invalid duration` en InfluxDB 1.8 (el lexer lee `1e` como duración). Usa el literal `1000000`. Es un fallo silencioso: el panel solo muestra el error al abrirlo, por eso lo caza `validate-dashboards-live.py`.
- **Fields/tags inexistentes NO dan error**: una query sobre un field que no existe devuelve 0 series (panel vacío para siempre) y un `GROUP BY "tag_inexistente"` devuelve UNA serie con el tag vacío (línea plana engañosa). Antes de escribir una query: `SHOW FIELD KEYS FROM <m>` / `SHOW TAG KEYS FROM <m>`. Ojo con `kapacitor_ingress` (no tiene tag `task`), `kapacitor_nodes` (no tiene `write_errors` ni `points_written`: son stats de nodos `influxdb_out` y ninguna task usa `influxdbOut`) e `influxdb_tsm1_wal` (el field es `writeErr`, no `writeError`, que es de `influxdb_write`).
- **NUNCA pongas comillas manuales alrededor de una template var**. Chronograf 1.10 (`ui/src/tempVars/utils/replace.ts`) interpola el valor de una var tipo tagValue **entrecomillado** (`'kapacitor-01'`) en general, y **crudo solo dentro de un literal regex** (tras `=~` o `!~`). Por eso la convención del repo es `=~ /^:instance:$/`: dentro del regex entra crudo y funciona. Escribir `= ':instance:'` produce `= ''kapacitor-01''` en el navegador → `error parsing query` → panel o dropdown vacío **sin ningún error visible en el JSON**. Ambos validadores lo cazan (el estático por patrón, el live reproduciendo la interpolación real).
- **`:task:` del 06 es una meta query encadenada a `:instance:`**: template `"type": "influxql"` (no `tagValues`, que no permite referenciar otras vars) con `SHOW TAG VALUES ON "telegraf" FROM "kapacitor_nodes" WITH KEY = "task" WHERE "instance" =~ /^:instance:$/`, para que el dropdown de tasks se filtre por la instancia elegida. `sourceID: "dynamic"` como el resto. Ojo: la forma regex no es opcional, es lo que evita el doble entrecomillado descrito arriba.
- **Nivel de alerta (`level`) y `kind`/`match` van como FIELD string, no tag**: `level` cambia en cada transición OK↔CRITICAL (churn de series) y, sobre todo, un tag no se puede seleccionar con `last()`, que es justo lo que necesitan las tablas del 03.
- **Memstats en snake_case** (`heap_inuse`, `pause_total_ns`…): formato de Telegraf ≥1.39. Telegraf antiguos emiten CamelCase; afecta a queries del dashboard 02.
- `kapacitor_alert` no existe en Telegraf 1.39; los eventos por topic se miden con `kapacitor_topics.collected`.
- `setup/sla_retention_and_cq.influxql` no es formato `influx -import` estándar: `docker/setup-influx.sh` lo trocea por `;` y ejecuta sentencia a sentencia (idempotente, tolera "ya existe"). Para aplicación manual con `-import` existe `setup/sla_retention_and_cq.import.influxql` (formato import: `# DDL`, una sentencia por línea); cambios de sentencias → mantener **ambos** en sync.
- Secretos por env (p. ej. `KAPACITOR_SLACK_0_URL`), nunca en los `.conf`; bloques auth/TLS comentados a propósito (hardening para producción).

## Limitaciones conocidas en compose

- Paneles de uptime de proceso/RSS/FDs vacíos (sin `inputs.procstat`) y "Queries lentas/min" vacío (`inputs.tail` comentado, influxd loguea a stdout). Ambos funcionan en despliegue real; no son bugs.
- **`kapacitor_topics_api` y `kapacitor_handlers` solo se pueblan para `kapacitor-01`**: en `docker/telegraf.conf` los `[[inputs.http]]` que scrapean la API de alertas (`/kapacitor/v1/alerts/topics` y `.../topics/infra/handlers`) están definidos solo contra kapacitor-01, que es el único que carga TICKscripts. Al elegir otra instancia en el 03, las tablas "Nivel actual por topic" y "Handlers del topic infra" salen vacías. En despliegue real el conf per-host (`conf/telegraf-kapacitor.conf`) los lleva activos en todos los hosts: los endpoints devuelven 200 con listas vacías (no 404) y con `optional = true` no generan ni error ni puntos.
- **Telegraf central en compose** (no uno por host): sus métricas de host e `internal_*` se atribuyen todas a `instance=influxdb-01` (el `INSTANCE_NAME` del servicio). Al seleccionar otra instancia en el dashboard 05, los **paneles de host** (cpu/mem/disk/diskio/system/internal) salen vacíos; los paneles TICK (por-URL) sí cambian. En despliegue real (un Telegraf por host) todos los paneles filtran bien.
