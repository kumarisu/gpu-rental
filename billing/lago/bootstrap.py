#!/usr/bin/env python3
"""Idempotent Lago bootstrap for the GPU Rental platform.

Creates (if missing):
  * billable metrics   cpu_seconds, ram_gb_hours, network_gb, disk_write_gb
  * a usage plan       (per_unit charges referencing the metrics)
  * one customer + subscription per demo user (LAGO_DEMO_USERS)

These are the targets of `billing-sync`, which pushes Prometheus deltas as
metered events (POST /api/v1/events, `properties.value` = unit quantity).

Run via: make lago-bootstrap   (docker compose --profile init run --rm lago-bootstrap)
"""
import json
import os
import time
import urllib.error
import urllib.request

API = os.environ["LAGO_API_INTERNAL"].rstrip("/")
API_KEY = os.environ.get("LAGO_API_KEY", "")
PLAN_CODE = os.environ.get("LAGO_PLAN_CODE", "gpu-usage")
PLAN_NAME = os.environ.get("LAGO_PLAN_NAME", "GPU Usage")
DEMO_USERS = [u.strip() for u in os.environ.get("LAGO_DEMO_USERS", "mike,anna").split(",") if u.strip()]
EMAIL_DOMAIN = os.environ.get("LAGO_EMAIL_DOMAIN", "gpu.local")

# -----------------------------------------------------------------------------
# Per-metric prices (kept for backwards compatibility / debugging / Grafana)
# -----------------------------------------------------------------------------
METRICS = {
    "cpu_seconds":   ("CPU time (seconds)",           "Sum of container CPU-seconds consumed by workspaces.",  "0.000002"),
    "ram_gb_hours":  ("RAM usage (GB.hours)",         "Average memory (GB) held for one hour.",               "0.000500"),
    "network_gb":    ("Network traffic (GB)",         "Bytes transferred in/out, in GB.",                     "0.020000"),
    "disk_write_gb": ("Disk writes (GB)",             "Bytes written by workspaces, in GB.",                  "0.001000"),
}


# -----------------------------------------------------------------------------
# Package definitions.
#
# Key       -> billable metric code used by billing-sync when it sends a package
#               event (event "code").
# display   -> human-readable package name used in logs / Grafana.
# price_cents -> flat price for the package (USD cents). In this bootstrap we
#               create a plan charge for the package metric but keep its amount
#               at 0; billing-sync sends the real price in the event properties
#               so pricing can change without re-running bootstrap.
# -----------------------------------------------------------------------------
PACKAGES = {
    # package code -> (display_name, price_cents)
    "basic-cpu-2-ram-8":  ("Basic CPU (2 cores / 8GB RAM)",  500),
    "pro-cpu-8-ram-32":   ("Pro CPU (8 cores / 32GB RAM)",   2000),
    "gpu-cuda-1-ram-32":  ("GPU CUDA (1 GPU / 32GB RAM)",    5000),
}


def http(method, path, data=None, timeout=15):
    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(f"{API}{path}", data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read() if e.fp else b""
        try:
            return e.code, (json.loads(raw) if raw else None)
        except json.JSONDecodeError:
            return e.code, raw.decode(errors="replace")


def wait_api():
    for _ in range(120):
        status, _ = http("GET", "/health", timeout=5)
        if status == 200:
            print("  lago api is ready")
            return
        time.sleep(5)
    raise SystemExit("Lago API did not become ready in time")


def check_key():
    if not API_KEY:
        raise SystemExit(
            "LAGO_ORG_API_KEY is empty. Either set LAGO_CREATE_ORG=true + LAGO_ORG_* "
            "in .env (auto-creates the org on first boot), or sign up at "
            f"{os.environ.get('LAGO_UI_URL', 'http://localhost:8580')} and copy the "
            "organisation API key from Developer → API keys into LAGO_ORG_API_KEY."
        )
    status, data = http("GET", "/api/v1/billable_metrics")
    if status in (401, 403):
        raise SystemExit(f"API key rejected (http {status}). Check LAGO_ORG_API_KEY.")
    print("  api key OK")


def ensure_billable_metrics():
    _, data = http("GET", "/api/v1/billable_metrics?page=1&per_page=100")
    existing = {m.get("code") for m in (data or {}).get("billable_metrics", [])}
    for code, (name, desc, _price) in METRICS.items():
        if code in existing:
            print(f"  billable_metric '{code}' exists")
            continue
        payload = {
            "billable_metric": {
                "name": name,
                "code": code,
                "description": desc,
                # NOTE: Lago >= v1.x uses *_agg enum names ("sum", "max"… are invalid)
                "aggregation_type": "sum_agg",
                "field_name": "value",
                "properties": {},
            }
        }
        status, resp = http("POST", "/api/v1/billable_metrics", payload)
        if status in (200, 201):
            print(f"  billable_metric '{code}' created (sum of properties.value)")
        else:
            print(f"  ! failed billable_metric '{code}' (http {status}): {resp}")

    # Package-level billable metric (workspace_package).
    pkg_metric_code = "workspace_package"
    if pkg_metric_code in existing:
        print(f"  billable_metric '{pkg_metric_code}' exists")
    else:
        pkg_payload = {
            "billable_metric": {
                "code": pkg_metric_code,
                "name": "Workspace package usage",
                "description": (
                    "Package-level usage events sent by billing-sync. "
                    "Event properties may include package_code, price_cents, "
                    "duration_seconds."
                ),
                "aggregation_type": "sum_agg",
                "field_name": "value",
                "properties": {},
            }
        }
        status, resp = http("POST", "/api/v1/billable_metrics", pkg_payload)
        if status in (200, 201):
            print(f"  billable_metric '{pkg_metric_code}' created")
        else:
            print(
                f"  ! failed billable_metric '{pkg_metric_code}' (http {status}): {resp}"
            )


def ensure_plan():
    _, data = http("GET", f"/api/v1/plans?code={PLAN_CODE}")
    if (data or {}).get("plans"):
        print(f"  plan '{PLAN_CODE}' exists")
        return
    # Newer Lago APIs require charges to reference the billable metric by its
    # UUID (billable_metric_id); billable_metric_code alone is no longer
    # resolved and yields a 404 billable_metric_not_found.
    _, metrics = http("GET", "/api/v1/billable_metrics?page=1&per_page=100")
    metric_ids = {m.get("code"): m.get("lago_id") or m.get("id")
                  for m in (metrics or {}).get("billable_metrics", [])}
    charges = []
    for code, (_n, _d, price) in METRICS.items():
        mid = metric_ids.get(code)
        if not mid:
            raise SystemExit(f"billable metric '{code}' not found — cannot create plan")
        charges.append({"billable_metric_id": mid,
                        "billable_metric_code": code,
                        # NOTE: 'per_unit' was removed; 'standard' charges a
                        # fixed amount per event unit, which is the same model.
                        "charge_model": "standard",
                        "properties": {"amount": price}})

    # Package-level charge referencing workspace_package.
    # Amount is set to 0 here intentionally because billing-sync sends the real
    # package price inside the event properties (price_cents). This keeps the
    # plan definition generic across packages and avoids rerunning bootstrap when
    # package prices change. If you prefer Lago to interpret prices directly,
    # replace this with per-package charges computed from PACKAGES below.
    pkg_mid = metric_ids.get("workspace_package")
    if pkg_mid:
        charges.append({"billable_metric_id": pkg_mid,
                        "billable_metric_code": "workspace_package",
                        "charge_model": "standard",
                        "properties": {"amount": "0"}})

    payload = {
        "plan": {
            "name": PLAN_NAME,
            "code": PLAN_CODE,
            "amount_cents": 0,
            "amount_currency": "USD",
            "interval": "monthly",
            "pay_in_advance": False,
            "charges": charges,
        }
    }
    status, resp = http("POST", "/api/v1/plans", payload)
    if status in (200, 201):
        print(f"  plan '{PLAN_CODE}' created with {len(charges)} charges")
    else:
        raise SystemExit(f"could not create plan (http {status}): {resp}")


def ensure_customer(external_id):
    status, _ = http("GET", f"/api/v1/customers/{external_id}")
    if status == 200:
        print(f"  customer '{external_id}' exists")
        return
    payload = {
        "customer": {
            "external_id": external_id,
            "name": external_id,
            "email": f"{external_id}@{EMAIL_DOMAIN}",
            "country": "US",
            "currency": "USD",
        }
    }
    status, resp = http("POST", "/api/v1/customers", payload)
    if status in (200, 201):
        print(f"  customer '{external_id}' created")
    else:
        print(f"  ! failed customer '{external_id}' (http {status}): {resp}")


def ensure_subscription(external_id):
    status, data = http("GET", f"/api/v1/subscriptions?external_customer_id={external_id}")
    if status == 200 and (data or {}).get("subscriptions"):
        print(f"  subscription for '{external_id}' exists")
        return
    payload = {
        "subscription": {
            "external_customer_id": external_id,
            # external_id is mandatory in newer Lago APIs
            "external_id": external_id,
            "plan_code": PLAN_CODE,
            "name": PLAN_NAME,
            "billing_time": "calendar",
        }
    }
    status, resp = http("POST", "/api/v1/subscriptions", payload)
    if status in (200, 201):
        print(f"  subscription '{external_id}' → plan '{PLAN_CODE}' created")
    else:
        print(f"  ! failed subscription '{external_id}' (http {status}): {resp}")


def main():
    print("▶ Lago bootstrap")
    wait_api()
    check_key()
    ensure_billable_metrics()
    ensure_plan()
    for username in DEMO_USERS:
        ensure_customer(username)
        ensure_subscription(username)
    print("✔ Lago bootstrap done.")
    print("  billing-sync can now send:")
    print(f"    per-metric events: {', '.join(METRICS)}")
    print(f"    package events   : {', '.join(sorted(PACKAGES))}")
    print("  (see billing/sync/sync.py for the active event mode + package mapping)")


if __name__ == "__main__":
    main()