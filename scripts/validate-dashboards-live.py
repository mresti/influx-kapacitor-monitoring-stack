#!/usr/bin/env python3
"""Validador EN VIVO de los dashboards de Chronograf (stdlib, contra influxdb-01).

Complementa a validate-dashboards.py (estático) ejecutando de verdad cada query
contra InfluxDB, con las template vars sustituidas por valores reales.

Detecta tres fallos que el validador estático no puede ver:
- InfluxQL invalido (p. ej. notacion cientifica: `/1e6` -> "invalid duration").
- Field inexistente en el measurement: la query NO da error, devuelve 0 series,
  y el panel sale vacio para siempre (bug silencioso).
- Tag inexistente en un GROUP BY: tampoco da error, devuelve UNA serie con el
  tag vacio, y el panel pinta una linea plana enganosa.

Un resultado vacio NO es fallo por si mismo: puede ser un filtro que no casa o
una limitacion conocida de compose (procstat, influxdb_slow_queries). Por eso
los measurements que no existen en este InfluxDB se reportan como SKIP.

Salida en español, exit 0 si no hay errores, 1 si los hay.
"""

import argparse
import glob
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

DASHBOARDS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dashboards")

FROM_PATTERN = re.compile(r'FROM\s+"(?P<db>[^"]+)"\."(?P<rp>[^"]+)"\."(?P<measurement>[^"]+)"')
FIELD_PATTERN = re.compile(r'\b(?:count|last|max|mean|median|min|percentile|stddev|sum)\s*\(\s*"([^"]+)"')
GROUP_BY_PATTERN = re.compile(r"GROUP BY\s+(?P<clause>.*?)(?:\s+fill\s*\(|$)", re.IGNORECASE | re.DOTALL)
QUOTED_PATTERN = re.compile(r'"([^"]+)"')
SUBQUERY_PATTERN = re.compile(r"FROM\s*\((?P<inner>.*)\)", re.DOTALL)
REGEX_LITERAL_PATTERN = re.compile(r"[=!]~\s*/(?:[^/\\]|\\.)*/")


class InfluxClient:
    """Cliente HTTP minimo contra la API /query de InfluxDB 1.x."""

    def __init__(self, url, database):
        self._url = url.rstrip("/")
        self._database = database
        self._schema_cache = {}

    def query(self, statement):
        """Devuelve (series, error). error != None solo si InfluxDB rechaza la query."""
        params = urllib.parse.urlencode({"db": self._database, "q": statement})
        try:
            with urllib.request.urlopen(f"{self._url}/query?{params}", timeout=30) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as exc:
            return [], _http_error_message(exc)
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            return [], f"sin conexion con InfluxDB: {exc}"
        result = (payload.get("results") or [{}])[0]
        if "error" in result:
            return [], result["error"]
        return result.get("series", []), None

    def schema(self, retention_policy, measurement):
        """(fields, tags) del measurement; conjuntos vacios si no existe."""
        key = (retention_policy, measurement)
        if key not in self._schema_cache:
            self._schema_cache[key] = (
                self._keys("SHOW FIELD KEYS", retention_policy, measurement),
                self._keys("SHOW TAG KEYS", retention_policy, measurement),
            )
        return self._schema_cache[key]

    def _keys(self, statement, retention_policy, measurement):
        series, error = self.query(
            f'{statement} FROM "{self._database}"."{retention_policy}"."{measurement}"'
        )
        if error:
            return set()
        return {values[0] for serie in series for values in serie.get("values", [])}


def _http_error_message(exc):
    try:
        return (json.load(exc).get("error") or f"HTTP {exc.code}")
    except (json.JSONDecodeError, OSError):
        return f"HTTP {exc.code}"


class Report:
    def __init__(self):
        self.errors = []
        self.skips = []
        self.checked = 0

    def fail(self, location, message):
        self.errors.append(f"{location}: {message}")

    def skip(self, location, message):
        self.skips.append(f"{location}: {message}")

    @property
    def ok(self):
        return not self.errors


def interpolate(query, values, quoted_variables):
    """Reproduce la interpolacion del frontend de Chronograf 1.10.

    Segun ui/src/tempVars/utils/replace.ts, el valor de una variable de tipo
    tagValue se inserta ENTRECOMILLADO ('valor'), salvo dentro de un literal
    regex (tras =~ o !~), donde se inserta crudo. Por eso la convencion del
    repo es =~ /^:instance:$/ y por eso poner comillas a mano alrededor de una
    variable ('':instance:'') produce InfluxQL invalido en el navegador.

    Las vars internas (:interval:, :dashboardTime:, :upperDashboardTime:) no
    son tagValues: van siempre crudas.
    """
    spans = [match.span() for match in REGEX_LITERAL_PATTERN.finditer(query)]

    def inside_regex(position):
        return any(start <= position < end for start, end in spans)

    pattern = re.compile("|".join(re.escape(name) for name in values))
    chunks = []
    cursor = 0
    for match in pattern.finditer(query):
        name = match.group(0)
        value = values[name]
        if name in quoted_variables and not inside_regex(match.start()):
            value = f"'{value}'"
        chunks.append(query[cursor:match.start()])
        chunks.append(value)
        cursor = match.end()
    chunks.append(query[cursor:])
    return "".join(chunks)


def innermost_query(query):
    """Para subconsultas valida solo la interna: los campos de la externa son
    alias de la interna (p. ej. sum("last") sobre last("errors")), no fields."""
    match = SUBQUERY_PATTERN.search(query)
    return match.group("inner") if match else query


def referenced_fields(query):
    return set(FIELD_PATTERN.findall(query))


def referenced_group_by_tags(query):
    match = GROUP_BY_PATTERN.search(query)
    if not match:
        return set()
    return set(QUOTED_PATTERN.findall(match.group("clause")))


def check_schema(client, query, location, report):
    """Verifica fields y tags del GROUP BY contra el schema real."""
    target = innermost_query(query)
    sources = FROM_PATTERN.findall(target)
    if len(sources) != 1:
        return
    _, retention_policy, measurement = sources[0]
    fields, tags = client.schema(retention_policy, measurement)
    if not fields and not tags:
        report.skip(location, f"measurement '{measurement}' no existe en este InfluxDB")
        return
    for field in sorted(referenced_fields(target) - fields):
        report.fail(location, f"field inexistente '{field}' en '{measurement}' (panel vacío)")
    for tag in sorted(referenced_group_by_tags(target) - tags):
        report.fail(location, f"tag inexistente '{tag}' en GROUP BY de '{measurement}' (serie falsa)")


def check_execution(client, query, location, report):
    _, error = client.query(query)
    if error:
        report.fail(location, f"InfluxDB rechaza la query: {error}")
        return False
    return True


def validate_templates(filename, dashboard, client, values, quoted, report):
    """Las meta queries de las template vars tambien se ejecutan: si fallan, el
    dropdown sale vacio y el dashboard queda inutilizable (no da error visible)."""
    for template in dashboard.get("templates", []):
        statement = (template.get("query") or {}).get("influxql")
        if not statement:
            continue
        location = f"{filename} / template {template.get('tempVar')}"
        report.checked += 1
        check_execution(client, interpolate(statement, values, quoted), location, report)


def validate_cells(filename, dashboard, client, values, quoted, report):
    for cell in dashboard.get("cells", []):
        for index, query in enumerate(cell.get("queries", [])):
            if query.get("type") != "influxql":
                continue
            location = f"{filename} / {cell.get('name')} / q{index}"
            resolved = interpolate(query.get("query") or "", values, quoted)
            report.checked += 1
            if check_execution(client, resolved, location, report):
                check_schema(client, resolved, location, report)


def validate_dashboard(path, client, values, report):
    filename = os.path.basename(path)
    with open(path, encoding="utf-8") as handle:
        dashboard = json.load(handle)["dashboard"]
    # Las variables declaradas son tagValues -> se interpolan entrecomilladas
    # fuera de regex; las internas (:interval:, :dashboardTime:...) no.
    quoted = {template.get("tempVar") for template in dashboard.get("templates", [])}
    validate_templates(filename, dashboard, client, values, quoted, report)
    validate_cells(filename, dashboard, client, values, quoted, report)


def parse_arguments():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default="http://localhost:8086", help="InfluxDB de monitorización")
    parser.add_argument("--database", default="telegraf")
    parser.add_argument("--instance", default="kapacitor-01", help="valor para :instance:")
    parser.add_argument("--task", default="03_availability", help="valor para :task:")
    parser.add_argument("--interval", default="1m", help="valor para :interval:")
    parser.add_argument("--start", default="now() - 1h", help="valor para :dashboardTime:")
    parser.add_argument("--end", default="now()", help="valor para :upperDashboardTime:")
    return parser.parse_args()


def template_values(arguments):
    return {
        ":dashboardTime:": arguments.start,
        ":upperDashboardTime:": arguments.end,
        ":interval:": arguments.interval,
        ":instance:": arguments.instance,
        ":task:": arguments.task,
    }


def print_report(report, arguments):
    print(
        f"Ejecutadas {report.checked} query(s) contra {arguments.url} "
        f"[instance={arguments.instance} task={arguments.task} interval={arguments.interval}]"
    )
    for skip in report.skips:
        print(f"SKIP  {skip}")
    for error in report.errors:
        print(f"FAIL  {error}")
    print("OK: sin errores" if report.ok else f"{len(report.errors)} error(es)")


def main():
    arguments = parse_arguments()
    client = InfluxClient(arguments.url, arguments.database)
    report = Report()
    paths = sorted(glob.glob(os.path.join(DASHBOARDS_DIR, "*.json")))
    if not paths:
        print(f"FAIL  no hay dashboards en {DASHBOARDS_DIR}")
        return 1
    for path in paths:
        validate_dashboard(path, client, template_values(arguments), report)
    print_report(report, arguments)
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
