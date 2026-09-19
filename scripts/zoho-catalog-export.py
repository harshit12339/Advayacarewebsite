#!/usr/bin/env python3
"""Build a Zoho Commerce import file from the live Supabase catalog.

Zoho's product importer takes a CSV and lets you map columns on the way in, so
this avoids needing API write scopes entirely. Products already in Zoho keep
their identity through the SKU column; new ones are created.

Outputs:
    zoho-products-import.csv   one row per product, ready to import
    product-images.txt         image URLs to download and upload
    catalog-review.md          human-readable sheet for checking before import

Usage:
    python3 scripts/zoho-catalog-export.py --out-dir ./zoho-import
"""

import argparse
import csv
import json
import os
import re
import sys
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# HSN codes. "zoho" entries were read from the existing Zoho records and are
# taken as correct. "suggested" entries follow the same chapter pattern - 3304
# beauty preparations, 3305 hair, 3401 soap - and are starting points for the
# accountant, not answers. Every suggested code must be confirmed before the
# store goes live, or GST is charged at the wrong rate.
HSN = {
    "24k-gold-glowing-gel":                 ("33049990", "zoho"),
    "rosemary-water-hair-spray":            ("33059090", "zoho"),
    "sunscreen-spf50":                      ("33049990", "suggested"),  # Zoho holds a 4-digit stub
    "24k-gold-elixir-face-oil-serum":       ("33049990", "suggested"),
    "glow-skin-toner":                      ("33049990", "suggested"),
    "intense-brightening-elixir-face-serum": ("33049990", "suggested"),
    "tivra-skin-peeling-oil":               ("33049990", "suggested"),
    "intense-body-butter":                  ("33049990", "suggested"),
    "cleansing-delight-soap-pack-of-2":     ("34011190", "suggested"),
    "jeevita-facial-soap-pack-of-2":        ("34011190", "suggested"),
    "svastha-soap":                         ("34011190", "suggested"),
    "001":                                  ("", "unknown"),  # pet grooming - no clear precedent
}

# The live storefront navigates by Face / Body / Hair / Pet Care, so that is the
# scheme customers already know and the one Zoho should carry. Zoho's existing
# SKIN and HAIR categories belong to the hospitality store that was built first;
# they are left alone here and should be retired separately once the amenity
# products are recategorised.
CATEGORY = {
    "Face": "Face",
    "Body": "Body",
    "Hair": "Hair",
    "Pet Care": "Pet Care",
}
# Categories that must exist in Zoho before the import runs.
NEW_CATEGORIES = {"Face", "Body", "Hair", "Pet Care"}

# Zoho already holds these SKUs and they do not follow the ADV- convention.
# Import matches on SKU, so reuse Zoho's value or the import creates a duplicate.
SKU_OVERRIDE = {
    "intense-body-butter": "INT-INT",
}

COLUMNS = [
    "SKU", "Product Name", "Description", "Short Description",
    "Rate", "Compare At Price", "Category", "Tags", "URL Handle",
    "SEO Title", "SEO Description", "Specifications",
    "Stock On Hand", "Reorder Level", "HSN/SAC", "Weight (g)",
    "Status", "Track Inventory", "Is Returnable", "Featured",
    "Image URLs",
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


def supabase_products():
    base = os.environ["SUPABASE_URL"].rstrip("/")
    key = os.environ["SUPABASE_SECRET_KEY"]
    req = urllib.request.Request(f"{base}/rest/v1/products?select=*&order=name")
    req.add_header("apikey", key)
    req.add_header("Authorization", f"Bearer {key}")
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def sku_for(product):
    override = SKU_OVERRIDE.get(product.get("id"))
    if override:
        return override
    slug = re.sub(r"[^A-Z0-9]+", "-", product["name"].upper()).strip("-")
    return f"ADV-{slug}"[:48]


def slug_for(product):
    """Prefer the existing id, but only when it is actually a usable slug.

    One product carries the id "001", which would become the storefront URL.
    Fall back to a slug built from the name in that case.
    """
    current = (product.get("id") or "").strip()
    if len(current) > 6 and "-" in current and not current.isdigit():
        return current
    return re.sub(r"[^a-z0-9]+", "-", product["name"].lower()).strip("-")


# Zoho rejects descriptions containing emoji (error 70013). Strip pictographs,
# dingbats, gender signs, variation selectors and zero-width joiners, but keep
# real typography - curly quotes, em dashes and bullets are fine and in use.
EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF"   # pictographs, symbols, supplemental
    "☀-➿"            # misc symbols and dingbats
    "⬀-⯿"            # arrows and misc symbols
    "♀♂"             # gender signs
    "︀-️"            # variation selectors
    "‍]"                  # zero width joiner
)


def clean(text):
    """Collapse the tab/newline soup from the original CMS and drop emoji."""
    if not text:
        return ""
    text = text.replace("\t", " ").replace("\r", "")
    text = EMOJI.sub("", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r" +\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def row_for(product):
    slug = slug_for(product)
    tags = product.get("filter_tags") or []
    category = CATEGORY.get(tags[0], "") if tags else ""
    images = product.get("images") or []
    compare = product.get("compare_at_price") or 0
    return {
        "SKU": sku_for(product),
        "Product Name": product["name"],
        "Description": clean(product.get("benefits_detail")),
        "Short Description": clean(product.get("one_line_summary")),
        "Rate": f"{float(product.get('price_inr') or 0):.2f}",
        "Compare At Price": f"{float(compare):.2f}" if compare else "",
        "Category": category,
        "Tags": ", ".join(tags),
        "URL Handle": slug,
        "SEO Title": product["name"],
        "SEO Description": clean(product.get("one_line_summary"))[:320],
        "Specifications": clean(product.get("ingredients")),
        "Stock On Hand": product.get("stock_quantity") or 0,
        "Reorder Level": product.get("low_stock_threshold") or "",
        "HSN/SAC": HSN.get(slug, ("", "unknown"))[0],
        "Weight (g)": "",  # no source in Supabase; needed for courier rates
        "Status": "active" if product.get("is_active") else "inactive",
        "Track Inventory": "true",
        "Is Returnable": "true",
        "Featured": "true" if product.get("is_best_seller") else "false",
        "Image URLs": " | ".join(images),
    }


def write_review(path, rows, products):
    lines = ["# Catalog review sheet", ""]
    lines.append(f"{len(rows)} products prepared for Zoho import. "
                 "Check the flagged items before importing.\n")

    lines.append("## HSN codes — needs sign-off before going live\n")
    lines.append("Only the two marked `zoho` are confirmed. The rest follow the same "
                 "chapter pattern (3304 beauty, 3305 hair, 3401 soap) and are starting "
                 "points for the accountant. A wrong code means GST at the wrong rate.\n")
    lines.append("| Product | HSN | Source | Confirmed? |")
    lines.append("|---|---|---|---|")
    for product in products:
        code, source = HSN.get(product.get("id"), ("", "unknown"))
        mark = "yes" if source == "zoho" else "**NO**"
        lines.append(f"| {product['name']} | {code or '—'} | {source} | {mark} |")
    lines.append("")

    missing_hsn = [r["Product Name"] for r in rows if not r["HSN/SAC"]]
    missing_img = [r["Product Name"] for r in rows if not r["Image URLs"]]
    no_cat = [r["Product Name"] for r in rows if not r["Category"]]
    zero_stock = [r["Product Name"] for r in rows if str(r["Stock On Hand"]) == "0"]

    def block(title, items, note):
        if not items:
            return
        lines.append(f"## {title} ({len(items)})\n")
        lines.append(f"{note}\n")
        lines.extend(f"- {name}" for name in items)
        lines.append("")

    block("Needs an HSN code", missing_hsn,
          "Blank on purpose. A tax code is the accountant's call - fill these in "
          "before import or GST will not compute correctly.")
    block("No image", missing_img,
          "No image exists in the current catalog either. These need photography.")
    block("No category", no_cat,
          "No filter tag set in the source data.")
    block("Zero stock but marked active", zero_stock,
          "These would be sellable in Zoho with nothing to ship. Confirm the "
          "stock figure or set them inactive.")

    lines.append("## Every product\n")
    lines.append("| Product | Price | Stock | Category | Images | Description |")
    lines.append("|---|---|---|---|---|---|")
    for r in rows:
        imgs = len([u for u in r["Image URLs"].split(" | ") if u])
        desc = len(r["Description"])
        lines.append(
            f"| {r['Product Name']} | Rs{r['Rate']} | {r['Stock On Hand']} | "
            f"{r['Category'] or '-'} | {imgs or '-'} | {desc or '-'} chars |"
        )
    with open(path, "w") as handle:
        handle.write("\n".join(lines) + "\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", default="zoho-import")
    args = parser.parse_args()

    load_env()
    products = supabase_products()
    rows = [row_for(p) for p in products]

    os.makedirs(args.out_dir, exist_ok=True)
    csv_path = os.path.join(args.out_dir, "zoho-products-import.csv")
    with open(csv_path, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    img_path = os.path.join(args.out_dir, "product-images.txt")
    with open(img_path, "w") as handle:
        for product in products:
            for url in product.get("images") or []:
                handle.write(f"{product['name']}\t{url}\n")

    review_path = os.path.join(args.out_dir, "catalog-review.md")
    write_review(review_path, rows, products)

    total_images = sum(len(p.get("images") or []) for p in products)
    total_copy = sum(len(r["Description"]) + len(r["Specifications"]) for r in rows)
    print(f"{len(rows)} products written to {csv_path}")
    print(f"{total_images} image URLs written to {img_path}")
    print(f"{total_copy:,} characters of description and ingredient copy included")
    print(f"Review sheet: {review_path}")


if __name__ == "__main__":
    main()
