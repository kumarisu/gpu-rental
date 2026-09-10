# GPU Rental Infra — helper commands
# Usage:  make [target]  (targets read .env when present)

-include .env
export

DOCKER_COMPOSE := docker compose

.PHONY: help secrets init up down restart ps logs \
        keycloak-init grafana-init lago-bootstrap coder-convert-login \
        push-templates billing-sync-logs doctor clean

help:                              ## Show available targets
	@grep -E '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) | \
	  awk 'BEGIN{FS=":.*## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

secrets:                           ## Print freshly generated secrets for .env
	@echo "# paste into .env"
	@echo "LAGO_SECRET_KEY_BASE=$$(openssl rand -hex 64)"
	@echo "LAGO_RSA_PRIVATE_KEY=$$(openssl genrsa 2048 2>/dev/null | base64 | tr -d '\n')"
	@echo "LAGO_ENCRYPTION_PRIMARY_KEY=$$(cat /dev/urandom | LC_ALL=C tr -dc 'a-zA-Z0-9' | fold -w 32 | head -n 1)"
	@echo "LAGO_ENCRYPTION_DETERMINISTIC_KEY=$$(cat /dev/urandom | LC_ALL=C tr -dc 'a-zA-Z0-9' | fold -w 32 | head -n 1)"
	@echo "LAGO_ENCRYPTION_KEY_DERIVATION_SALT=$$(cat /dev/urandom | LC_ALL=C tr -dc 'a-zA-Z0-9' | fold -w 32 | head -n 1)"

up:                                ## Start ALL services (docker compose up -d)
	$(DOCKER_COMPOSE) up -d --remove-orphans

down:                              ## Stop and remove the compose network
	$(DOCKER_COMPOSE) down

restart:                           ## Restart everything
	$(DOCKER_COMPOSE) restart

ps:                                ## Container status
	$(DOCKER_COMPOSE) ps

logs:                              ## Tail all logs
	$(DOCKER_COMPOSE) logs -f

init: keycloak-init grafana-init lago-bootstrap  ## One-time provisioning (idempotent)

template-images:                 ## Build the workspace images referenced by the Coder templates
	docker build -t gpu-rental/ws-gpu:latest coder/templates/gpu-cuda
	docker build -t gpu-rental/ws-cpu:latest coder/templates/cpu-base

keycloak-init:                     ## Create Keycloak realm, OIDC clients & demo users
	$(DOCKER_COMPOSE) --profile init run --rm keycloak-init

grafana-init:                      ## Import Grafana datasources & dashboards
	$(DOCKER_COMPOSE) --profile init run --rm grafana-init

lago-bootstrap:                    ## Create Lago billable metrics, plan, demo customers/subscriptions
	$(DOCKER_COMPOSE) --profile init run --rm lago-bootstrap

coder-convert-login:               ## Repair Coder OIDC access for @gpu.local accounts
	# 1) Accounts created via password auth (e.g. first-time setup) before
	#    OIDC was enabled reject OIDC logins with "Incorrect login type" —
	#    flip them to login_type=oidc.
	# 2) If the Keycloak users were recreated (new subject IDs), stale links
	#    make Coder refuse logins with "account already linked to different
	#    identity" — drop them so the next OIDC login re-links cleanly.
	docker exec coder-db psql -U coder -d coder -c "UPDATE users SET login_type='oidc' WHERE login_type='password' AND email LIKE '%@gpu.local'; DELETE FROM user_links WHERE login_type='oidc' AND user_id IN (SELECT id FROM users WHERE email LIKE '%@gpu.local');"

push-templates:                    ## Push Coder templates — make push-templates TOKEN=<cli token from Coder UI>
	@test -n "$(TOKEN)" || { echo "usage: make push-templates TOKEN=<cli token from Coder UI>  (Settings → Tokens)"; exit 1; }
	$(DOCKER_COMPOSE) exec -T coder-server /bin/bash -lc \
	  "echo '$(TOKEN)' | coder login $(CODER_URL) --use-token-as-session && \
	   coder templates push -y --directory /templates/gpu-cuda && \
	   coder templates push -y --directory /templates/cpu-base"

billing-sync-logs:                 ## Follow billing-sync logs
	$(DOCKER_COMPOSE) logs -f billing-sync

doctor:                            ## Check health of every core service
	@echo "== Keycloak =="      ; curl -fsS $(KEYCLOAK_URL)/realms/gpu-rental/.well-known/openid-configuration     >/dev/null && echo OK || echo "FAIL (run: make keycloak-init)"
	@echo "== Coder =="          ; curl -fsS $(CODER_URL)/health >/dev/null && echo OK || echo "waiting…"
	@echo "== Prometheus =="     ; curl -fsS $(PROMETHEUS_URL)/-/healthy >/dev/null && echo OK || echo FAIL
	@echo "== Grafana =="        ; curl -fsS -o /dev/null -w '%{http_code}\n' $(GRAFANA_URL)/api/health || echo FAIL
	@echo "== Loki =="           ; curl -fsS $(LOKI_URL)/health >/dev/null && echo OK || echo FAIL
	@echo "== Lago API =="       ; curl -fsS $(LAGO_API_URL)/health >/dev/null && echo OK || echo "waiting (run: make lago-bootstrap)"

clean:                             ## Remove containers + named volumes (keeps ./data)
	$(DOCKER_COMPOSE) down -v