# GPU Rental Infra

Self-hosted platform for renting **GPU / CPU dev workspaces** as Docker containers, with SSO login,
usage metering, log aggregation and **per-user usage-based billing**.

| # | Requirement | Component |
|---|-------------|-----------|
| 1 | Login (SSO) | **Keycloak** — OIDC provider for Coder & Grafana |
| 2 | Templates + runtime workspaces | **Coder** — workspace templates, Docker-provisioned containers |
| 3 | Usage metrics → Prometheus / Grafana, logs → Loki | **cAdvisor + node-exporter + Prometheus + Grafana + Loki + Promtail** |
| 4 | Usage per user → Lago for billing | **billing-sync** — Prometheus deltas → Lago metered events |

---

## Architecture

```
                    Browser
                       │
        ┌──────────────┼──────────────────┬─────────────┐
        ▼              ▼                  ▼             ▼
   Keycloak :8080   Coder :7080   Grafana :3000    Lago UI :8580
   (SSO login)  (templates/        (dashboards)    (billing UI)
                 workspaces)
                       │
        ┌──────────────┼──────────────────┐
        ▼              ▼                  ▼
   cAdvisor     node-exporter        Loki :3100 ◄── Promtail
        │              │                  ▲
        ▼              ▼                  │ Docker json-file logs
  Prometheus :9090 ────┴────────────      │ (incl. workspace containers)
        │                                 │
        ▼  (container_* metrics, coder.owner labels)
  billing-sync  ──POST /api/v1/events──►  Lago API :8000
                                           (api/worker/clock/postgres/redis/pdf)
```

**Workspace containers** are created by Coder on the Docker host, named
`coder-<user>-<workspace>` and tagged with `coder.owner` / `coder.workspace_id`. Those labels are
what cAdvisor exposes to Prometheus and what lets every metric/log be attributed **per user**.

---

## Requirements

- **Linux host** (recommended for production) with Docker Engine ≥ 24 + Docker Compose v2.
  macOS / Docker Desktop works for development of most services.
- NVIDIA driver + nvidia-container-toolkit **only if** you use the GPU template / exporter.
- On Linux, the `coder` container must access the Docker socket: set
  `DOCKER_GROUP_GID=$(getent group docker | cut -d: -f3)` in `.env`.

## Quickstart

```bash
# 1. Configuration
cp .env.example .env
#    EDIT .env — set every CHANGE_ME value (run `make secrets` for the random ones)

# 2. Start everything (Keycloak, Coder, Prometheus, Grafana, Loki, Lago, billing-sync)
make up

# 3. One-time provisioning (idempotent):
make init
#    ├─ keycloak-init  → realm `gpu-rental`, OIDC clients (coder, grafana), users mike/anna
#    ├─ grafana-init   → imports datasources + "GPU Rental — Usage per user" dashboard
#    └─ lago-bootstrap → billable metrics, plan `gpu-usage`, customers + subscriptions

# 4. Build the workspace images & publish the Coder templates
make template-images
make push-templates TOKEN=<cli-token>   # Coder UI → profile → token, or:
#   docker compose exec coder-server coder tokens create --name infra
#   then re-run with the printed token

# 5. First login
#    open http://localhost:7080 → "Sign in with Keycloak" → mike / <KEYCLOAK_DEMO_MIKE_PASSWORD>
#    first user to log in becomes the Coder owner. Create a workspace from gpu-cuda / cpu-base.
#    A container `coder-mike-<name>` appears on the host and starts generating data.

# 6. Verify
make doctor
#    Grafana http://localhost:3000  → dashboard "GPU Rental — Usage per user"
#    Lago    http://localhost:8580  → Developer → API keys, customers, subscriptions
```

## Ports & default URLs

| Service | URL | Default login |
|---|---|---|
| Keycloak | http://localhost:8080 | admin / `KEYCLOAK_ADMIN_PASSWORD` (Admin Console) |
| Coder | http://localhost:7080 | Keycloak SSO (`mike`/demo password) |
| Prometheus | http://localhost:9090 | — |
| Grafana | http://localhost:3000 | admin / `GRAFANA_ADMIN_PASSWORD` (or Keycloak SSO) |
| Loki | http://localhost:3100 | — |
| cAdvisor | http://localhost:9091 | — |
| Lago UI | http://localhost:8580 | admin@gpu.local / `LAGO_ORG_USER_PASSWORD` |
| Lago API | http://localhost:8000 | Bearer `LAGO_ORG_API_KEY` |

⚠️ Every password/secret is a `CHANGE_ME_*` placeholder — generate strong values via `make secrets`.

## How usage is collected and billed

1. **cAdvisor** scrapes every container on the host and Prometheus stores
   `container_cpu_usage_seconds_total`, `container_memory_usage_bytes`,
   `container_network_*_bytes_total`, `container_fs_*_bytes_total`, tagged with
   the Coder labels (`container_label_coder_owner`, …).
2. **Grafana** visualises them per user (`Monitoring/grafana/dashboards/gpu-usage.json`).
   Logs land in **Loki** via **Promtail** and are filterable per container (`tag` label).
3. **billing-sync** (`billing/sync/sync.py`) runs every `SYNC_INTERVAL_SECONDS` and for each
   running `coder-<user>-<workspace>` container computes the delta since the previous cycle:
   - `cpu_seconds`   ← increase of the CPU-seconds counter
   - `ram_gb_hours`  ← current RSS (GB) held during the window
   - `network_gb`    ← increase of rx+tx byte counters
   - `disk_write_gb` ← increase of the writes counter
4. Deltas are aggregated **per user** and sent to Lago as **metered events**
   `POST /api/v1/events` with `properties.value = <delta>`; Lago aggregates the
   `sum` billable metrics into the `gpu-usage` plan (per-unit prices set in
   `billing/lago/bootstrap.py`) and can invoice per billing period.

State is persisted in `data/sync/state.db` (bind-mounted), so `billing-sync` restarts
do not lose or double-count usage. **New metric?** add a billable metric in the bootstrap
script, a row in `USAGE` in `sync.py`, and a price in `bootstrap.py`.

## Logging (container name in every log line)

By default Docker's `json-file` log driver writes `log/time/stream` only. To label every line
with its container name (needed to filter workspace logs per user in Grafana), configure the
Docker **daemon** once:

```json
// /etc/docker/daemon.json  (then: systemctl restart docker)
{
  "log-driver": "json-file",
  "log-opts": { "tag": "{{.Name}}" }
}
```

Promtail (`monitoring/promtail/promtail.yml`) tails `/var/lib/docker/containers/*/*-json.log`,
parses the JSON and ships the daemon's `tag` field as the `container_name` label.
In Grafana's Logs panel:
`{container_name="coder-mike-gpu01"}` or `{job="docker"}`.

*(Loki/Promtail are pinned to 3.6.x; if you prefer Grafana Alloy for log shipping,
the promtail config keys map 1:1 to Alloy's `docker_containers` source.)*

## GPU support

- **Workspaces**: the `gpu-cuda` Coder template passes `gpus = "all"` (+ `/dev/shm` 1G).
  Build its image with `make template-images` on the GPU host.
- **Host-level metrics**: run the optional host exporter (`monitoring/gpu/`) to get per-GPU
## Project layout

```
├── docker-compose.yml            # the whole platform (default + `init` profiles)
├── .env.example                  # all configuration (copy to .env)
├── Makefile                      # up / init / push-templates / doctor …
├── keycloak/init.py              # realm + OIDC clients + demo users (idempotent)
├── coder/
│   └── templates/
│       ├── gpu-cuda/             # GPU template  (main.tf + Dockerfile)
│       └── cpu-base/             # CPU template  (main.tf + Dockerfile)
├── monitoring/
│   ├── prometheus/prometheus.yml
│   ├── loki/loki.yml
│   ├── promtail/promtail.yml
│   ├── grafana/
│   │   ├── init.py               # dashboard import (uid substitution)
│   │   ├── provisioning/         # datasources (file-based)
│   │   └── dashboards/gpu-usage.json
│   └── gpu/                      # optional host-side NVIDIA exporter
└── billing/
    ├── lago/bootstrap.py         # billable metrics + plan + customers/subscriptions
    └── sync/                     # billing-sync service (Prometheus → Lago)
```

## Make targets

| Target | Description |
|---|---|
| `make up` / `down` / `restart` / `logs` / `ps` | lifecycle |
| `make init` | keycloak-init + grafana-init + lago-bootstrap (idempotent) |
| `make keycloak-init` | Keycloak realm, clients, demo users |
| `make grafana-init` | Grafana datasources + dashboard import |
| `make lago-bootstrap` | Lago billable metrics, plan, demo customers/subscriptions |
| `make template-images` | build `gpu-rental/ws-gpu` / `gpu-rental/ws-cpu` images |
| `make push-templates TOKEN=…` | publish Coder templates to the server |
| `make doctor` | health checks for all core services |

## Troubleshooting

- **Coder can't reach `/var/run/docker.sock`** → `DOCKER_GROUP_GID` doesn't match the host
  (`getent group docker | cut -d: -f3`) or the daemon socket lives elsewhere (rootless/Colima).
- **Keycloak login fails in Coder** → re-run `make keycloak-init`; the client redirect URI
  must match `CODER_URL` exactly (`http://localhost:7080/api/v2/users/oidc/callback`).
- **No metrics for workspaces** → check `http://localhost:9091/docker/<container>` (cAdvisor)
  and Prometheus → “Explorer” for `container_cpu_usage_seconds_total`. Container templates must
  add the `coder.owner` label (both shipped templates do).
- **Events rejected by Lago (401/422)** → `LAGO_ORG_API_KEY` wrong/missing: `make lago-bootstrap`;
  verify the customer exists (Lago UI → Customers) and the subscription has the active plan.
- **Lago UI shows no organisation** → `LAGO_CREATE_ORG`/LAGO_ORG_* were unset on first boot;
  sign up in the UI, copy the API key into `.env`, re-run `make up` + `make lago-bootstrap`.
- **No logs in Loki** → verify promtail reads the files (`docker logs promtail`) and that the
  daemon writes `tag` (see *Logging* above).

## Security / production notes

- Change **all** secrets in `.env` (`make secrets`), restrict port exposure, put an HTTPS
  reverse-proxy (Caddy/Traefik) in front, and set the public `*_URL` variables to real FQDNs.
- The `init` containers (`profiles: ["init"]`) run `python:3.12-slim` from Docker Hub — pin an
  image digest for production.
- cAdvisor runs `privileged: true`; on hardened hosts replace with the documented
  `--cap-add` set.
- Loki/Prometheus/Grafana ship with **no authentication** between containers — keep the compose
  network private; use the reverse proxy for external access.
- `billing-sync` replays deltas after a crash using its sqlite state — keep `./data` backed up.
  utilization + per-container GPU memory attribution in Prometheus/Grafana and, if desired,
  a `gpu_seconds` billable metric.