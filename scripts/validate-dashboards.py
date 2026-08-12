#!/usr/bin/env python3
"""Validador estático de los dashboards de Chronograf (stdlib, sin red).

Comprueba invariantes que rompen dashboards silenciosamente:
- JSON parseable con `dashboard.name` y `dashboard.cells`.
- Coherencia `queryConfig.rawText` == `query` (el bug de editar uno y olvidar el otro).
- Comillas manuales alrededor de una template var (`':instance:'`): Chronograf ya
  entrecomilla el valor fuera de los regex, asi que quedaria `''valor''` y rompe
  el panel o el dropdown en el navegador, sin error visible en el JSON.
- STRICT en 05: template dinamica `:instance:` y filtro de instancia en TODA query influxql.
- WARN en 02/03/04: queries influxql sin filtro de instancia (candidatas a follow-up).

Salida en español, exit 0 si no hay errores, 1 si los hay.
"""

import glob
import json
import os
import re
import sys

DASHBOARDS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dashboards")
INSTANCE_FILTER = '"instance" =~ /^:instance:$/'
INSTANCE_TEMPLATE = ":instance:"
# "influxql" = meta query personalizada (permite encadenar variables, como el
# :task: del 06). Vale igual que tagValues como origen dinamico de valores.
INSTANCE_TEMPLATE_TYPES = ("tagValues", "influxql")
# Chronograf entrecomilla solo el valor de las tagValues fuera de los regex, asi
# que ':var:' escrito a mano acaba como ''valor'' -> InfluxQL invalido.
MANUAL_QUOTING_PATTERN = re.compile(r"'(:[\w-]+:)'")
STRICT_PREFIX = "05"
WARN_PREFIXES = ("02", "03", "04")


class Report:
    def __init__(self):
        self.errors = []
        self.warnings = []

    def fail(self, filename, message):
        self.errors.append(f"{filename}: {message}")

    def warn(self, filename, message):
        self.warnings.append(f"{filename}: {message}")

    @property
    def ok(self):
        return not self.errors


def discover_dashboards():
    return sorted(glob.glob(os.path.join(DASHBOARDS_DIR, "*.json")))


def load_dashboard(path, report):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as error:
        report.fail(os.path.basename(path), f"no parsea ({error})")
        return None


def iter_queries(dashboard):
    for cell in dashboard.get("cells", []):
        for query in cell.get("queries", []):
            yield query


def has_instance_template(dashboard):
    return any(
        template.get("type") in INSTANCE_TEMPLATE_TYPES and template.get("tempVar") == INSTANCE_TEMPLATE
        for template in dashboard.get("templates", [])
    )


def check_structure(filename, dashboard, report):
    if not isinstance(dashboard.get("name"), str) or not dashboard["name"].strip():
        report.fail(filename, "falta dashboard.name")
    if "cells" not in dashboard:
        report.fail(filename, "falta dashboard.cells")


def check_rawtext_matches_query(filename, dashboard, report):
    for index, query in enumerate(iter_queries(dashboard)):
        raw_text = query.get("queryConfig", {}).get("rawText")
        if raw_text is not None and raw_text != query.get("query"):
            report.fail(filename, f"query #{index}: rawText != query (edición desincronizada)")


def iter_statements(dashboard):
    """Todo el InfluxQL del dashboard: queries de celda y meta queries de las
    template vars (estas ultimas rompen el dropdown sin dar error visible)."""
    for cell in dashboard.get("cells", []):
        for index, query in enumerate(cell.get("queries", [])):
            yield f"celda '{cell.get('name')}' q{index}", query.get("query") or ""
    for template in dashboard.get("templates", []):
        statement = (template.get("query") or {}).get("influxql")
        if statement:
            yield f"template {template.get('tempVar')}", statement


def check_manual_quoting(filename, dashboard, report):
    for location, statement in iter_statements(dashboard):
        for variable in MANUAL_QUOTING_PATTERN.findall(statement):
            report.fail(
                filename,
                f"{location}: {variable} entre comillas manuales; Chronograf ya las "
                f"anade fuera de regex y quedaria ''valor''. Usa =~ /^{variable}$/",
            )


def influxql_queries_without_filter(dashboard):
    for query in iter_queries(dashboard):
        if query.get("type") == "influxql" and INSTANCE_FILTER not in (query.get("query") or ""):
            yield query


def check_strict_instance_filter(filename, dashboard, report):
    if not has_instance_template(dashboard):
        report.fail(filename, f"falta template tagValues {INSTANCE_TEMPLATE}")
    unfiltered = sum(1 for _ in influxql_queries_without_filter(dashboard))
    if unfiltered:
        report.fail(filename, f"{unfiltered} query(s) influxql sin filtro de instancia")


def warn_missing_instance_filter(filename, dashboard, report):
    unfiltered = sum(1 for _ in influxql_queries_without_filter(dashboard))
    if unfiltered:
        report.warn(filename, f"{unfiltered} query(s) influxql sin filtro de instancia")


def validate_dashboard(path, report):
    filename = os.path.basename(path)
    document = load_dashboard(path, report)
    if document is None:
        return
    dashboard = document.get("dashboard")
    if not isinstance(dashboard, dict):
        report.fail(filename, "falta el objeto dashboard")
        return
    check_structure(filename, dashboard, report)
    check_rawtext_matches_query(filename, dashboard, report)
    check_manual_quoting(filename, dashboard, report)
    if filename.startswith(STRICT_PREFIX):
        check_strict_instance_filter(filename, dashboard, report)
    elif filename.startswith(WARN_PREFIXES):
        warn_missing_instance_filter(filename, dashboard, report)


def print_report(paths, report):
    print(f"Validando {len(paths)} dashboard(s) en {DASHBOARDS_DIR}")
    for warning in report.warnings:
        print(f"WARN  {warning}")
    for error in report.errors:
        print(f"FAIL  {error}")
    if report.ok:
        print("OK: sin errores")


def main():
    paths = discover_dashboards()
    report = Report()
    if not paths:
        print(f"FAIL  no hay dashboards en {DASHBOARDS_DIR}")
        return 1
    for path in paths:
        validate_dashboard(path, report)
    print_report(paths, report)
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
