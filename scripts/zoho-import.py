#!/usr/bin/env python3
"""Apply the prepared catalog CSV to Zoho Commerce.

Matches on SKU: existing products are updated, unmatched rows are created.
Prices come from Supabase, which is what the live storefront charges today.

Dry run by default. --apply writes, and --limit is there so the first write can
be a single product that gets read back and checked before the rest follow.

Usage:
    python3 scripts/zoho-import.py                    # dry run, all rows
    python3 scripts/zoho-import.py --apply --limit 1  # write one, verify
    python3 scripts/zoho-import.py --apply            # write the rest
"""

import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = "https://commerce.zoho.in/store/api/v1"
CSV_PATH = os.path.join(ROOT, "zoho-import", "zoho-products-import.csv")
CATEGORY_IDS = os.path.join(ROOT, "zoho-import", "category-ids.json")


def load_env():
    path = os.path.join(ROOT, ".env.local")
    with open(path) as handle:
        for line in handle:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def token():
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
        sys.exit(f"auth failed: {payload}")
    return payload["access_token"]


def call(path, tok, org, method="GET", payload=None):
    req = urllib.request.Request(
        f"{BASE}/{path}",
        data=json.dumps(payload).encode() if payload else None,
        method=method,
    )
    req.add_header("Authorization", f"Zoho-oauthtoken {tok}")
    req.add_header("X-com-zoho-store-organizationid", org)
    if payload:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=40) as response:
            return json.load(response), None
    except urllib.error.HTTPError as error:
        return None, f"HTTP {error.code}: {error.read().decode('utf-8', 'replace')[:220]}"
    except Exception as error:  # noqa: BLE001
        return None, str(error)


def build_payload(row, categories, existing=None):
    """Fields Zoho accepts on a product. Variant-level values are set separately."""
    # Only `description` is written on existing products. Their seo_title already
    # follows a branded 60-char pattern, seo_description is identical to ours, and
    # short_description is the same copy wrapped in theme markup - overwriting any
    # of the three would be a downgrade. New products have none of it, so they get
    # everything.
    body = {
        "description": row["Description"],
        "url": row["URL Handle"],
        "is_featured": row["Featured"].lower() == "true",
        "is_returnable": row["Is Returnable"].lower() == "true",
        # track_inventory is deliberately not sent. The API accepts it and then
        # silently keeps it False - inventory tracking has to be switched on in
        # Settings > Items first. Stock cannot be pushed until that is done.
        "show_in_storefront": True,
        "status": row["Status"],
    }
    category_id = categories.get(row["Category"])
    if category_id:
        body["category_id"] = category_id
    # Names already in Zoho are left alone - they are the merchandiser's wording
    # and in at least one case more descriptive than the Supabase value. New
    # products obviously need a name.
    if existing is None:
        body["name"] = row["Product Name"]
        body["product_type"] = "goods"
        body["product_short_description"] = row["Short Description"]
        body["seo_title"] = row["SEO Title"]
        body["seo_description"] = row["SEO Description"]
    return body


def variant_payload(row):
    body = {"rate": float(row["Rate"]), "sku": row["SKU"]}
    if row.get("HSN/SAC"):
        body["hsn_or_sac"] = row["HSN/SAC"]
    return body


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--only", help="only rows whose product name contains this")
    args = parser.parse_args()

    load_env()
    org = os.environ["ZOHO_ORG_ID"]
    tok = token()
    categories = json.load(open(CATEGORY_IDS))

    products, error = call("products", tok, org)
    if error:
        sys.exit(f"could not read products: {error}")
    by_sku = {}
    for product in products["products"]:
        detail, _ = call(f"products/{product['product_id']}", tok, org)
        full = (detail or {}).get("product", product)
        variant = (full.get("variants") or [{}])[0]
        sku = (variant.get("sku") or "").strip()
        if sku:
            by_sku[sku] = {
                "product_id": full["product_id"],
                "variant_id": variant.get("variant_id"),
                "name": full.get("name"),
                "rate": variant.get("rate"),
            }

    rows = list(csv.DictReader(open(CSV_PATH)))
    if args.only:
        needle = args.only.lower()
        rows = [r for r in rows if needle in r["Product Name"].lower()]
    if args.limit:
        rows = rows[: args.limit]

    mode = "APPLYING" if args.apply else "DRY RUN"
    print(f"{mode} - {len(rows)} row(s), org {org}\n")

    created = updated = failed = 0
    for row in rows:
        existing = by_sku.get(row["SKU"])
        action = "UPDATE" if existing else "CREATE"
        name = row["Product Name"][:40]

        if not args.apply:
            body = build_payload(row, categories, existing)
            extras = []
            if existing and existing["rate"] != float(row["Rate"]):
                extras.append(f"price {existing['rate']:.0f}->{float(row['Rate']):.0f}")
            if existing and existing["name"] != row["Product Name"]:
                extras.append(f"keeps name '{existing['name'][:28]}'")
            extras.append(f"cat={row['Category']}")
            extras.append(f"desc={len(row['Description'])}c")
            print(f"  {action:<7} {name:<42} {', '.join(extras)}")
            continue

        body = build_payload(row, categories, existing)
        if existing:
            result, error = call(f"products/{existing['product_id']}", tok, org, "PUT", body)
        else:
            body["variants"] = [variant_payload(row)]
            result, error = call("products", tok, org, "POST", body)

        if error:
            failed += 1
            print(f"  FAIL    {name:<42} {error}")
            continue

        # Price and SKU live on the variant, which is addressed at the top level
        # as variants/{id} - not nested under the product.
        if existing and existing.get("variant_id"):
            _, verror = call(
                f"variants/{existing['variant_id']}",
                tok, org, "PUT", variant_payload(row),
            )
            if verror:
                print(f"  WARN    {name:<42} product ok, variant failed: {verror}")

        if existing:
            updated += 1
        else:
            created += 1
        print(f"  {action:<7} {name:<42} ok")
        time.sleep(0.3)

    if args.apply:
        print(f"\nupdated {updated} | created {created} | failed {failed}")
    else:
        print("\nDry run only. Add --apply to write.")


if __name__ == "__main__":
    main()
