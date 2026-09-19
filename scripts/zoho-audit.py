#!/usr/bin/env python3
"""Audit a Zoho Commerce store via the REST API.

Reads what is actually configured in the store (catalog, payments, shipping,
tax, coupons, pages) so we can diff it against the Supabase/React storefront
before cutting over. Works on unpublished stores.

Credentials come from .env.local (gitignored) or the environment:

    ZOHO_CLIENT_ID=...
    ZOHO_CLIENT_SECRET=...
    ZOHO_ORG_ID=60084868171
    # one of:
    ZOHO_REFRESH_TOKEN=...      # preferred, long lived
    ZOHO_GRANT_TOKEN=...        # one-shot code from the API console, auto-exchanged
    ZOHO_DC=in                  # in | com | eu | au

Usage:
    python3 scripts/zoho-audit.py            # summary
    python3 scripts/zoho-audit.py --dump DIR # also write raw JSON per resource
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Resource path -> (label, key holding the list in the response)
RESOURCES = [
    ("products", "Products", "products"),
    ("categories", "Categories", "categories"),
    ("salesorders", "Orders", "salesorders"),
    ("customers", "Customers", "customers"),
    ("coupons", "Coupons", "coupons"),
    ("giftcards", "Gift cards", "giftcards"),
    ("settings/paymentgateways", "Payment gateways", "paymentgateways"),
    ("settings/shipping", "Shipping", "shipping"),
    ("settings/taxes", "Taxes", "taxes"),
    ("settings/currencies", "Currencies", "currencies"),
    ("pages", "CMS pages", "pages"),
]

def base_candidates(dc, api_domain):
    """Confirmed-working bases first; api_domain variants as fallback."""
    bases = [
        f"https://commerce.zoho.{dc}/store/api/v1",   # confirmed reachable
        f"https://www.zohoapis.{dc}/commerce/v1",     # confirmed reachable
    ]
    if api_domain:
        root = api_domain.rstrip("/")
        bases += [f"{root}/store/api/v1", f"{root}/commerce/api/v1"]
    return bases


def load_env():
    path = os.path.join(ROOT, ".env.local")
    if os.path.exists(path):
        with open(path) as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def post_form(url, fields):
    body = urllib.parse.urlencode(fields).encode()
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


SCOPE = (
    "ZohoCommerce.items.READ,ZohoCommerce.settings.READ,"
    "ZohoCommerce.salesorders.READ,ZohoCommerce.contacts.READ"
)


def get_access_token(dc, client_id, client_secret, org_id):
    """Return (access_token, api_domain).

    Prefers a stored refresh token, then a one-shot grant token, and finally
    falls back to the self-client client_credentials flow, which needs only the
    id and secret plus the service org id.
    """
    token_url = f"https://accounts.zoho.{dc}/oauth/v2/token"
    refresh_token = os.environ.get("ZOHO_REFRESH_TOKEN")
    grant_token = os.environ.get("ZOHO_GRANT_TOKEN")

    if not refresh_token and not grant_token:
        print("No refresh/grant token — trying client_credentials flow...")
        payload = post_form(token_url, {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": os.environ.get("ZOHO_SCOPE", SCOPE),
            "soid": f"ZohoCommerce.{org_id}",
        })
        if "access_token" not in payload:
            sys.exit(
                f"client_credentials failed: {payload}\n\n"
                "Generate a grant token instead: api-console.zoho.in -> your Self Client\n"
                "-> Generate Code, then set ZOHO_GRANT_TOKEN in .env.local and rerun."
            )
        return payload["access_token"], payload.get("api_domain")

    if not refresh_token and grant_token:
        payload = post_form(token_url, {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "client_secret": client_secret,
            "code": grant_token,
        })
        refresh_token = payload.get("refresh_token")
        if not refresh_token:
            sys.exit(f"Grant token exchange failed: {payload}")
        print("Save this to .env.local so you never need a grant token again:")
        print(f"  ZOHO_REFRESH_TOKEN={refresh_token}\n")
        return payload.get("access_token"), payload.get("api_domain")

    payload = post_form(token_url, {
        "grant_type": "refresh_token",
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
    })
    if "access_token" not in payload:
        sys.exit(f"Token refresh failed: {payload}")
    return payload["access_token"], payload.get("api_domain")


def api_get(base, path, token, org_id):
    url = f"{base}/{path}"
    request = urllib.request.Request(url, method="GET")
    request.add_header("Authorization", f"Zoho-oauthtoken {token}")
    request.add_header("X-com-zoho-store-organizationid", org_id)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response), None
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:200]
        return None, f"HTTP {error.code}: {detail}"
    except Exception as error:  # noqa: BLE001 - report and continue the audit
        return None, str(error)


def find_base(dc, api_domain, token, org_id):
    for base in base_candidates(dc, api_domain):
        _, error = api_get(base, "products", token, org_id)
        if error is None:
            return base
        # 6041 means we reached Commerce but the org id is wrong - right base.
        if "6041" in str(error):
            return base
        print(f"  probe {base} -> {error.splitlines()[0][:90]}")
    return None


def discover_org(base, token):
    """Ask Commerce which organizations this token can actually see."""
    payload, error = api_get(base, "organizations", token, "")
    if error:
        return None, error
    orgs = (payload or {}).get("organizations") or []
    if not orgs:
        return None, "token sees zero organizations"
    for org in orgs:
        oid = org.get("organization_id") or org.get("id")
        name = org.get("name") or org.get("organization_name") or "?"
        print(f"  found org: {oid}  {name}")
    first = orgs[0]
    return str(first.get("organization_id") or first.get("id")), None


def summarize(label, payload, list_key):
    items = payload.get(list_key) if isinstance(payload, dict) else None
    if items is None and isinstance(payload, dict):
        # settings endpoints return an object, not a list
        keys = [k for k in payload if k not in ("code", "message")]
        return f"configured ({', '.join(keys[:4])})" if keys else "empty"
    if not items:
        return "0 — NOTHING CONFIGURED"
    return f"{len(items)} item(s)"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dump", metavar="DIR", help="write raw JSON per resource")
    args = parser.parse_args()

    load_env()
    dc = os.environ.get("ZOHO_DC", "in")
    client_id = os.environ.get("ZOHO_CLIENT_ID")
    client_secret = os.environ.get("ZOHO_CLIENT_SECRET")
    org_id = os.environ.get("ZOHO_ORG_ID")

    missing = [n for n, v in [
        ("ZOHO_CLIENT_ID", client_id),
        ("ZOHO_CLIENT_SECRET", client_secret),
        ("ZOHO_ORG_ID", org_id),
    ] if not v]
    if missing:
        sys.exit("Missing in .env.local: " + ", ".join(missing))

    token, api_domain = get_access_token(dc, client_id, client_secret, org_id)
    api_domain = api_domain or f"https://commerce.zoho.{dc}"
    print(f"Authenticated. api_domain={api_domain}\n")

    print("Probing API base path...")
    base = find_base(dc, api_domain, token, org_id)
    if not base:
        sys.exit("\nNo base path worked. Check that the token scope covers ZohoCommerce.")
    print(f"  using {base}")

    discovered, error = discover_org(base, token)
    if discovered:
        if discovered != org_id:
            print(f"  ZOHO_ORG_ID={org_id} looks wrong - using discovered {discovered}")
            print(f"  update .env.local: ZOHO_ORG_ID={discovered}")
        org_id = discovered
    else:
        print(f"  org discovery: {error}")
        print("  The Zoho account that authorized this client owns no Commerce org.")
        print("  Check you created the Self Client while signed in as the account")
        print("  that owns the store (api-console.zoho.in, same login as the editor).")
    print()

    if args.dump:
        os.makedirs(args.dump, exist_ok=True)

    print(f"{'RESOURCE':<22} STATUS")
    print("-" * 60)
    for path, label, list_key in RESOURCES:
        payload, error = api_get(base, path, token, org_id)
        if error:
            print(f"{label:<22} ! {error.splitlines()[0]}")
            continue
        print(f"{label:<22} {summarize(label, payload, list_key)}")
        if args.dump:
            name = path.replace("/", "_") + ".json"
            with open(os.path.join(args.dump, name), "w") as handle:
                json.dump(payload, handle, indent=2)

    if args.dump:
        print(f"\nRaw JSON written to {args.dump}")


if __name__ == "__main__":
    main()
