#!/usr/bin/env python3
"""Reconcile the Supabase catalog against the Zoho Commerce catalog.

Supabase is treated as the source of truth for the D2C line: it is what the
live storefront actually sells. Products that exist only in Zoho (the
hospitality/amenities line) are reported but never touched.

Dry run by default - it prints the plan and writes it to JSON. Applying needs
write scopes on the Zoho token (ZohoCommerce.items.CREATE/UPDATE), which the
read-only audit token does not have.

Usage:
    python3 scripts/zoho-sync.py                  # dry run
    python3 scripts/zoho-sync.py --plan plan.json # dry run + save plan
    python3 scripts/zoho-sync.py --apply prices   # needs write scopes
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ZOHO_BASE = "https://commerce.zoho.in/store/api/v1"

# Names differ cosmetically between the two systems; map the known ones.
ALIASES = {
    "sunscreenbroadspectrumspf50": "sunscreenspf50",
    "advayaintensebrighteningelixirfaceserum": "intensebrighteningelixirfaceserum",
    "spf50": "sunscreenspf50",
}

# Zoho-only lines that must never be treated as "missing from Supabase".
ZOHO_ONLY_PREFIXES = ("basic", "premium")
NOT_A_PRODUCT = {"shippingcharges"}


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


def norm(name):
    key = re.sub(r"[^a-z0-9]", "", name.lower())
    return ALIASES.get(key, key)


def zoho_token():
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
        sys.exit(f"Zoho auth failed: {payload}")
    return payload["access_token"], payload.get("scope", "")


def zoho_get(path, token, org):
    req = urllib.request.Request(f"{ZOHO_BASE}/{path}")
    req.add_header("Authorization", f"Zoho-oauthtoken {token}")
    req.add_header("X-com-zoho-store-organizationid", org)
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def supabase_products():
    base = os.environ["SUPABASE_URL"].rstrip("/")
    key = os.environ["SUPABASE_SECRET_KEY"]
    url = f"{base}/rest/v1/products?select=name,price_inr,stock_quantity,is_active"
    req = urllib.request.Request(url)
    req.add_header("apikey", key)
    req.add_header("Authorization", f"Bearer {key}")
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def zoho_put(path, token, org, payload):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(f"{ZOHO_BASE}/{path}", data=body, method="PUT")
    req.add_header("Authorization", f"Zoho-oauthtoken {token}")
    req.add_header("X-com-zoho-store-organizationid", org)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.load(response), None
    except urllib.error.HTTPError as error:
        return None, f"HTTP {error.code}: {error.read().decode('utf-8', 'replace')[:150]}"
    except Exception as error:  # noqa: BLE001
        return None, str(error)


def content_updates(supa, zoho):
    """Field-level content that Supabase can fill in on products Zoho already has.

    Prices are deliberately excluded - the three conflicts are a pricing
    decision, not a sync problem.
    """
    supa_by = {norm(p["name"]): p for p in supa}
    updates = []
    for zp in zoho:
        sp = supa_by.get(norm(zp["name"]))
        if not sp:
            continue
        fields, notes = {}, []
        if sp.get("benefits_detail") and not zp.get("description"):
            fields["description"] = sp["benefits_detail"]
            notes.append(f"description({len(sp['benefits_detail'])}c)")
        if sp.get("one_line_summary") and not zp.get("product_short_description"):
            fields["product_short_description"] = sp["one_line_summary"]
            notes.append("short_desc")
        # Zoho auto-slugs look like one letter plus nine hex chars.
        if sp.get("id") and re.fullmatch(r"[a-z]\w{9}", str(zp.get("url", ""))):
            fields["url"] = sp["id"]
            notes.append(f"slug->{sp['id']}")
        if sp.get("one_line_summary") and not zp.get("seo_description"):
            fields["seo_description"] = sp["one_line_summary"][:320]
            notes.append("seo_desc")
        if not zp.get("seo_title"):
            fields["seo_title"] = sp["name"]
            notes.append("seo_title")
        if sp.get("is_best_seller") and not zp.get("is_featured"):
            fields["is_featured"] = True
            notes.append("featured")
        if fields:
            updates.append({
                "name": zp["name"],
                "product_id": zp["product_id"],
                "fields": fields,
                "notes": notes,
                "images_available": len(sp.get("images") or []),
                "images_in_zoho": image_count(zp),
            })
    return updates


def image_count(product):
    docs = product.get("documents") or []
    if not docs:
        variants = product.get("variants") or [{}]
        docs = variants[0].get("documents") or []
    return len(docs)


def build_plan(supa, zoho):
    supa_by = {norm(p["name"]): p for p in supa}
    zoho_by = {norm(p["name"]): p for p in zoho}

    plan = {"create": [], "price": [], "stock": [], "images": [], "zoho_only": [], "junk": []}

    for key, sp in sorted(supa_by.items()):
        zp = zoho_by.get(key)
        if not zp:
            plan["create"].append({
                "name": sp["name"],
                "price": float(sp["price_inr"]),
                "stock": sp.get("stock_quantity") or 0,
            })
            continue
        supa_price = float(sp["price_inr"])
        zoho_price = float(zp.get("min_rate") or 0)
        if supa_price != zoho_price:
            plan["price"].append({
                "name": sp["name"],
                "product_id": zp["product_id"],
                "zoho": zoho_price,
                "supabase": supa_price,
                "delta": round(supa_price - zoho_price, 2),
            })
        plan["stock"].append({
            "name": sp["name"],
            "product_id": zp["product_id"],
            "stock": sp.get("stock_quantity") or 0,
        })
        if image_count(zp) == 0:
            plan["images"].append({"name": sp["name"], "product_id": zp["product_id"]})

    for key, zp in sorted(zoho_by.items()):
        if key in supa_by:
            continue
        if key in NOT_A_PRODUCT:
            plan["junk"].append({"name": zp["name"], "product_id": zp["product_id"]})
        elif key.startswith(ZOHO_ONLY_PREFIXES):
            plan["zoho_only"].append({"name": zp["name"], "line": "amenities"})
        else:
            plan["zoho_only"].append({"name": zp["name"], "line": "other"})
    return plan


def report(plan):
    print(f"CREATE in Zoho ({len(plan['create'])}) - live in Supabase, absent from Zoho")
    for item in plan["create"]:
        print(f"   + {item['name']:<46} Rs{item['price']:<8.0f} stock={item['stock']}")

    print(f"\nPRICE conflicts ({len(plan['price'])}) - needs a human decision")
    for item in plan["price"]:
        arrow = "raise" if item["delta"] > 0 else "lower"
        print(f"   ! {item['name']:<46} Zoho Rs{item['zoho']:<7.0f} Supabase Rs{item['supabase']:<7.0f} ({arrow} by Rs{abs(item['delta']):.0f})")

    zero = [s for s in plan["stock"] if not s["stock"]]
    print(f"\nSTOCK to push ({len(plan['stock'])}), of which {len(zero)} are zero")
    for item in plan["stock"]:
        note = "   <- active in Zoho but out of stock" if not item["stock"] else ""
        print(f"   > {item['name']:<46} {item['stock']:>5}{note}")

    print(f"\nIMAGES missing in Zoho ({len(plan['images'])})")
    for item in plan["images"]:
        print(f"   ? {item['name']}")

    print(f"\nDELETE ({len(plan['junk'])}) - not real products")
    for item in plan["junk"]:
        print(f"   - {item['name']}  (id {item['product_id']})")

    print(f"\nZOHO-ONLY, left untouched ({len(plan['zoho_only'])})")
    for item in plan["zoho_only"]:
        print(f"   = {item['name']}  [{item['line']}]")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", metavar="FILE", help="write the plan as JSON")
    parser.add_argument("--apply", choices=["content"],
                        help="write content fields to existing Zoho products (needs write scopes)")
    parser.add_argument("--content", action="store_true",
                        help="show the content-enrichment plan for existing Zoho products")
    args = parser.parse_args()

    load_env()
    org = os.environ.get("ZOHO_ORG_ID")
    token, scope = zoho_token()
    supa = supabase_products()
    zoho = zoho_get("products", token, org)["products"]

    print(f"Supabase: {len(supa)} products | Zoho org {org}: {len(zoho)} products\n")

    if args.content or args.apply == "content":
        updates = content_updates(supa, zoho)
        print(f"CONTENT UPDATES for {len(updates)} existing Zoho products")
        print("(prices excluded - those 3 conflicts need a pricing decision)\n")
        for item in updates:
            imgs = ""
            if item["images_in_zoho"] == 0 and item["images_available"]:
                imgs = f"  [+{item['images_available']} images available, separate upload]"
            print(f"  {item['name'][:44]:<46} {', '.join(item['notes'])}{imgs}")

        if args.apply != "content":
            print("\nDry run. Re-run with --apply content once a write-scoped token is in place.")
            return

        writable = any(s in scope for s in ("items.CREATE", "items.UPDATE", "items.ALL"))
        if not writable:
            sys.exit(f"\nCannot apply - token is read-only.\n  scope: {scope}\n"
                     "  need: ZohoCommerce.items.UPDATE")
        print("\nApplying...")
        ok = 0
        for item in updates:
            _, error = zoho_put(f"products/{item['product_id']}", token, org, item["fields"])
            if error:
                print(f"  FAIL {item['name'][:44]:<46} {error}")
            else:
                ok += 1
                print(f"  OK   {item['name'][:44]:<46} {', '.join(item['notes'])}")
        print(f"\n{ok}/{len(updates)} products updated")
        return

    plan = build_plan(supa, zoho)
    report(plan)

    if args.plan:
        with open(args.plan, "w") as handle:
            json.dump(plan, handle, indent=2)
        print(f"\nPlan written to {args.plan}")

    if args.apply:
        writable = any(s in scope for s in ("items.CREATE", "items.UPDATE", "items.ALL"))
        if not writable:
            sys.exit(
                f"\nCannot apply: token scope is read-only.\n  scope: {scope}\n"
                "Generate a new grant code including ZohoCommerce.items.CREATE,ZohoCommerce.items.UPDATE"
            )
        sys.exit("Write path not enabled yet - rerun once a write-scoped token is in place.")


if __name__ == "__main__":
    main()
