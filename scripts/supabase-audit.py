#!/usr/bin/env python3
"""Audit the live Supabase project ahead of a Zoho migration.

Answers the two questions the repo cannot: how many rows each table actually
holds, and what the schema of `orders` / `order_items` is - those two tables
are only ever ALTERed by the migrations, never created, so their shape exists
nowhere in version control.

Credentials come from .env.local (gitignored):

    SUPABASE_URL=https://<project-ref>.supabase.co
    SUPABASE_SECRET_KEY=sb_secret_...      # or the legacy service_role JWT

Usage:
    python3 scripts/supabase-audit.py
    python3 scripts/supabase-audit.py --schema orders --schema order_items
    python3 scripts/supabase-audit.py --dump-schema supabase/schema-snapshot.json
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Grouped the way the migration decision needs them, not alphabetically.
GROUPS = [
    ("Storefront - migrate into Zoho", [
        "products", "blog_posts", "blog_drafts", "member_comments",
    ]),
    ("Money - reconcile before cutover", [
        "gift_cards", "gift_card_transactions", "general_coupons",
        "general_coupon_usages", "member_coupons", "coupon_redemptions",
    ]),
    ("Orders - decide migrate vs archive", [
        "orders", "order_items", "order_status_events",
        "order_inventory_reservations", "product_stock_events",
    ]),
    ("Members - password reset required", [
        "membership_profiles", "membership_recommendation_runs",
    ]),
    ("Affiliates - stays in Supabase", [
        "affiliate_applications", "affiliate_profiles", "affiliate_coupons",
    ]),
    ("B2B - stays in Supabase", [
        "b2b_accounts", "b2b_contacts", "b2b_opportunities", "b2b_activities",
        "b2b_outreach", "b2b_trade_terms", "b2b_quotes", "b2b_credits",
        "b2b_suppressions", "b2b_submission_attempts", "general_inquiries",
    ]),
]


def load_env():
    path = os.path.join(ROOT, ".env.local")
    if not os.path.exists(path):
        sys.exit("No .env.local found - see the docstring for what to put in it.")
    with open(path) as handle:
        for line in handle:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def request(url, key, extra_headers=None):
    req = urllib.request.Request(url, method="GET")
    req.add_header("apikey", key)
    req.add_header("Authorization", f"Bearer {key}")
    for header, value in (extra_headers or {}).items():
        req.add_header(header, value)
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return response, json.load(response), None
    except urllib.error.HTTPError as error:
        return None, None, f"HTTP {error.code}: {error.read().decode('utf-8', 'replace')[:160]}"
    except Exception as error:  # noqa: BLE001 - report and keep auditing
        return None, None, str(error)


def count_rows(base, key, table):
    """PostgREST returns the total in Content-Range when asked for an exact count."""
    url = f"{base}/rest/v1/{table}?select=*&limit=0"
    response, _, error = request(url, key, {"Prefer": "count=exact"})
    if error:
        return None, error
    content_range = response.headers.get("Content-Range", "")
    if "/" in content_range:
        total = content_range.rsplit("/", 1)[1]
        return (None if total == "*" else int(total)), None
    return None, "no Content-Range header"


def get_openapi(base, key):
    _, payload, error = request(f"{base}/rest/v1/", key)
    if error:
        sys.exit(f"Could not read the PostgREST schema: {error}")
    return payload


def columns_for(spec, table):
    definitions = spec.get("definitions") or spec.get("components", {}).get("schemas", {})
    entry = definitions.get(table)
    if not entry:
        return None
    required = set(entry.get("required") or [])
    out = []
    for name, meta in (entry.get("properties") or {}).items():
        kind = meta.get("format") or meta.get("type") or "?"
        flags = []
        if name in required:
            flags.append("required")
        description = meta.get("description") or ""
        if "Primary Key" in description:
            flags.append("PK")
        if "Foreign Key" in description:
            flags.append("FK")
        out.append((name, kind, ", ".join(flags)))
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--schema", action="append", default=[],
                        help="print full column list for this table (repeatable)")
    parser.add_argument("--dump-schema", metavar="FILE",
                        help="write the whole PostgREST schema to a JSON file")
    args = parser.parse_args()

    load_env()
    base = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
    key = os.environ.get("SUPABASE_SECRET_KEY") or os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not base or not key:
        sys.exit("Set SUPABASE_URL and SUPABASE_SECRET_KEY in .env.local")

    spec = get_openapi(base, key)
    definitions = spec.get("definitions") or spec.get("components", {}).get("schemas", {})
    known = set(definitions)
    print(f"Connected to {base}")
    print(f"PostgREST exposes {len(known)} tables/views\n")

    listed = {t for _, tables in GROUPS for t in tables}
    total_rows = 0
    for title, tables in GROUPS:
        print(title)
        for table in tables:
            if table not in known:
                print(f"  {table:<34} -- not exposed")
                continue
            count, error = count_rows(base, key, table)
            if error:
                print(f"  {table:<34} !  {error}")
                continue
            total_rows += count or 0
            flag = "" if count else "   <- empty"
            print(f"  {table:<34} {count:>7}{flag}")
        print()

    extra = sorted(known - listed)
    if extra:
        print(f"Not in the migration plan ({len(extra)}): {', '.join(extra)}\n")

    print(f"Total rows across planned tables: {total_rows}\n")

    for table in args.schema:
        cols = columns_for(spec, table)
        print(f"=== {table} ===")
        if not cols:
            print("  not exposed via PostgREST\n")
            continue
        for name, kind, flags in cols:
            print(f"  {name:<32} {kind:<28} {flags}")
        print()

    if args.dump_schema:
        with open(args.dump_schema, "w") as handle:
            json.dump(spec, handle, indent=2)
        print(f"Full schema written to {args.dump_schema}")


if __name__ == "__main__":
    main()
