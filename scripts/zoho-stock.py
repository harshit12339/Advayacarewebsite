#!/usr/bin/env python3
"""Push Supabase stock levels into Zoho as a single opening-stock adjustment.

Two steps, because Zoho needs both:

1. Convert each product from a "sales" item to an "inventory" item. Sales items
   do not carry stock at all, and this cannot be done in bulk.
2. One inventory adjustment carrying every quantity as a line item - the whole
   catalog lands as a single dated record rather than twelve separate ones.

Using an adjustment rather than opening stock avoids Zoho's demand for a unit
cost, which the business has not supplied. Inventory is therefore tracked by
quantity but carries no valuation - worth revisiting with real costs later.

Usage:
    python3 scripts/zoho-stock.py            # dry run
    python3 scripts/zoho-stock.py --apply
"""

import argparse
import csv
import datetime
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from zoho_auth import access_token, org_id  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = "https://commerce.zoho.in/store/api/v1"
CSV_PATH = os.path.join(ROOT, "zoho-import", "zoho-products-import.csv")

# Read off the one product that was already an inventory item.
INVENTORY_ACCOUNT = "3712772000000000626"
PURCHASE_ACCOUNT = "3712772000000000567"


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
        return None, f"HTTP {error.code}: {error.read().decode('utf-8', 'replace')[:180]}"
    except Exception as error:  # noqa: BLE001
        return None, str(error)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    tok, org = access_token(), org_id()
    products, error = call("products", tok, org)
    if error:
        sys.exit(f"could not read products: {error}")

    current = {}
    for product in products["products"]:
        detail, _ = call(f"products/{product['product_id']}", tok, org)
        full = (detail or {}).get("product", product)
        variant = (full.get("variants") or [{}])[0]
        sku = (variant.get("sku") or "").strip()
        if sku:
            current[sku] = {
                "product_id": full["product_id"],
                "variant_id": variant.get("variant_id"),
                "name": full.get("name"),
                "is_inventory": variant.get("variant_type") == "inventory",
                "stock": variant.get("stock_on_hand"),
            }

    rows = [r for r in csv.DictReader(open(CSV_PATH)) if r["SKU"] in current]
    convert = [r for r in rows if not current[r["SKU"]]["is_inventory"]]
    stocked = [r for r in rows if int(r["Stock On Hand"] or 0) > 0]

    print(f"{'APPLYING' if args.apply else 'DRY RUN'} - {len(rows)} products\n")
    print(f"{'PRODUCT':<44}{'TYPE':<12}{'NOW':>6}{'TARGET':>8}")
    print("-" * 72)
    for row in rows:
        c = current[row["SKU"]]
        kind = "inventory" if c["is_inventory"] else "sales->inv"
        now = c["stock"] if c["stock"] is not None else "-"
        print(f"{c['name'][:43]:<44}{kind:<12}{str(now):>6}{row['Stock On Hand']:>8}")

    print(f"\nconvert to inventory item: {len(convert)}")
    print(f"stock lines in adjustment: {len(stocked)}  "
          f"(3 products are zero and need no line)")

    if not args.apply:
        print("\nDry run. Add --apply to write.")
        return

    print("\nstep 1 - converting to inventory items")
    failed = 0
    for row in convert:
        c = current[row["SKU"]]
        _, error = call(f"products/{c['product_id']}", tok, org, "PUT", {
            "track_inventory": True,
            "variant_type": "inventory",
            "can_be_purchased": True,
            "inventory_account_id": INVENTORY_ACCOUNT,
            "purchase_account_id": PURCHASE_ACCOUNT,
        })
        if error:
            failed += 1
            print(f"  FAIL {c['name'][:42]:<44}{error}")
        else:
            print(f"  ok   {c['name'][:42]}")
        time.sleep(0.3)

    print("\nstep 2 - one adjustment for the whole catalog")
    line_items = [
        {"item_id": current[r["SKU"]]["variant_id"],
         "quantity_adjusted": int(r["Stock On Hand"])}
        for r in stocked
        # already correct from an earlier run; adjusting again would double it
        if current[r["SKU"]]["stock"] != float(r["Stock On Hand"])
    ]
    if not line_items:
        print("  nothing to adjust - stock already matches")
        return

    result, error = call("inventoryadjustments", tok, org, "POST", {
        "date": datetime.date.today().isoformat(),
        "reason": "Opening stock from Supabase",
        "adjustment_type": "quantity",
        "line_items": line_items,
    })
    if error:
        sys.exit(f"  adjustment failed: {error}")
    adjustment = result.get("inventory_adjustment", {})
    print(f"  ok - {len(line_items)} lines, adjustment "
          f"{adjustment.get('inventory_adjustment_id', '?')}")
    if failed:
        print(f"\n{failed} conversion(s) failed - rerun to retry")


if __name__ == "__main__":
    main()
