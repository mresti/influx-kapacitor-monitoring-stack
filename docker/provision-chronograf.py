#!/usr/bin/env python3
"""One-shot Chronograf provisioner (stdlib only).

Espera a que Chronograf responda, crea el source de InfluxDB (idempotente por
URL) e importa los 5 dashboards de /dashboards (idempotente por nombre).
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

CHRONOGRAF_URL = os.environ.get("CHRONOGRAF_URL", "http://chronograf:8888").rstrip("/")
INFLUXDB_URL = os.environ.get("INFLUXDB_URL", "http://influxdb:8086")
DASHBOARDS_DIR = os.environ.get("DASHBOARDS_DIR", "/dashboards")
SOURCE_NAME = "influxdb-01"


def request(method, path, payload=None):
    url = f"{CHRONOGRAF_URL}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = resp.read().decode()
        return resp.status, json.loads(body) if body else {}


def wait_for_chronograf():
    deadline = time.time() + 90
    while time.time() < deadline:
        try:
            request("GET", "/chronograf/v1/sources")
            print("provision: Chronograf disponible")
            return
        except (urllib.error.URLError, ConnectionError, OSError) as exc:
            print(f"provision: esperando a Chronograf... ({exc})")
            time.sleep(3)
    print("provision: ERROR Chronograf no respondio a tiempo", file=sys.stderr)
    sys.exit(1)


def ensure_source():
    _, data = request("GET", "/chronograf/v1/sources")
    for source in data.get("sources", []):
        if source.get("url") == INFLUXDB_URL or source.get("name") == SOURCE_NAME:
            print(f"provision: source ya existe (id={source.get('id')}), no se duplica")
            return
    payload = {
        "name": SOURCE_NAME,
        "type": "influx",
        "url": INFLUXDB_URL,
        "default": True,
        "telegraf": "telegraf",
    }
    status, created = request("POST", "/chronograf/v1/sources", payload)
    print(f"provision: source creado (status={status}, id={created.get('id')})")


def existing_dashboards_by_name():
    _, data = request("GET", "/chronograf/v1/dashboards")
    return {d.get("name"): d.get("id") for d in data.get("dashboards", [])}


def import_dashboards():
    existing = existing_dashboards_by_name()
    files = sorted(
        f for f in os.listdir(DASHBOARDS_DIR) if f.endswith(".json")
    )
    for filename in files:
        with open(os.path.join(DASHBOARDS_DIR, filename)) as fh:
            dashboard = json.load(fh)["dashboard"]
        name = dashboard["name"]
        payload = {
            "name": name,
            "cells": dashboard.get("cells", []),
            "templates": dashboard.get("templates", []),
        }
        if name in existing:
            dashboard_id = existing[name]
            status, _ = request(
                "PUT", f"/chronograf/v1/dashboards/{dashboard_id}", payload
            )
            print(f"provision: dashboard '{name}' actualizado (status={status})")
        else:
            status, _ = request("POST", "/chronograf/v1/dashboards", payload)
            print(f"provision: dashboard '{name}' importado (status={status})")


def main():
    wait_for_chronograf()
    ensure_source()
    import_dashboards()
    print("provision: completado")


if __name__ == "__main__":
    main()
