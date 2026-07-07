# Observabilidad del stack TICK — InfluxDB OSS 1.8.10 + Kapacitor 1.8.2

Configuraciones (`.conf`), dashboards de Chronograf (`.json`), Continuous
Queries y TICKscripts de alerta para observar salud, SLA y capacidad de InfluxDB
y Kapacitor con las métricas de **Telegraf**. **Incluye las 8 mejoras
propuestas** (ver sección final).

## Estructura

```
influx-obs/
├── conf/
│   ├── influxdb.conf        # [monitor], /debug/vars, /metrics, flux, hardening
│   ├── kapacitor.conf       # /debug/vars, [stats], suscripciones, hardening
│   └── telegraf.conf        # inputs TICK + host + prometheus + starlark "up"
├── setup/
│   └── sla_retention_and_cq.influxql   # RP sla_long + Continuous Queries (#3,#4)
├── tick/                    # alertas como código (#2,#4,#7)
│   ├── 00_ingest_errors.tick
│   ├── 01_cardinality.tick
│   ├── 02_kapacitor_node_errors.tick
│   └── 03_availability.tick
├── dashboards/              # importar en Chronograf
│   ├── 01-health.json
│   ├── 02-influx-deep.json
│   ├── 03-kapacitor-deep.json
│   ├── 04-sla-stats.json
│   └── 05-capacity-stats.json
└── gen_dashboards.py        # regenera los .json
```

## Arquitectura de datos

```
InfluxDB  ──/debug/vars──┐
          ──/metrics─────┤
Kapacitor /…/debug/vars──┼──► Telegraf ──► InfluxDB (DB "telegraf") ──► Chronograf
/ping  +  /…/ping  ──────┤        │  (processors.starlark añade up=1/0)
host cpu/mem/disk/proc───┘        └──► Continuous Queries ──► RP sla_long
                                          Kapacitor (TICKscripts) ──► alertas
```

## Puesta en marcha

1. Copia los `.conf` a `/etc/influxdb`, `/etc/kapacitor`, `/etc/telegraf` y
   ajusta URLs/credenciales. Para varias instancias, añade todas las URLs en
   `[[inputs.influxdb]]` y `[[inputs.kapacitor]]` (el tag `url` las distingue).
2. Reinicia `influxd`, `kapacitord`, `telegraf`.
3. Crea RP y Continuous Queries (una vez):
   `influx -database telegraf -import -path setup/sla_retention_and_cq.influxql -precision ns`
4. Despliega las alertas: copia `tick/*.tick` a `/etc/kapacitor/load/` (el
   `[load]` de `kapacitor.conf`) y reinicia Kapacitor, o cárgalas con
   `kapacitor define ... -tick ...`. Comprueba con `kapacitor list tasks`.
5. En Chronograf: **Dashboards → Import Dashboard** y sube cada `.json`.

## Los 5 dashboards

| # | Fichero | Qué responde |
|---|---------|--------------|
| 1 | `01-health.json` | Disponibilidad, **uptime de proceso y host**, latencia `/ping`, errores de ingesta, queries, errores en nodos de Kapacitor, throughput. |
| 2 | `02-influx-deep.json` | Memoria/GC, HTTP, write/query engine, TSM/WAL, **percentiles de latencia y de pausa GC**. Filtro por instancia (`:url:`). |
| 3 | `03-kapacitor-deep.json` | Tareas, edges, ingress, nodos, alertas por nivel, cardinalidad, memoria. Filtro por instancia (`:url:`). |
| 4 | `04-sla-stats.json` | SLA (%) por servicio/instancia, error budget, **SLI de ingesta**, **histórico diario desde CQ**. |
| 5 | `05-capacity-stats.json` | Cardinalidad por DB, **gauge de uso vs límite de series**, disco, RAM/CPU/FDs, buffer de Telegraf. |

## Notas de métricas

- `influxdb_memstats` usa campos en mayúsculas (`Alloc`, `HeapInuse`);
  `kapacitor_memstats` usa snake_case (`heap_in_use_bytes`). No es un typo.
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
   `:url:` (tag `url`, único por instancia) y agrupan por `url`, así escalan a N
   instancias sin tocar las queries.
6. **Percentiles de latencia.** Paneles p50/p95/p99 de la sonda `/ping`
   (muestras reales) y percentiles reales de **pausa de GC**
   (`go_gc_duration_seconds`, summary de `/metrics`, vía `inputs.prometheus`).
   > Limitación honesta: InfluxDB **1.8** NO expone histogramas de latencia de
   > request en `/metrics` (eso llegó en 2.x). Por eso los percentiles de
   > latencia de servicio se aproximan con la sonda `/ping`; para p95/p99 reales
   > por endpoint haría falta instrumentación de aplicación o migrar a 2.x.
7. **Alertas como código.** Cuatro TICKscripts en `tick/`: errores de ingesta,
   cardinalidad, errores en nodos de Kapacitor y disponibilidad (con `deadman`).
   Se cargan solos desde el `[load]` dir.
8. **Hardening.** Bloques comentados de auth/TLS en los tres `.conf`, uso de
   variables de entorno para contraseñas en Telegraf, y recordatorios de activar
   `auth-enabled`/`https` en producción.
