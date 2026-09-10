#!/usr/bin/env python3
"""
billing-sync — Prometheus usage deltas → Lago metered events (per user).

Every SYNC_INTERVAL_SECONDS it:

  1. asks Prometheus which Coder workspace containers are running
     (cAdvisor metric `container_*` filtered by the `coder.owner` label);
  2. computes the usage delta since the previous cycle:
       cpu_seconds   : increase of container_cpu_usage_seconds_total
       ram_gb_hours  : current RSS (GB) held during the window
       network_gb    : increase of rx+tx bytes counters
       disk_write_gb : increase of fs-write bytes counter
  3. aggregates deltas per OWNER (a user may run several workspaces);
  4. POSTs one metered event per billable metric to Lago:
       POST /api/v1/events   {"event": {transaction_id, external_customer_id,
                              external_subscription_id, code, timestamp,
                              properties: {value: <delta>}}}

State (previous counter values + cached Lago ids) is kept in /data/state.db
so restarts do not lose or double-count usage.
"""
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

PROMETHEUS = os.environ["PROMETHEUS_URL"].rstrip("/")
LAGO = os.environ["LAGO_API_URL"].rstrip("/")
LAGO_KEY = os.environ.get("LAGO_API_KEY", "")
PLAN_CODE = os.environ.get("LAGO_PLAN_CODE", "gpu-usage")
EMAIL_DOMAIN = os.environ.get("LAGO_EMAIL_DOMAIN", "gpu.local")
INTERVAL = int(os.environ.get("SYNC_INTERVAL_SECONDS", "60"))
MIN_DELTA = float(os.environ.get("SYNC_MIN_DELTA", "0.000001"))
STATE_DB = os.environ.get("SYNC_STATE_DB", "/data/state.db")

# code -> list of (prometheus metric, is_counter, unit-conversion)
USAGE = {
    "cpu_seconds":   [("container_cpu_usage_seconds_total",       True,  1.0)],          # counter → seconds
    "ram_gb_hours":  [("container_memory_usage_bytes",            False, 1e-9)],         # gauge: bytes → GB (window applied below)
    "network_gb":    [("container_network_receive_bytes_total",   True,  1e-9),          # counters → GB
                      ("container_network_transmit_bytes_total",  True,  1e-9)],
    "disk_write_gb": [("container_fs_write_bytes_total",          True,  1e-9)],         # counter → GB
}


def http(method, url, data=None, header=None, timeout=20):
    headers = {"Content-Type": "application/json"}
    if header:
        headers.update(header)
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read() if e.fp else b""
        return e.code, (json.loads(raw) if raw else {})


def fetch_metric(metric):
    """Return {container_name: latest_value} for a cAdvisor series."""
    out = {}
    params = urllib.parse.urlencode(
        {"query": f"{metric}{{container_label_coder_owner!=\"\"}}"})
    status, data = http("GET", f"{PROMETHEUS}/api/v1/query?{params}")
    if status != 200 or not data or data.get("status") != "success":
        return out
    for row in data["data"]["result"]:
        name = row.get("metric", {}).get("name")
        if name and row.get("values"):
            out[name] = float(row["values"][-1][1])
    return out


def fetch_containers():
    """Return {container_name: owner} for running Coder workspaces.

    cAdvisor tags every workspace container with the `coder.owner` label,
    exposed to Prometheus as `container_label_coder_owner`.  The label query in
    fetch_metric() already guarantees we only see Coder workspaces; the owner is
    derived from the container name `coder-<owner>-<workspace>` (works even when
    label indexing lags behind by one scrape).
    """
    out = {}
    for name in fetch_metric("container_cpu_usage_seconds_total"):
        if not name.startswith("coder-"):
            continue
        idx = name.rfind("-")
        if idx < len("coder-"):
            continue
        owner = name[len("coder-"):idx]
        out[name] = owner or None
    return {k: v for k, v in out.items() if v}


class State:
    """Tiny sqlite-backed state: last counter per container + cached Lago ids."""

    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS counters (k TEXT PRIMARY KEY, v REAL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)")
        self.db.commit()

    def counter(self, key, default=None):
        row = self.db.execute("SELECT v FROM counters WHERE k=?", (key,)).fetchone()
        return row[0] if row else default

    def set_counter(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO counters (k, v) VALUES (?,?)", (key, value))
        self.db.commit()

    def kv_get(self, key):
        row = self.db.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
        return row[0] if row else None

    def kv_set(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO kv (k, v) VALUES (?,?)", (key, value))
        self.db.commit()


def collect(state, containers):
    """Read current values; return per-owner deltas since the last cycle."""
    owners = {}
    for code, series_list in USAGE.items():
        for metric, is_counter, factor in series_list:
            current = fetch_metric(metric)
            for name in list(containers):
                if name not in current:
                    continue
                owner = containers[name]
                value = current[name] * factor
                key = f"{name}:{code}"
                prev = state.counter(key)
                if prev is None:
                    # first sighting — record the baseline, nothing to bill yet
                    state.set_counter(key, value)
                    continue
                if is_counter:
                    delta = value - prev
                    if delta < 0:            # container restart → counter reset
                        delta = 0.0
                else:
                    delta = value * (INTERVAL / 3600.0)   # gauge held during the window
                if delta > 0:
                    entry = owners.setdefault(owner, {})
                    entry[code] = entry.get(code, 0.0) + delta
                state.set_counter(key, value)
    return owners
def lago_subscription(state, owner):
    """Get/create the Lago subscription id for an owner (self-healing)."""
    cached = state.kv_get(f"sub:{owner}")
    if cached:
        return cached
    auth = {"Authorization": f"Bearer {LAGO_KEY}"}
    status, _ = http("GET", f"{LAGO}/api/v1/customers/{owner}", header=auth)
    if status != 200:
        payload = {"customer": {"external_id": owner, "name": owner,
                                "email": f"{owner}@{EMAIL_DOMAIN}",
                                "country": "US", "currency": "USD"}}
        status, _ = http("POST", f"{LAGO}/api/v1/customers", payload, header=auth)
        if status not in (200, 201):
            print(f"  ! cannot create customer '{owner}' (http {status})")
            return None
    status, data = http("GET", f"{LAGO}/api/v1/subscriptions?external_customer_id={owner}",
                        header=auth)
    subs = (data or {}).get("subscriptions", []) if status == 200 else []
    if not subs:
        payload = {"subscription": {"external_customer_id": owner, "plan_code": PLAN_CODE,
                                    "name": "usage", "billing_time": "calendar"}}
        status, data = http("POST", f"{LAGO}/api/v1/subscriptions", payload, header=auth)
        if status not in (200, 201):
            print(f"  ! cannot subscribe '{owner}' (http {status})")
            return None
        subs = [(data or {}).get("subscription", {})]
    sub = (subs[0] or {}).get("external_id")
    if sub:
        state.kv_set(f"sub:{owner}", sub)
    return sub


def send_events(state, owners):
    for owner, deltas in owners.items():
        sub = lago_subscription(state, owner)
        if not sub:
            continue
        for code, delta in deltas.items():
            if delta < MIN_DELTA:
                continue
            payload = {"event": {
                "transaction_id": str(uuid.uuid4()),
                "external_customer_id": owner,
                "external_subscription_id": sub,
                "code": code,
                "timestamp": int(time.time()),
                "properties": {"value": round(delta, 9)},
            }}
            status, resp = http("POST", f"{LAGO}/api/v1/events", payload,
                                header={"Authorization": f"Bearer {LAGO_KEY}"})
            if status in (200, 201):
                print(f"  → {owner}:{code} += {delta:.6g}")
            else:
                print(f"  ! event rejected {owner}:{code} (http {status}): {resp}")


def one_cycle(state):
    containers = fetch_containers()
    if not containers:
        print("  no running workspace containers (yet)")
        return False
    print(f"  workspaces: {len(containers)} → {containers}")
    owners = collect(state, containers)
    if owners:
        send_events(state, owners)
    return True


def main():
    print(f"▶ billing-sync  (Prometheus={PROMETHEUS}  Lago={LAGO}  interval={INTERVAL}s)")
    if not LAGO_KEY:
        print("  WARNING: LAGO_API_KEY empty — events will be rejected."
              " Run `make lago-bootstrap` first.")
    state = State(STATE_DB)
    while True:
        try:
            if one_cycle(state):
                with open("/tmp/sync-ok", "w", encoding="utf-8") as fh:
                    fh.write(str(int(time.time())))
        except Exception as exc:  # noqa: BLE001
            print(f"  cycle error: {exc}")
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()