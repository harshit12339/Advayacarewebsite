#!/usr/bin/env python3
"""Snapshot everything readable from the Zoho Commerce store.

Written to run before the catalog import, so there is a point to roll back to.
Every product is fetched individually as well as in list form - the list
response omits most fields, and it is the per-product record that matters if
anything has to be reconstructed.

Usage:
    python3 scripts/zoho-backup.py
    python3 scripts/zoho-backup.py --out-dir backups/zoho
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = "https://commerce.zoho.in/store/api/v1"

# Collections worth capturing, with the key holding the list in each response.
ENDPOINTS = [
    ("products", "products"),
    ("categories", "categories"),
    ("collections", "collections"),
    ("coupons", "coupons"),
    ("customers", "contacts"),
    ("salesorders", "salesorders"),
    ("invoices", "invoices"),
    ("packages", "packages"),
    ("brands", "brands"),
    ("carts", "carts"),
    ("users", "users"),
    ("organizations", "organizations"),
    ("settings/taxes", "taxes"),
    ("settings/shippingzones", "shippingzones"),
    ("settings/paymentgateways", "payment_gateways"),
    ("settings/currencies", "currencies"),
    ("settings/emailtemplates", "emailtemplates"),
]

# No API exists for these - flagged in the manifest so the gap is on the record.
NOT_BACKED_UP = [
    "Storefront theme and page layouts",
    "CMS pages (policy pages, about, contact)",
    "Product image binaries (only metadata is returned by the API)",
    "SEO and domain settings",
    "Checkout customisations and code snippets",
]


def load_env():
    path = os.path.join(ROOT, ".env.local")
    if not os.path.exists(path):
        sys.exit("No .env.local found.")
    with open(path) as handle:
        for line in handle:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def access_token():
    fields = {
        "grant_type": "refresh_token",
        "client_id": os.environ["ZOHO_CLIENT_ID"],
        "client_secret": os.environ["ZOHO_CLIENT_SECRET"],
        "refresh_token": os.environ["ZOHO_REFRESH_TOKEN"],
    }
    body = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request("https://accounts.zoho.in/oauth/v2/token", data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    payload = json.load(urllib.request.urlopen(req, timeout=30))
    if "access_token" not in payload:
        sys.exit(f"Auth failed: {payload}")
    return payload["access_token"]


def get(path, token, org):
    req = urllib.request.Request(f"{BASE}/{path}")
    req.add_header("Authorization", f"Zoho-oauthtoken {token}")
    req.add_header("X-com-zoho-store-organizationid", org)
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.load(response), None
    except urllib.error.HTTPError as error:
        return None, f"HTTP {error.code}: {error.read().decode('utf-8', 'replace')[:120]}"
    except Exception as error:  # noqa: BLE001 - record and continue
        return None, str(error)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", default=None)
    args = parser.parse_args()

    load_env()
    org = os.environ.get("ZOHO_ORG_ID")
    if not org:
        sys.exit("ZOHO_ORG_ID not set")
    token = access_token()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_dir = args.out_dir or os.path.join(ROOT, "backups", f"zoho-{org}-{stamp}")
    os.makedirs(out_dir, exist_ok=True)

    manifest = {
        "taken_at": datetime.now(timezone.utc).isoformat(),
        "organization_id": org,
        "api_base": BASE,
        "resources": {},
        "errors": {},
        "not_backed_up": NOT_BACKED_UP,
    }

    print(f"Backing up org {org} -> {out_dir}\n")

    for path, list_key in ENDPOINTS:
        payload, error = get(path, token, org)
        name = path.replace("/", "_")
        if error:
            manifest["errors"][path] = error
            print(f"  {name:<28} SKIPPED  {error.splitlines()[0][:60]}")
            continue
        with open(os.path.join(out_dir, f"{name}.json"), "w") as handle:
            json.dump(payload, handle, indent=2)
        items = payload.get(list_key)
        count = len(items) if isinstance(items, list) else "object"
        manifest["resources"][path] = count
        print(f"  {name:<28} {count}")

    # The list response strips most fields, so pull each product in full.
    products, error = get("products", token, org)
    if not error:
        detail_dir = os.path.join(out_dir, "products")
        os.makedirs(detail_dir, exist_ok=True)
        rows = products.get("products", [])
        print(f"\n  fetching {len(rows)} full product records...")
        saved = 0
        for product in rows:
            pid = product["product_id"]
            detail, detail_error = get(f"products/{pid}", token, org)
            if detail_error:
                manifest["errors"][f"products/{pid}"] = detail_error
                continue
            with open(os.path.join(detail_dir, f"{pid}.json"), "w") as handle:
                json.dump(detail, handle, indent=2)
            saved += 1
            time.sleep(0.15)  # stay well inside Zoho's rate limits
        manifest["resources"]["products (full records)"] = saved
        print(f"  saved {saved} full product records")

    with open(os.path.join(out_dir, "MANIFEST.json"), "w") as handle:
        json.dump(manifest, handle, indent=2)

    print(f"\nManifest: {os.path.join(out_dir, 'MANIFEST.json')}")
    if manifest["errors"]:
        print(f"{len(manifest['errors'])} endpoint(s) could not be read - see the manifest.")
    print("\nNot covered by this backup (no API):")
    for item in NOT_BACKED_UP:
        print(f"  - {item}")


if __name__ == "__main__":
    main()
