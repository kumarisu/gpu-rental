#!/usr/bin/env python3
"""Idempotent Keycloak bootstrap for GPU Rental.

Creates realm `gpu-rental`, OIDC clients `coder` & `grafana`, demo users.
Uses the Keycloak Admin REST API. Run via: make keycloak-init
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

KC = os.environ["KEYCLOAK_INTERNAL"].rstrip("/")          # http://keycloak:8080
KC_PUBLIC = os.environ.get("KEYCLOAK_URL", "http://localhost:8080").rstrip("/")
ADMIN = os.environ["KEYCLOAK_ADMIN"]
ADMIN_PASSWORD = os.environ["KEYCLOAK_ADMIN_PASSWORD"]
REALM = "gpu-rental"

CODER_URL = os.environ.get("CODER_URL", "http://localhost:7080").rstrip("/")
GRAFANA_URL = os.environ.get("GRAFANA_URL", "http://localhost:3000").rstrip("/")
CODER_SECRET = os.environ["CODER_OIDC_CLIENT_SECRET"]
GRAFANA_SECRET = os.environ["GRAFANA_OIDC_CLIENT_SECRET"]

DEMO_USERS = [
    ("mike", os.environ.get("KEYCLOAK_DEMO_MIKE_PASSWORD", "mike123"), "Mike", "Walker"),
    ("anna", os.environ.get("KEYCLOAK_DEMO_ANNA_PASSWORD", "anna123"), "Anna", "Smith"),
]
EMAIL_DOMAIN = "gpu.local"


def http(method, url, data=None, token=None, timeout=5):
    body = None
    headers = {"Content-Type": "application/json"}
    if data is not None:
        body = json.dumps(data).encode()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        return e.code, None
    except urllib.error.URLError:
        return None, None


def wait_ready():
    for _ in range(60):
        _, realm = http("GET", f"{KC}/realms/master/.well-known/openid-configuration")
        if realm:
            return
        print("  keycloak not ready, waiting…")
        time.sleep(5)
    raise SystemExit("Keycloak unreachable — is the compose stack up?")


def admin_token():
    params = urllib.parse.urlencode({
        "client_id": "admin-cli",
        "grant_type": "password",
        "username": ADMIN,
        "password": ADMIN_PASSWORD,
    }).encode()
    url = f"{KC}/realms/master/protocol/openid-connect/token"
    req = urllib.request.Request(url, data=params, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())["access_token"]
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"Admin login failed (KEYCLOAK_ADMIN/KEYCLOAK_ADMIN_PASSWORD?): {exc}")


def create_realm(token):
    status, _ = http("GET", f"{KC}/admin/realms/{REALM}", token=token)
    if status == 200:
        print(f"  realm '{REALM}' already exists")
        return
    payload = {
        "realm": REALM,
        "enabled": True,
        "sslRequired": "external",
        "registrationAllowed": False,
        "registrationEmailAsUsername": True,
        "loginWithEmailAllowed": True,
        "rememberMe": True,
        "defaultSignatureAlgorithm": "RS256",
    }
    code, _ = http("POST", f"{KC}/admin/realms", data=payload, token=token)
    if code in (201, 200):
        print(f"  realm '{REALM}' created")
    else:
        raise SystemExit(f"could not create realm (http {code})")


def client_payload(client_id, secret, uris):
    return {
        "clientId": client_id,
        "name": client_id,
        "enabled": True,
        "publicClient": False,
        "standardFlowEnabled": True,
        "directAccessGrantsEnabled": False,
        "serviceAccountsEnabled": False,
        "consentRequired": False,
        "secret": secret,
        "protocol": "openid-connect",
        "redirectUris": uris,
        "webOrigins": ["*"],
    }


def create_client(token, client_id, secret, uris):
    _, clients = http("GET", f"{KC}/admin/realms/{REALM}/clients?clientId={client_id}", token=token)
    if clients:
        print(f"  client '{client_id}' already exists")
        return
    code, _ = http("POST", f"{KC}/admin/realms/{REALM}/clients",
                   data=client_payload(client_id, secret, uris), token=token)
    if code in (201, 200):
        print(f"  client '{client_id}' created (secret={secret})")
    else:
        raise SystemExit(f"could not create client '{client_id}' (http {code})")


def create_user(token, username, password, first, last):
    email = f"{username}@{EMAIL_DOMAIN}"
    _, users = http("GET", f"{KC}/admin/realms/{REALM}/users?username={username}&exact=true", token=token)
    if users:
        print(f"  user '{username}' already exists")
        return
    payload = {
        "username": username,
        "email": email,
        "emailVerified": True,
        "enabled": True,
        "firstName": first,
        "lastName": last,
        "credentials": [{"type": "password", "value": password, "temporary": False}],
    }
    code, _ = http("POST", f"{KC}/admin/realms/{REALM}/users", data=payload, token=token)
    if code in (201, 200):
        print(f"  user '{username}' created ({email})")
    else:
        raise SystemExit(f"could not create user '{username}' (http {code})")


def main():
    print(f"▶ Keycloak bootstrap (realm: {REALM})")
    wait_ready()
    token = admin_token()

    print(f"  public realm:  {KC_PUBLIC}/realms/{REALM}")
    print(f"  internal realm:{KC}/realms/{REALM}")
    create_realm(token)

    create_client(
        token, "coder", CODER_SECRET,
        [f"{CODER_URL}/api/v2/users/oidc/callback",
         f"{KC_PUBLIC.replace('localhost', '*')}/realms/{REALM}/*"],
    )
    create_client(
        token, "grafana", GRAFANA_SECRET,
        [f"{GRAFANA_URL}/login/openid",
         f"{GRAFANA_URL}/*",
         f"{KC_PUBLIC.replace('localhost', '*')}/realms/{REALM}/*"],
    )

    for username, password, first, last in DEMO_USERS:
        create_user(token, username, password, first, last)

    print("✔ Keycloak bootstrap done.")
    print(f"  Next: open {CODER_URL}, first login registers the owner user.")


if __name__ == "__main__":
    main()