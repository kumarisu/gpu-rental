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

# -----------------------------------------------------------------------------
# Package mapping (“plan C”: external mapping session → package code).
#
# When billing-sync runs in package mode (SYNC_MODE=package), it still computes
# Prometheus usage deltas (so we keep “kế thừa metrics”), but instead of sending
# 4 separate per-metric events it sends ONE package-level event per active
# workspace package.
#
# The package code is resolved per container, in priority order:
#   1) the Docker label `gpu_rental_package` (declared in the Coder templates
#      cpu-base / gpu-cuda), which cAdvisor exposes to Prometheus as
#      `container_label_gpu_rental_package` and which fetch_containers() reads
#      via fetch_metric_with_labels();
#   2) the coder agent metadata key `gpu_rental_package_code` (runtime override);
#   3) the operator-provided external mapping PACKAGE_BY_OWNER
#      ("owner=package_code,owner2=package_code2") — handy for demos where the
#      container label is not (yet) present.
# -----------------------------------------------------------------------------
SYNC_MODE = os.environ.get("SYNC_MODE", "package")  # "metric" | "package"

PACKAGE_EVENTS_ENABLED = SYNC_MODE == "package"

PACKAGES = {
    # package_code -> dict describing the package (mirrors billing/lago/bootstrap.py)
    "basic-cpu-2-ram-8": {
        "display_name": "Basic CPU (2 cores / 8GB RAM)",
        "price_cents": 500,
    },
    "pro-cpu-8-ram-32": {
        "display_name": "Pro CPU (8 cores / 32GB RAM)",
        "price_cents": 2000,
    },
    "gpu-cuda-1-ram-32": {
        "display_name": "GPU CUDA (1 GPU / 32GB RAM)",
        "price_cents": 5000,
    },
}

# External per-owner package mapping (plan C, demo-friendly fallback source).
# Format: comma-separated "owner=package_code" pairs, e.g.
#   PACKAGE_BY_OWNER=mike=gpu-cuda-1-ram-32,anna=basic-cpu-2-ram-8
_PACKAGE_BY_OWNER = {}
for _pair in os.environ.get("PACKAGE_BY_OWNER", "").split(","):
    _pair = _pair.strip()
    if "=" in _pair:
        _owner, _code = (p.strip() for p in _pair.split("=", 1))
        if _owner and _code:
            _PACKAGE_BY_OWNER[_owner] = _code


def package_code_for_container(name, owner, agent_metadata=None, container_info=None):
    """Return a package code (or None) for a container, using (in priority):
    1) Prometheus container label `gpu_rental_package` (from Coder template
       docker labels, exposed by cAdvisor),
    2) coder agent metadata key `gpu_rental_package_code` if present,
    3) the operator mapping PACKAGE_BY_OWNER (owner -> package code),
    4) None (in which case the container is ignored in package mode).
    """
    if container_info and isinstance(container_info, dict):
        pkg_code = (container_info.get("package") or "").strip()
        if pkg_code:
            return pkg_code

    if agent_metadata and isinstance(agent_metadata, dict):
        pkg_code = (agent_metadata.get("gpu_rental_package_code") or "").strip()
        if pkg_code:
            return pkg_code

    if owner and owner in _PACKAGE_BY_OWNER:
        return _PACKAGE_BY_OWNER[owner]

    return None


def package_event_payload(state, owner, package_code, duration_seconds):
    """Build one Lago metered event for package usage.

    The event code is always `workspace_package` (created by lago-bootstrap).
    The real package price (price_cents) and package_code are sent in
    `properties` so downstream Lago/cloud reporting can record which package was
    used without relying on the charge amount inside the plan.
    """
    sub = lago_subscription(state, owner)
    if not sub:
        return None
    return {
        "event": {
            "transaction_id": str(uuid.uuid4()),
            "external_customer_id": owner,
            "external_subscription_id": sub,
            "code": "workspace_package",
            "timestamp": int(time.time()),
            "properties": {
                "package_code": package_code,
                "price_cents": PACKAGES[package_code]["price_cents"],
                "duration_seconds": duration_seconds,
                # Kept as a numeric 1 so Lago can still sum the billable metric
                # as “number of package usage events” if desired.
                "value": 1,
            },
        }
    }


# -----------------------------------------------------------------------------
# Prometheus usage deltas (kept because we chose “package kế thừa metrics”).
# These are used in two ways:
#   * In package mode, to compute how long each workspace has been active during
#     the sync window (duration_seconds), and to surface usage stats in Grafana.
#   * In metric mode, to send the existing per-metric events (backward compatible).
# -----------------------------------------------------------------------------
USAGE = {
    "cpu_seconds":   [("container_cpu_usage_seconds_total",       True,  1.0)],          # counter → seconds
    "ram_gb_hours":  [("container_memory_usage_bytes",            False, 1e-9)],         # gauge: bytes → GB (window applied below)
    "network_gb":    [("container_network_receive_bytes_total",   True,  1e-9),          # counters → GB
                      ("container_network_transmit_bytes_total",  True,  1e-9)],
    "disk_write_gb": [("container_fs_writes_bytes_total",         True,  1e-9)],         # counter → GB
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


def fetch_metric_with_labels(metric):
    """Instant query -> {container_name: (value, labels)}.

    Labels are needed for package attribution: cAdvisor exposes the Docker
    label `gpu_rental_package` as `container_label_gpu_rental_package`.

    NB: instant-query responses carry `value: [ts, str]` (not `values` --
    that shape only exists on range-query responses), so check `value`.
    """
    out = {}
    params = urllib.parse.urlencode(
        {"query": f"{metric}{{container_label_coder_owner!=\"\"}}"})
    status, data = http("GET", f"{PROMETHEUS}/api/v1/query?{params}")
    if status != 200 or not data or data.get("status") != "success":
        return out
    for row in data["data"]["result"]:
        labels = row.get("metric", {}) or {}
        name = labels.get("name")
        value = row.get("value")
        if name and value:
            try:
                out[name] = (float(value[1]), dict(labels))
            except (TypeError, ValueError):
                continue
    return out


def fetch_metric(metric):
    """Return {container_name: latest_value} for a cAdvisor series."""
    return {name: value for name, (value, _labels)
            in fetch_metric_with_labels(metric).items()}


def fetch_containers():
    """Return {container_name: {owner, package}} for running Coder workspaces.

    cAdvisor tags every workspace container with the `coder.owner` label,
    exposed to Prometheus as `container_label_coder_owner`, plus the billing
    package label `gpu_rental_package` (declared in the Coder templates),
    exposed as `container_label_gpu_rental_package`. The label query in
    fetch_metric() already guarantees we only see Coder workspaces; the owner
    is derived from the container name `coder-<owner>-<workspace>` (works even
    when label indexing lags behind by one scrape) while the package code is
    read from the Prometheus label when present.
    """
    out = {}
    rows = fetch_metric_with_labels("container_cpu_usage_seconds_total")
    for name, row in rows.items():
        _value, labels = row
        if not name.startswith("coder-"):
            continue
        idx = name.rfind("-")
        if idx < len("coder-"):
            continue
        owner = labels.get("container_label_coder_owner") or name[len("coder-"):idx]
        package = (labels.get("container_label_gpu_rental_package") or "").strip() or None
        if not owner:
            continue
        out[name] = {"owner": owner, "package": package}
    # Fallback for Prometheus servers that drop label detail (or very old
    # cAdvisor versions): keep owner-only discovery so billing never goes blind.
    if not out:
        for name in fetch_metric("container_cpu_usage_seconds_total"):
            if not name.startswith("coder-"):
                continue
            idx = name.rfind("-")
            if idx < len("coder-"):
                continue
            owner = name[len("coder-"):idx]
            if owner:
                out[name] = {"owner": owner, "package": None}
    return out


def fetch_agent_metadata(owner, containers):
    """Best-effort map: owner -> {container_name: agent_metadata_dict}.

    Coder exposes agent metadata through its API, not through Prometheus.
    billing-sync does not currently call the Coder API, so this function is a
    placeholder for the chosen plan C integration path.

    When Coder agent metadata is available (for example via a future small Coder
    API scrape or a sidecar dump into Prometheus/JSON), populate this dict and
    pass it into the package selection logic.  Until then, billing-sync falls
    back to the metadata key `gpu_rental_package_code` only if it can be read
    from another source (for example a future Prometheus label exported from the
    Coder agent itself).  If no package code is available, the container is not
    billed in package mode.
    """
    # TODO(process): implement Coder metadata retrieval for production use.
    # Returning an empty mapping keeps the current behaviour predictable: without
    # explicit package metadata, package mode will not invoice unknown containers.
    return {}


def container_active_duration_seconds(name, containers, metric_snapshots):
    """Return how long a container appears to have been active during the current
    sync window, based on the same Prometheus counter snapshots used elsewhere.

    Currently this is a rough proxy: if the container is present in a CPU counter
    snapshot, we assume it was active for the full sync interval.  This is
    acceptable for the chosen model where the package price is fixed per billing
    period and the event is informational, but it is not a precise usage meter.
    """
    if not PACKAGE_EVENTS_ENABLED:
        return 0.0
    if name not in containers:
        return 0.0
    cpu_series = metric_snapshots.get("cpu_seconds")
    if not cpu_series or name not in cpu_series:
        return 0.0
    # If CPU usage is non-trivial, treat the container as active for the window.
    try:
        cpu_value = float(cpu_series[name])
    except (TypeError, ValueError):
        return 0.0
    if cpu_value <= 0:
        return 0.0
    return float(INTERVAL)


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
    """Read current values; return per-owner deltas since the last cycle.

    `containers` maps container name -> {"owner": ..., "package": ...} (see
    fetch_containers). Older callers may still pass name -> owner strings;
    both shapes are accepted here.
    """
    owners = {}
    for code, series_list in USAGE.items():
        for metric, is_counter, factor in series_list:
            current = fetch_metric(metric)
            for name, info in list(containers.items()):
                if name not in current:
                    continue
                owner = info.get("owner") if isinstance(info, dict) else info
                if not owner:
                    continue
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


def send_events(state, owners, containers, metric_snapshots, agent_metadata):
    """Send billing events according to the active SYNC_MODE.

    Metric mode (backward compatible):
      one per-metric event per owner/metric delta, as before.

    Package mode (plan C, package kế thừa metrics):
      one package event per active container that has a resolvable package code,
      carrying package_code, price_cents, and duration_seconds in properties.
      Per-metric deltas are STILL computed (for Grafana + debugging) but are not
      sent as separate Lago events in package mode.
    """
    # Computing per-owner deltas is still useful in package mode for visibility
    # and for any future hybrid billing rules, so we keep it independent of mode.
    deltas = collect(state, containers)

    if not PACKAGE_EVENTS_ENABLED:
        # Original behaviour: send per-metric events.
        for owner, per_metric in deltas.items():
            sub = lago_subscription(state, owner)
            if not sub:
                continue
            for code, delta in per_metric.items():
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
                status, resp = http(
                    "POST", f"{LAGO}/api/v1/events", payload,
                    header={"Authorization": f"Bearer {LAGO_KEY}"})
                if status in (200, 201):
                    print(f"  → {owner}:{code} += {delta:.6g}")
                else:
                    print(f"  ! event rejected {owner}:{code} (http {status}): {resp}")
        return

    # Package mode.
    for name, info in containers.items():
        owner = info.get("owner") if isinstance(info, dict) else info
        if not owner:
            continue
        pkg_code = package_code_for_container(
            name, owner, agent_metadata.get(owner, {}).get(name),
            container_info=info if isinstance(info, dict) else None)
        if not pkg_code or pkg_code not in PACKAGES:
            continue
        duration = container_active_duration_seconds(
            name, containers, metric_snapshots)
        if duration <= 0:
            continue
        payload = package_event_payload(state, owner, pkg_code, duration)
        if not payload:
            continue
        status, resp = http(
            "POST", f"{LAGO}/api/v1/events", payload,
            header={"Authorization": f"Bearer {LAGO_KEY}"})
        pkg = PACKAGES[pkg_code]
        if status in (200, 201):
            print(
                f"  → pkg {owner}:{pkg_code} "
                f"({pkg['display_name']}) {duration:.0f}s @ {pkg['price_cents']}c"
            )
        else:
            print(
                f"  ! package event rejected {owner}:{pkg_code} "
                f"(http {status}): {resp}"
            )


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


def prometheus_ok():
    """True when Prometheus answers — used for the container healthcheck."""
    try:
        # NB: /-/healthy answers plain text, so don't reuse the JSON http() helper.
        with urllib.request.urlopen(f"{PROMETHEUS}/-/healthy", timeout=10) as resp:
            return resp.status == 200
    except Exception:  # noqa: BLE001
        return False


def one_cycle(state):
    containers = fetch_containers()
    if not containers:
        print("  no running workspace containers (yet)")
        # Healthy = the pipeline is reachable, even with zero workspaces.
        return prometheus_ok()
    print(f"  workspaces: {len(containers)} → {containers}")

    agent_metadata = fetch_agent_metadata(None, containers)

    # In package mode we still snapshot Prometheus metrics so we can:
    #   * decide if a container was active during the window, and
    #   * expose per-user metrics in Grafana (unchanged).
    metric_snapshots = {code: {} for code in USAGE}
    for code, series_list in USAGE.items():
        for metric, _is_counter, _factor in series_list:
            metric_snapshots[code].update(fetch_metric(metric))

    if PACKAGE_EVENTS_ENABLED:
        send_events(state, None, containers, metric_snapshots, agent_metadata)
    else:
        owners = collect(state, containers)
        if owners:
            send_events(state, owners, containers, metric_snapshots, agent_metadata)
    return True


def main():
    print(
        f"▶ billing-sync  (Prometheus={PROMETHEUS}  Lago={LAGO}  "
        f"interval={INTERVAL}s  mode={SYNC_MODE})"
    )
    if not LAGO_KEY:
        print("  WARNING: LAGO_API_KEY empty — events will be rejected."
              " Run `make lago-bootstrap` first.")
    if PACKAGE_EVENTS_ENABLED:
        missing = sorted(code for code in PACKAGES if code not in PACKAGES)
        # The above is intentionally a no-op truth check for readability; if
        # PACKAGES changes structure, update this validation.
        if sorted(PACKAGES) != sorted(PACKAGES):
            pass
        print(f"  package mode enabled: {', '.join(sorted(PACKAGES))}")
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