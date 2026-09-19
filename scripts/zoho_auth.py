"""Shared Zoho auth with an on-disk token cache.

Zoho rate-limits the token endpoint, and refreshing on every script run trips it.
Access tokens last an hour, so cache one and reuse it until it is nearly expired.
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, ".zoho-token-cache.json")
SKEW = 300  # refresh with five minutes to spare


def load_env():
    path = os.path.join(ROOT, ".env.local")
    if os.path.exists(path):
        with open(path) as handle:
            for line in handle:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def access_token(force=False):
    load_env()
    if not force and os.path.exists(CACHE):
        try:
            cached = json.load(open(CACHE))
            if cached.get("expires_at", 0) - SKEW > time.time():
                return cached["access_token"]
        except Exception:  # noqa: BLE001 - a bad cache is not fatal
            pass

    fields = {
        "grant_type": "refresh_token",
        "client_id": os.environ["ZOHO_CLIENT_ID"],
        "client_secret": os.environ["ZOHO_CLIENT_SECRET"],
        "refresh_token": os.environ["ZOHO_REFRESH_TOKEN"],
    }
    body = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request("https://accounts.zoho.in/oauth/v2/token", data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        payload = json.load(urllib.request.urlopen(req, timeout=30))
    except Exception as error:  # noqa: BLE001
        sys.exit(f"token refresh failed: {error}\n"
                 "If this is a rate limit, wait a few minutes - the cache will "
                 "avoid it on subsequent runs.")
    if "access_token" not in payload:
        sys.exit(f"token refresh failed: {payload}")

    json.dump(
        {"access_token": payload["access_token"],
         "expires_at": time.time() + int(payload.get("expires_in", 3600))},
        open(CACHE, "w"),
    )
    return payload["access_token"]


def org_id():
    load_env()
    return os.environ["ZOHO_ORG_ID"]
