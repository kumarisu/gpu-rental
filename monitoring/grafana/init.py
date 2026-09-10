#!/usr/bin/env python3
"""Idempotent Grafana provisioning for GPU Rental.

Wait for Grafana, login, resolve the auto-provisioned datasource UIDs
(Prometheus, Loki) and import the dashboards in /dashboards.

The dashboard JSON may use the placeholders __DS_PROMETHEUS__ / __DS_LOKI__ —
they are replaced with the real UIDs before import.

Run via: make grafana-init   (docker compose --profile init run --rm grafana-init)
"""
import glob
import json
import os
import time
import urllib.error
import urllib.request

GRAFANA = os.environ.get("GRAFANA_URL", "http://grafana:3000").rstrip("/")
USER = os.environ.get("GRAFANA_ADMIN_USER", "admin")
PASSWORD = os.environ["GRAFANA_ADMIN_PASSWORD"]
DASHBOARD_DIR = "/dashboards"


def http(method, url, data=None, cookie=None, timeout=10):
    headers = {"Content-Type": "application/json"}
    if cookie:
        headers["Cookie"] = cookie
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None), resp.headers.get("Set-Cookie", "")
    except urllib.error.HTTPError as e:
        raw = e.read() if e.fp else b""
        return e.code, (json.loads(raw) if raw.startswith(b"{") else {}), ""


def wait_ready():
    for _ in range(120):
        status, _, _ = http("GET", f"{GRAFANA}/api/health")
        if status == 200:
            print("  grafana is ready")
            return
        time.sleep(5)
    raise SystemExit("Grafana did not become ready in time")


def login():
    status, data, cookie = http("POST", f"{GRAFANA}/api/login",
                                {"user": USER, "password": PASSWORD})
    if status != 200:
        raise SystemExit(f"Grafana login failed (http {status})")
    # eg: grafana_session=...; Path=/; HttpOnly
    cookie = cookie.split(";")[0]
    print(f"  logged in as {USER}")
    return cookie


def datasource_uids(cookie):
    status, data, _ = http("GET", f"{GRAFANA}/api/datasources", cookie=cookie)
    if status != 200:
        raise SystemExit(f"cannot list datasources (http {status})")
    return {d.get("name"): d.get("uid") for d in data}


def delete_existing(cookie, title):
    status, data, _ = http("GET", f"{GRAFANA}/api/search?query={title}", cookie=cookie)
    if status != 200:
        return
    for item in data or []:
        if item.get("type") == "dash-db" and item.get("title") == title:
            http("DELETE", f"{GRAFANA}/api/dashboards/uid/{item['uid']}", cookie=cookie)
            print(f"  removed previous dashboard '{title}'")


def import_dashboard(path, uids, cookie):
    with open(path, encoding="utf-8") as fh:
        dash = fh.read()
    for placeholder, uid in uids.items():
        dash = dash.replace(placeholder, uid or "")
    payload = json.loads(dash)
    title = payload.get("dashboard", {}).get("title", "?")
    delete_existing(cookie, title)
    status, data, _ = http("POST", f"{GRAFANA}/api/dashboards/db", payload, cookie)
    if status == 200:
        print(f"  imported dashboard '{title}'")
    else:
        print(f"  ! failed to import '{title}' (http {status}): {data}")


def main():
    print("▶ Grafana provisioning")
    wait_ready()
    cookie = login()
    uids = datasource_uids(cookie)
    print(f"  datasource uids: {uids}")

    tokens = {
        "__DS_PROMETHEUS__": uids.get("Prometheus"),
        "__DS_LOKI__": uids.get("Loki"),
    }
    for path in sorted(glob.glob(f"{DASHBOARD_DIR}/*.json")):
        import_dashboard(path, tokens, cookie)
    print("✔ Grafana provisioning done.")


if __name__ == "__main__":
    main()