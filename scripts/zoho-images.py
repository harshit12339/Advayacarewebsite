#!/usr/bin/env python3
"""Upload the optimised product images to Zoho.

Only touches products that currently have no image - anything already carrying
artwork is left alone. Files are matched to products by slug, and the -1/-2/-3
suffix sets the display order.

Endpoint shape was established by probing: POST products/{id}/images as
multipart with the file in a field literally named "image".

Usage:
    python3 scripts/zoho-images.py            # dry run
    python3 scripts/zoho-images.py --apply
    python3 scripts/zoho-images.py --apply --only "toner"
"""

import argparse
import glob
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from zoho_auth import access_token, org_id  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = "https://commerce.zoho.in/store/api/v1"
IMAGE_DIR = os.path.join(ROOT, "zoho-import", "images")


def get(path, tok, org):
    req = urllib.request.Request(f"{BASE}/{path}")
    req.add_header("Authorization", f"Zoho-oauthtoken {tok}")
    req.add_header("X-com-zoho-store-organizationid", org)
    with urllib.request.urlopen(req, timeout=40) as response:
        return json.load(response)


def upload(product_id, file_path, order, tok, org):
    data = open(file_path, "rb").read()
    boundary = "----" + uuid.uuid4().hex
    name = os.path.basename(file_path)
    body = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"attachment_order\"\r\n\r\n{order}\r\n"
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"{name}\"\r\n"
        f"Content-Type: image/webp\r\n\r\n"
    ).encode() + data + f"\r\n--{boundary}--\r\n".encode()

    req = urllib.request.Request(f"{BASE}/products/{product_id}/images", data=body, method="POST")
    req.add_header("Authorization", f"Zoho-oauthtoken {tok}")
    req.add_header("X-com-zoho-store-organizationid", org)
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            return json.load(response), None
    except urllib.error.HTTPError as error:
        return None, f"HTTP {error.code}: {error.read().decode('utf-8', 'replace')[:140]}"
    except Exception as error:  # noqa: BLE001
        return None, str(error)


def slugify(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--only", help="limit to products whose name contains this")
    args = parser.parse_args()

    tok, org = access_token(), org_id()
    targets = []
    for product in get("products", tok, org)["products"]:
        detail = get(f"products/{product['product_id']}", tok, org)["product"]
        variant = (detail.get("variants") or [{}])[0]
        if not (variant.get("sku") or "").strip():
            continue  # Zoho-only lines have no SKU and no source images
        if detail.get("documents"):
            continue  # already has artwork
        files = sorted(glob.glob(os.path.join(IMAGE_DIR, f"{slugify(detail['name'])}-*.webp")))
        if not files:
            continue
        if args.only and args.only.lower() not in detail["name"].lower():
            continue
        targets.append((detail["name"], detail["product_id"], files))

    total = sum(len(f) for _, _, f in targets)
    size = sum(os.path.getsize(f) for _, _, files in targets for f in files)
    print(f"{'UPLOADING' if args.apply else 'DRY RUN'} - "
          f"{len(targets)} products, {total} images, {size / 1048576:.1f} MB\n")

    for name, _, files in targets:
        print(f"  {name[:44]:<46}{len(files)} images")

    if not args.apply:
        print("\nDry run. Add --apply to upload.")
        return

    print()
    done = failed = 0
    for name, product_id, files in targets:
        for order, path in enumerate(files, start=1):
            _, error = upload(product_id, path, order, tok, org)
            if error:
                failed += 1
                print(f"  FAIL {os.path.basename(path):<44}{error}")
            else:
                done += 1
                print(f"  ok   {os.path.basename(path):<44}{os.path.getsize(path) // 1024} KB")
            time.sleep(0.4)

    print(f"\nuploaded {done} | failed {failed}")


if __name__ == "__main__":
    main()
