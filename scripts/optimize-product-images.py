#!/usr/bin/env python3
"""Download the product images and convert them to web-weight WebP.

The originals average ~2.9 MB, with several PNGs over 6 MB. At that size a
product page is unusable on mobile data. Converting to WebP at a sane width
keeps them visually identical at a fraction of the payload.

Needs cwebp (brew install webp).

Usage:
    python3 scripts/optimize-product-images.py
    python3 scripts/optimize-product-images.py --width 2000 --quality 85
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = os.path.join(ROOT, "zoho-import", "product-images.txt")


def slugify(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def width_of(path):
    """Pixel width via sips, which ships with macOS. 0 if it can't be read."""
    try:
        out = subprocess.run(["sips", "-g", "pixelWidth", path],
                             capture_output=True, text=True, timeout=20).stdout
        match = re.search(r"pixelWidth:\s*(\d+)", out)
        return int(match.group(1)) if match else 0
    except Exception:  # noqa: BLE001 - fall back to converting without resize
        return 0


def human(size):
    return f"{size / 1048576:.1f} MB" if size >= 1048576 else f"{size / 1024:.0f} KB"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--width", type=int, default=1600,
                        help="max width in px; height follows aspect ratio")
    parser.add_argument("--quality", type=int, default=82)
    parser.add_argument("--out-dir", default=os.path.join(ROOT, "zoho-import", "images"))
    args = parser.parse_args()

    if not shutil.which("cwebp"):
        sys.exit("cwebp not found. Install it with: brew install webp")
    if not os.path.exists(SOURCE):
        sys.exit(f"{SOURCE} not found. Run scripts/zoho-catalog-export.py first.")

    raw_dir = os.path.join(args.out_dir, "_originals")
    os.makedirs(args.out_dir, exist_ok=True)
    os.makedirs(raw_dir, exist_ok=True)

    entries = []
    with open(SOURCE) as handle:
        for line in handle:
            line = line.rstrip("\n")
            if "\t" in line:
                name, url = line.split("\t", 1)
                entries.append((name, url))

    counters, before, after, failures = {}, 0, 0, []
    print(f"{len(entries)} images -> WebP, max {args.width}px, quality {args.quality}\n")
    print(f"{'FILE':<46}{'BEFORE':>10}{'AFTER':>10}{'SAVED':>8}")
    print("-" * 74)

    for name, url in entries:
        slug = slugify(name)
        counters[slug] = counters.get(slug, 0) + 1
        stem = f"{slug}-{counters[slug]}"
        ext = os.path.splitext(url.split("?")[0])[1] or ".png"
        raw_path = os.path.join(raw_dir, stem + ext)
        out_path = os.path.join(args.out_dir, stem + ".webp")

        try:
            urllib.request.urlretrieve(url, raw_path)
        except Exception as error:  # noqa: BLE001 - keep going, report at the end
            failures.append((stem, f"download failed: {error}"))
            continue

        # Only ever downscale. Upscaling a small image inflates the file and
        # adds no detail - three of these were already web-sized.
        cmd = ["cwebp", "-quiet", "-q", str(args.quality)]
        if width_of(raw_path) > args.width:
            cmd += ["-resize", str(args.width), "0"]
        cmd += [raw_path, "-o", out_path]

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0 or not os.path.exists(out_path):
            failures.append((stem, result.stderr.strip()[:90] or "cwebp failed"))
            continue

        src = os.path.getsize(raw_path)
        dst = os.path.getsize(out_path)

        # If the conversion did not help, ship the original.
        note = ""
        if dst >= src:
            os.remove(out_path)
            out_path = os.path.join(args.out_dir, stem + ext)
            shutil.copyfile(raw_path, out_path)
            dst = src
            note = "  kept original"

        before += src
        after += dst
        pct = (1 - dst / src) * 100 if src else 0
        print(f"{stem[:45]:<46}{human(src):>10}{human(dst):>10}{pct:>7.0f}%{note}")

    print("-" * 74)
    if before:
        print(f"{'TOTAL':<46}{human(before):>10}{human(after):>10}"
              f"{(1 - after / before) * 100:>7.0f}%")
    if failures:
        print(f"\n{len(failures)} failed:")
        for stem, why in failures:
            print(f"  {stem}: {why}")

    print(f"\nWebP files: {args.out_dir}")
    print(f"Originals kept in: {raw_dir}")
    print("\nUpload the .webp files to Zoho. Filenames carry the product slug, "
          "and the -1/-2/-3 suffix is the display order.")


if __name__ == "__main__":
    main()
