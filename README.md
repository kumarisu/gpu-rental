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

## Data flows

Where data moves **from → to**, what **triggers** it, and which component **carries**
it. The per-user attribution key for the whole platform is the Docker label
`coder.owner` (container `coder-<user>-<workspace>`): cAdvisor exposes it to
Prometheus as `container_label_coder_owner`, Promtail/Loki carries it as
`container_name`, and billing-sync maps it to the Lago `external_customer_id`.

### 1. Initial bootstrap data (`make init`)

Trigger: **operator runs `make up` once, then `make init`**. All three sub-steps are
idempotent (safe to re-run; required again after `make clean -v` wipes volumes).

| Step (`make …`) | Data (from → to) | Trigger | Carrier |
|---|---|---|---|
| (auto) Lago org create | `.env` (`LAGO_ORG_*`, `LAGO_CREATE_ORG=true`) → `lago-db` (`organizations`, `api_keys`) | first boot of `lago-api` | `lago-api` Rails initializer (in-process, no network hop) |
| `keycloak-init` | `.env` (realm name, client secrets, demo passwords) → Keycloak: realm `gpu-rental`, OIDC clients `coder`/`grafana`, users `mike`/`anna` | operator command (`--profile init run --rm keycloak-init`) | one-shot container `keycloak-init` (`keycloak/init.py`, stdlib `urllib`) → Keycloak Admin REST over `KEYCLOAK_INTERNAL=http://keycloak:8080` |
| `grafana-init` | `monitoring/grafana/provisioning/*` + `dashboards/gpu-usage.json` → Grafana datasources + dashboard "GPU Rental — Usage per user" (UID substitution `__DS_*__`) | operator command, after Grafana healthy | one-shot container `grafana-init` (`monitoring/grafana/init.py`) → Grafana HTTP API `http://grafana:3000/api/...` (Basic `admin` / `GRAFANA_ADMIN_PASSWORD`) |
| `lago-bootstrap` | plan definition in code → Lago: 4 billable metrics (`cpu_seconds`, `ram_gb_hours`, `network_gb`, `disk_write_gb`), plan `gpu-usage` + per-unit prices, customers + subscriptions for `LAGO_DEMO_USERS` | operator command, after Lago `/health` = 200 and `LAGO_ORG_API_KEY` valid | one-shot container `lago-bootstrap` (`billing/lago/bootstrap.py`) → Lago REST `POST /api/v1/{billable_metrics,plans,customers,subscriptions}` over `LAGO_API_INTERNAL` (Bearer org key) |

```
.env ──(keycloak-init, Admin REST)──▶ Keycloak :8080 (realm, clients, users)
provisioning/*.yaml + gpu-usage.json ──(grafana-init, Grafana API)──▶ Grafana :3000
bootstrap.py ──(POST /api/v1/*, Bearer key)──▶ Lago API :8000 ──▶ lago-db / lago-redis
```

### 2. Creating a new workspace (workspace data flow)

Trigger: **user clicks "Create workspace" in Coder UI**. Prerequisite, done once per
template version by the operator: `make template-images` builds
`gpu-rental/ws-cpu|ws-gpu`, then `make push-templates TOKEN=<owner-token>` publishes
`cpu-base`/`gpu-cuda` via the `coder` CLI *inside* the `coder` container talking to
`CODER_URL`.

```
Browser ──OIDC login (Keycloak)──▶ Coder :7080 ──terraform apply──▶ Docker daemon (/var/run/docker.sock)
       ◀── workspace agent (SSH/terminal) ── container coder-<user>-<ws> + volume coder-<id>-home
```

| Hop | Data (from → to) | Trigger | Carrier |
|---|---|---|---|
| Browser → Coder | OIDC code → session; workspace spec (template version + params: image, CPU/RAM) | user submits the "New workspace" form | Coder UI → `coder-server` (`POST /api/v2/...`, session cookie; identity verified against Keycloak `gpu-rental` realm) |
| Coder → Docker daemon | Terraform plan (`coder/templates/<name>/main.tf`: `docker_container` + `docker_volume`, labels `coder.owner`, `coder.workspace_id`, …) → running container `coder-<user>-<ws>` + persistent home volume | provisioner job picks up the build (`terraform apply`) | built-in Coder provisioner inside `coder-server` → Docker provider → `/var/run/docker.sock` (needs `DOCKER_GROUP_GID` so uid `1000/coder` can dial the socket) |
| Workspace → user | agent startup + image (`codercom/example-base` or `gpu-rental/ws-*`) → live terminal/IDE over the agent tunnel | container start | Coder agent process inside the workspace container (reverse tunnel back to `coder-server`) |

The new container immediately starts emitting metrics/logs, so flows 3 and 4 pick it
up automatically — no registration step needed because discovery is label-based
(`container_label_coder_owner!=""`).

### 3. (next: usage → Grafana — see below)

      (runs every 60s)
    ░░░░░░░░║
    ░░░░░░░░║  POST /api/v1/events
            ║  properties: {event_name:"gpu.usage",
            ║    data: {customer_id:"mike", # external_customer_id
            ║            properties:{cpu_seconds: 49.07, ram_gb_hours:0.002, network_gb:0.004,...}}
            ║
   ┌────────■────────────────────────────────────────────────────────────────────┐                                                                                                            
   │  Lago API  (lago-api:3000)                                                  │                                                                                                            
   │  ┣━ receives event → agg per customer per plan (sum) → accumulates          │                                                                                                            
   │  ┣━ API keys (LAGO_ORG_API_KEY) authorized; idempotent (event id)           │                                                                                                            
   │  ┗━ used by Lago billing worker to produce invoice per period               │                                                                                                            
   └─────────────────────────────────────────────────────────────────────────────┘                                                                                                            
            │                                                                                                                                                                                 
            ▼
   ┌────────■─────────── Lago UI  (http://localhost:8580) ────────────────────────┐                                                                                                          
   │  • Developer → API keys  (copy LAGO_ORG_API_KEY into .env)                   │                                                                                                           
   │  • Customers → mike / anna (external_id) + subscription → plan gpu-usage     │                                                                                                           
   │  • Usage / Invoices → shows accumulated usage + charges per billing period   │                                                                                                           
   └──────────────────────────────────────────────────────────────────────────────┘                                                                                                           
   ```

   > **Onboarding (automatic org + API key):** if `LAGO_CREATE_ORG=true`, the first boot of
   > `lago-api` self-creates the organisation + an API key + admin sign-in inside the container
   > before any operator API call. This seed is the foundation that `lago-bootstrap` and
   > `billing-sync` depend on. Re-run `make lago-bootstrap` to re-create metric/plan/customer
   > data after a DB wipe (`make clean -v`).

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
