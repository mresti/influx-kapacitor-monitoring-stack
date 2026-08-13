# Runbook — "Nivel actual por topic (API)" en CRITICAL (dashboard 03)

## Qué muestra la tabla

Query del panel (dashboard `03 - Kapacitor Deep`):

```sql
SELECT last("level") AS "nivel", last("collected") AS "eventos"
FROM "telegraf"."autogen"."kapacitor_topics_api"
WHERE ... AND "instance" =~ /^:instance:$/ GROUP BY "id"
```

Los datos vienen de un `[[inputs.http]]` de Telegraf que scrapea la **API de
alertas de Kapacitor**: `GET /kapacitor/v1/alerts/topics` (no sale en
`/debug/vars`). Una fila por topic (`id`; en este stack, `infra` y los topics
implícitos `main:<task>:alert*` si existieran):

| Columna | Campo API | Significado (doc oficial) |
|---|---|---|
| `id` | `id` | Identificador del topic. Un topic es un *namespace* pub-sub: las alertas publican eventos en él y los handlers suscritos los reciben. |
| `nivel` | `level` | **Nivel actual del topic = la severidad más alta de los eventos sin recuperar** que contiene. Valores: `OK` < `INFO` < `WARNING` < `CRITICAL`. |
| `eventos` | `collected` | Nº acumulado de eventos de alerta procesados por el topic. |

## Qué significa un `id` en CRITICAL

Según la doc oficial, el nivel del topic es un agregado: **al menos un evento
del topic sigue en CRITICAL sin haberse recuperado**. No dice cuál ni cuántos:
puede ser 1 evento o varios; el topic se queda en CRITICAL hasta que **todos**
sus eventos vuelvan a OK (recuperación automática cuando la condición del
TICKscript deja de cumplirse; no hay comando de "ack"). El estado **persiste a
reinicios** de Kapacitor.

En este stack todos los TICKscripts publican en el topic `infra`
(`.topic('infra')`), así que `infra` = CRITICAL significa que alguna de estas 5
alertas está disparada:

| Task | Condición CRIT | Qué mirar |
|---|---|---|
| `00_ingest_errors` | errores de ingesta en InfluxDB | dashboard 01/02: writes fallidos, `influxdb_write` |
| `01_cardinality` | series ≥ 90 % de `max-series-per-database` | dashboard 05: gauge de series; cardinalidad por DB |
| `02_kapacitor_node_errors` | errores en nodos de tasks | dashboards 03/06: errores/s por nodo y task |
| `03_availability` | deadman / sonda `up`=0 | dashboard 04: qué instancia está caída; `/ping` |
| `04_telegraf_pipeline` | Telegraf descarta métricas | dashboard 05: buffer y `metrics_dropped` |

## Acciones para resolverlo

1. **Identificar el/los eventos CRITICAL** (el `id` del evento dice task y serie):

   ```bash
   docker compose exec kapacitor-01 kapacitor show-topic infra
   # o solo lo crítico, vía API:
   curl -s 'http://localhost:9092/kapacitor/v1/alerts/topics/infra/events?min-level=CRITICAL'
   ```

   `show-topic` lista `ID`, `Level`, `Collected`, `Handlers` y una tabla
   `Events` con nivel, mensaje y fecha por evento.

2. **Confirmar que los handlers avisaron** (si nadie recibió nada, revisar
   también el handler):

   ```bash
   docker compose exec kapacitor-01 kapacitor list topic-handlers infra
   docker compose exec kapacitor-01 tail -50 /var/log/kapacitor/alerts.log   # handler log.yaml
   ```

3. **Resolver la causa raíz** según la task disparada (tabla anterior). El
   evento se recupera **solo** cuando la condición vuelve a OK; el topic baja a
   OK cuando lo hacen todos sus eventos.

4. **Verificar la recuperación**: repetir `kapacitor show-topic infra`
   (Level: OK) y esperar el siguiente scrape de Telegraf (10 s en compose) para
   ver la tabla del dashboard actualizada.

5. **Evento huérfano/stale** (la serie o task ya no existe y el evento nunca va
   a recuperar; caso típico: task borrada o tag que dejó de emitirse):

   ```bash
   docker compose exec kapacitor-01 kapacitor delete topics infra
   ```

   La API documenta que esto **borra todos los eventos y el estado** del topic;
   el topic se recrea solo con el siguiente evento. Tras borrarlo, verificar que
   los handlers siguen ligados (`kapacitor list topic-handlers infra`); si no,
   re-aplicar los YAML de `tick/handlers/` (reinicio de kapacitor-01 los
   recarga del `[load]` dir).

## Ojo (limitaciones de la tabla)

- `last("level")` sobre la ventana del dashboard: si Telegraf deja de scrapear,
  la tabla se queda congelada o vacía — no confundir con "OK".
- En compose solo se scrapea kapacitor-01; con otra `:instance:` la tabla sale
  vacía por diseño (ver README, limitaciones).
- `collected` es acumulado desde el arranque: que crezca no implica problema;
  el nivel lo da `level`.

## Fuentes (doc oficial)

- [Alerts overview — topics y niveles](https://docs.influxdata.com/kapacitor/v1/working/alerts/)
- [Using alert topics — `show-topic`, estado y recuperación](https://docs.influxdata.com/kapacitor/v1/working/using_alert_topics/)
- [HTTP API — `/kapacitor/v1/alerts/topics` (`level`, `collected`, `min-level`, DELETE)](https://docs.influxdata.com/kapacitor/v1/working/api/)
- [CLI — `kapacitor delete topics`](https://docs.influxdata.com/kapacitor/v1/reference/cli/kapacitor/delete/)
- [CLI — `kapacitor show-topic`](https://docs.influxdata.com/kapacitor/v1/reference/cli/kapacitor/show-topic/)
