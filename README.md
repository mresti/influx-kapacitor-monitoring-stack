# Observabilidad del stack TICK — InfluxDB OSS 1.8.10 + Kapacitor 1.8.2

Configuraciones (`.conf`), dashboards de Chronograf (`.json`), Continuous
Queries y TICKscripts de alerta para observar salud, SLA y capacidad de InfluxDB
y Kapacitor con las métricas de **Telegraf**. **Incluye las 8 mejoras + 6
propuestas adicionales** (ver secciones finales) y una topología real de
2 InfluxDB + 5 Kapacitor.

## Estructura

```
influx-obs/
├── conf/
│   ├── influxdb.conf              # [monitor], /debug/vars, /metrics, flux, hardening
│   ├── kapacitor.conf            # /debug/vars, [stats], suscripciones, hardening
│   ├── telegraf-influxdb.conf    # conf per-host rol InfluxDB (sondea localhost + host + starlark "up")
│   └── telegraf-kapacitor.conf   # conf per-host rol Kapacitor (sondea localhost + host + starlark "up")
├── scripts/
│   └── validate-dashboards.py    # validador estático de dashboards (stdlib, sin red)
├── setup/
│   └── sla_retention_and_cq.influxql   # RP sla_long + Continuous Queries (#3,#4)
├── tick/                    # alertas como código (#2,#4,#7 + 6 propuestas)
│   ├── 00_ingest_errors.tick
│   ├── 01_cardinality.tick
│   ├── 02_kapacitor_node_errors.tick
│   ├── 03_availability.tick
│   ├── 04_telegraf_pipeline.tick     # buffer/metrics_dropped de Telegraf
│   └── handlers/                     # handlers del topic 'infra' (como código)
│       ├── log.yaml                  # activo (log a fichero)
│       ├── slack.yaml.example
│       └── smtp.yaml.example
├── dashboards/              # importar en Chronograf
│   ├── 01-health.json
│   ├── 02-influx-deep.json
│   ├── 03-kapacitor-deep.json
│   ├── 04-sla-stats.json
│   └── 05-capacity-stats.json
├── docker-compose.yml       # stack completo (topología real) para pruebas
└── docker/                  # variantes/servicios de aprovisionamiento (compose)
    ├── telegraf.conf        # variante compose (UN Telegraf central, urls a los servicios)
    ├── setup-influx.sh      # crea DB + RP + CQs + retención
    └── provision-chronograf.py  # crea source + importa/actualiza dashboards
```

## Topología (instancias independientes)

Caso de uso real: **el Kapacitor de alertas cuelga del InfluxDB de
monitorización**, no del de datos. Y **cada host corre su propio Telegraf**
(no un sondeo central): sondea solo su localhost y marca todas sus métricas con
`instance = "${INSTANCE_NAME}"`; todos escriben a influxdb-01 vía
`MONITOR_INFLUX_URL`.

```
   Telegraf (host influxdb-01) ─instance=influxdb-01─┐
   Telegraf (host influxdb-02) ─instance=influxdb-02─┤
   Telegraf (host kapacitor-01)─instance=kapacitor-01┤   DB "telegraf"   ┌──────────────┐
   Telegraf (host kapacitor-02)─instance=kapacitor-02┼─────────────────►│ influxdb-01  │
   Telegraf (host kapacitor-…) ─instance=kapacitor-…─┘  (MONITOR_INFLUX │ (monitoriz.) │
        cada uno sondea SU localhost                        _URL)        └──────┬───────┘
        (/debug/vars, /metrics, /ping, host, internal)                          │ suscripción
                                                                                 ▼
   ┌──────────────┐   suscripción   ┌───────────────────────────┐        ┌───────────────┐
   │ influxdb-02  │ ───────────────►│ kapacitor-02..05 (datos)  │        │ kapacitor-01  │ ALERTAS
   │ (datos)      │                 └───────────────────────────┘        │ (TICKscripts, │ topic 'infra'
   └──────────────┘                                                       │  handlers)    │ → log/slack/smtp
                    Chronograf lee de influxdb-01 (dashboards)            └───────────────┘
```

### Selectores de instancia (`:instance:`)

Los dashboards que filtran por instancia usan la variable de plantilla
`:instance:` (`AND "instance" =~ /^:instance:$/`) de tipo **tagValues**
(dinámica): `SHOW TAG VALUES ... WITH KEY = "instance"`. Descubre las instancias
solas según lo que hay en la DB (no hay que mantener listas; instancia nueva =
aparece sola al llegar datos):

- **InfluxDB Deep (02):** desde `influxdb_httpd`.
- **Kapacitor Deep (03):** desde `kapacitor`.
- **SLA / Capacity (04, 05):** desde `http_response` (única medida con el tag
  `instance` de las 7 instancias: influx + kapacitor).

**Health (01):** no tiene variable ni filtro `instance`. Como Chronograf no
ofrece opción "All" en tagValues, el overview muestra **siempre todas las
instancias** vía `GROUP BY "instance"`.

**Capacity (05) filtra al completo.** Con un Telegraf por host, todos los
measurements (incluidos host `cpu`/`mem`/`disk`/`diskio`/`system`, `internal_*` y
`procstat`) llevan el tag `instance`, así que **cada panel del 05** aplica
`AND "instance" =~ /^:instance:$/` y agrupa por `instance`. El nombre de instancia
debe ser `[A-Za-z0-9_-]` (se interpola en el regex de Chronograf).

> Nota (cutover): las series **históricas escritas antes** de añadir el tag
> `instance` no lo tienen, así que los paneles de host mostrarán **huecos**
> pre-cutover al filtrar; se rellenan a partir del momento en que cada Telegraf
> per-host empieza a emitir el tag.

## Arquitectura de datos

```
InfluxDB  ──/debug/vars──┐
          ──/metrics─────┤
Kapacitor /…/debug/vars──┼──► Telegraf ──► InfluxDB (DB "telegraf") ──► Chronograf
/ping  +  /…/ping  ──────┤        │  (processors.starlark añade up=1/0)
host cpu/mem/disk/proc───┘        └──► Continuous Queries ──► RP sla_long
                                          Kapacitor (TICKscripts) ──► topic 'infra'
                                                                    └► handlers (log/slack)
```

## Puesta en marcha

1. **Un Telegraf por host.** Copia `conf/influxdb.conf`/`conf/kapacitor.conf` a
   sus hosts. En cada host, copia el `telegraf-*.conf` **de su rol** a
   `/etc/telegraf/telegraf.conf` (rol InfluxDB → `telegraf-influxdb.conf`; rol
   Kapacitor → `telegraf-kapacitor.conf`); sondean solo su localhost. El tag
   `instance` y la URL de salida vienen de **dos env vars** (`INSTANCE_NAME`,
   `MONITOR_INFLUX_URL`), no del `.conf`. Con systemd, un `EnvironmentFile` por
   host (charset de `INSTANCE_NAME`: solo `[A-Za-z0-9_-]`):

   ```ini
   # /etc/telegraf/telegraf.env  (referenciado desde el unit con EnvironmentFile=)
   INSTANCE_NAME=influxdb-01
   MONITOR_INFLUX_URL=http://influxdb-01:8086
   ```

   ```ini
   # override del unit:  systemctl edit telegraf
   [Service]
   EnvironmentFile=/etc/telegraf/telegraf.env
   ```
2. Reinicia `influxd`, `kapacitord`, `telegraf` (`systemctl restart telegraf`).
3. Crea RP y Continuous Queries (una vez):
   `influx -database telegraf -import -path setup/sla_retention_and_cq.influxql -precision ns`
4. Despliega las alertas: copia `tick/*.tick` a `/etc/kapacitor/load/tasks/` (el
   `[load]` de `kapacitor.conf` escanea el subdirectorio `tasks/`) y reinicia
   Kapacitor, o cárgalas con `kapacitor define ... -tick ...`. Comprueba con
   `kapacitor list tasks`.
5. En Chronograf: **Dashboards → Import Dashboard** y sube cada `.json`.

## Importar dashboards por la UI de Chronograf

Método manual (alternativa a `docker compose up chronograf-provision`).

**Prerequisitos:**

1. Un **source** en Chronograf apuntando al InfluxDB de **monitorización**
   (influxdb-01), con **Telegraf Database = `telegraf`**.
2. Para el **dashboard 04**: la RP `sla_long` y las Continuous Queries creadas
   **antes** (`setup/sla_retention_and_cq.influxql`); si no, el panel de histórico
   diario sale vacío.
3. Telegraf ya **escribiendo datos**: con la DB vacía el desplegable `:instance:`
   sale vacío; se puebla solo en cuanto hay datos (es dinámico, `tagValues`).

**Pasos** (por cada fichero de `dashboards/`):

1. **Dashboards → Import Dashboard**.
2. Arrastra el `.json` del repo (el formato `{"meta": ..., "dashboard": ...}` es
   justo el que espera la UI).
3. En **Reconcile Sources**, mapea al source local.
4. **Import**. Repite por cada fichero.

**Aviso:** la UI **siempre crea un dashboard nuevo** (no deduplica por nombre): al
reimportar, **borra antes el antiguo** o tendrás duplicados. El script
`docker compose up chronograf-provision` **sí** deduplica (PUT por nombre) y es el
método preferido.

## Prueba local con Docker Compose

Stack completo con la **topología real** (2 InfluxDB + 5 Kapacitor +
Telegraf 1.39.1 + Chronograf 1.10.9) para validar conf, dashboards, CQs y
alertas sin instalar nada. Son **9 contenedores** + 2 one-shots:

```bash
docker compose up -d      # ~90 s hasta que todo esté healthy
```

URLs (puertos publicados en el host):

- InfluxDB monitorización (influxdb-01): http://localhost:8086
- InfluxDB datos (influxdb-02):          http://localhost:8087
- Kapacitor alertas (kapacitor-01):      http://localhost:9092
- Kapacitor datos (kapacitor-02..05):    http://localhost:9093..9096
- Chronograf:                            http://localhost:8888

Se aprovisiona solo (servicios one-shot, ambos contra influxdb-01):

- **influx-setup** — crea la DB `telegraf`, la RP `sla_long`, las Continuous
  Queries y la retención (`monitor` de `_internal` a 7d) de
  `setup/sla_retention_and_cq.influxql`.
- **chronograf-provision** — crea el source e **importa/actualiza** los 5
  dashboards (idempotente-actualizante: en un segundo `up` hace PUT, no duplica).

Detalles:

- Telegraf usa `docker/telegraf.conf`: **UN Telegraf central** que sondea todos
  los servicios por HTTP (aproximación del modelo real per-host, cuyos canónicos
  son `conf/telegraf-influxdb.conf` + `conf/telegraf-kapacitor.conf`). Pin
  `telegraf:1.39.1` (última 1.39.x).
- **Solo `kapacitor-01`** (alertas) monta `./tick` en `/etc/kapacitor/load/tasks/`
  y `./tick/handlers` en `/etc/kapacitor/load/handlers/` (el `[load]` escanea esos
  subdirectorios). `kapacitor-02..05` arrancan con `KAPACITOR_LOAD_ENABLED=false`.
- **Limitaciones conocidas en local:** (1) sin `inputs.procstat`
  (`influxd`/`kapacitord` en contenedores aparte) → paneles de **uptime de
  proceso, RSS y FDs** vacíos; (2) `inputs.tail` de queries lentas no activo
  (influxd loguea a stdout) → panel **"Queries lentas/min"** vacío; (3) el
  Telegraf central atribuye **todas** las métricas de host e `internal_*` a
  `instance=influxdb-01`, así que al filtrar el dashboard 05 por otra instancia
  los **paneles de host salen vacíos** (los paneles TICK por-URL sí cambian).
  Los tres funcionan en un despliegue real (un Telegraf por host, junto a los
  procesos/logs).

```bash
docker compose down -v    # parar y borrar volúmenes
```

## Los 5 dashboards

| # | Fichero | Qué responde |
|---|---------|--------------|
| 1 | `01-health.json` | Disponibilidad, **uptime de proceso y host**, latencia `/ping`, errores de ingesta, queries, errores en nodos de Kapacitor, throughput. |
| 2 | `02-influx-deep.json` | Memoria/GC, HTTP, write/query engine, TSM/WAL, **percentiles de latencia y de pausa GC**. Filtro por instancia (`:instance:`). |
| 3 | `03-kapacitor-deep.json` | Tareas, edges, ingress, nodos, alertas por nivel, cardinalidad, memoria. Filtro por instancia (`:instance:`). |
| 4 | `04-sla-stats.json` | SLA (%) por servicio/instancia, error budget, **SLI de ingesta**, **histórico diario desde CQ**. |
| 5 | `05-capacity-stats.json` | Cardinalidad por DB, **gauge de uso vs límite de series**, disco, RAM/CPU/FDs, diskio, buffer de Telegraf. **Filtrable al completo por instancia** (`:instance:`): host + TICK + `internal_*` (requiere Telegraf per-host; en compose los paneles host solo tienen `influxdb-01`). |

Validación estática (sin red, stdlib) antes de importar: `python3 scripts/validate-dashboards.py`
comprueba que los JSON parsean, que `queryConfig.rawText` == `query` (evita editar
uno y olvidar el otro) y que el dashboard 05 filtra por `:instance:` en todas sus
queries (WARN informativo para 02/03/04).

## Notas de métricas

- Nombres de campos de memstats según versión de Telegraf: los Telegraf
  recientes (p. ej. 1.39, el de la variante docker) emiten `influxdb_memstats`
  en snake_case (`heap_inuse`, `pause_total_ns`, `num_gc`, `alloc`), igual que
  `kapacitor_memstats` (`heap_in_use_bytes`, `num_gc`, `gc_cpu_fraction`). Los
  dashboards usan esos nombres. **Caveat:** Telegraf antiguos emitían CamelCase
  (`HeapInuse`, `PauseTotalNs`, `NumGC`); si tu despliegue real usa una versión
  vieja, ajusta las queries de memstats del dashboard 02.
- Tag `service` (no `server`) en `http_response` y `prometheus`: el plugin
  `http_response` ya fija un tag `server` = la URL sondeada, que pisaría a un tag
  propio con esa clave. Por eso los health checks etiquetan `service` =
  `influxdb`/`kapacitor`, y los dashboards/CQs/alertas filtran por `service`.
- El campo `up` (1/0) lo genera `processors.starlark` en Telegraf a partir de
  `http_response.result_code`. Es la base del SLA y de la alerta de disponibilidad.

---

## Las 8 mejoras — IMPLEMENTADAS

1. **Uptime real de proceso.** Paneles de uptime de `influxd` y `kapacitord`
   (`now() - procstat.created_at`, vía Flux) y uptime del host
   (`system.uptime`) en el dashboard Health.
2. **SLA basado en errores, no solo en sondas.** Nuevo SLI de ingesta en el
   dashboard SLA: `% escrituras OK = (1 - pointsWrittenFail/pointsWrittenOK)·100`,
   más un panel de puntos fallidos/descartados por segundo.
3. **Resolución del SLA + RP dedicada.** `setup/sla_retention_and_cq.influxql`
   crea la RP `sla_long` (400 d) y Continuous Queries que pre-agregan
   disponibilidad horaria y diaria. El dashboard SLA tiene un panel de
   **histórico diario** que consume `sla_long.sla_daily` (rangos largos baratos).
4. **Alta cardinalidad (single-node OSS).** Gauge "uso de series vs límite (%)"
   en Capacity, CQ de histórico de cardinalidad, y TICKscript
   `01_cardinality.tick` que alerta al 70 %/90 % de `max-series-per-database`.
   `index-version = tsi1` ya activado en `influxdb.conf`.
5. **Plantilla por instancia.** InfluxDB Deep y Kapacitor Deep usan el selector
   `:instance:` (tag `instance`, único por instancia) y agrupan por `instance`,
   así escalan a N instancias sin tocar las queries. Se usa `instance` en lugar
   de `url` porque los valores del tag `url` son URLs cuyas `/` rompen el regex
   `=~ /^:instance:$/` al interpolarlo Chronograf.
6. **Percentiles de latencia.** Paneles p50/p95/p99 de la sonda `/ping`
   (muestras reales) y percentiles reales de **pausa de GC**
   (`go_gc_duration_seconds`, summary de `/metrics`, vía `inputs.prometheus`).
   > Limitación honesta: InfluxDB **1.8** NO expone histogramas de latencia de
   > request en `/metrics` (eso llegó en 2.x). Por eso los percentiles de
   > latencia de servicio se aproximan con la sonda `/ping`; para p95/p99 reales
   > por endpoint haría falta instrumentación de aplicación o migrar a 2.x.
7. **Alertas como código.** TICKscripts en `tick/`: errores de ingesta,
   cardinalidad, errores en nodos de Kapacitor y disponibilidad (con `deadman`).
   Se cargan solos desde el `[load]` dir (subdirectorio `tasks/`).
8. **Hardening.** Bloques comentados de auth/TLS en los tres `.conf`, uso de
   variables de entorno para contraseñas en Telegraf, y recordatorios de activar
   `auth-enabled`/`https` en producción.

## Las 6 propuestas adicionales — IMPLEMENTADAS

1. **Handlers de alertas como código.** Los 5 TICKscripts publican en el topic
   `infra` (`.topic('infra')`) además de `.log()`. Los handlers viven en
   `tick/handlers/` (montados en `/etc/kapacitor/load/handlers/` de kapacitor-01):
   `log.yaml` (activo), `slack.yaml.example` y `smtp.yaml.example`. `kapacitor.conf`
   trae `[[slack]]`/`[smtp]` deshabilitados con placeholders; el webhook/token se
   inyecta por env (`KAPACITOR_SLACK_0_URL`), nunca en texto plano. Panel nuevo en
   Kapacitor Deep con `kapacitor_topics.collected` (eventos por topic). Nota:
   telegraf 1.39 **no** emite `kapacitor_alert`; se usa `kapacitor_topics`.
2. **Compactaciones TSM.** Paneles en InfluxDB Deep: profundidad de cola por nivel
   (`tsmLevel{1,2}CompactionQueue`, `tsmFullCompactionQueue`) y tasas + errores
   (`tsm{Full,Level1}Compactions`, `cacheCompactions`, `…Err`) de
   `influxdb_tsm1_engine`.
3. **Pipeline de Telegraf.** Panel en Capacity con `internal_gather.gather_errors`
   y `internal_write.metrics_dropped` (derivadas, por host) + TICKscript
   `04_telegraf_pipeline.tick` (warn si buffer > 80 %, crit si descarta métricas;
   topic `infra`).
4. **Queries lentas.** `log-queries-after=10s` en `influxdb.conf` + bloque
   `[[inputs.tail]]` (grok) **comentado** en `telegraf.conf` con instrucciones, y
   panel "Queries lentas/min" en InfluxDB Deep (con nota de activación; vacío en
   docker porque influxd loguea a stdout).
5. **diskio.** Paneles en Capacity: latencia media `await`
   (`Δ(read_time+write_time)/Δ(reads+writes)`), `iops_in_progress` y throughput
   `read_bytes`/`write_bytes`/s, por host y dispositivo.
6. **Retención.** `setup/sla_retention_and_cq.influxql` (aplicado por
   `docker/setup-influx.sh`) fija `monitor` de `_internal` a 168h (7d, idempotente)
   y deja documentado (comentado) el `ALTER` de `autogen` de telegraf a 30d.
