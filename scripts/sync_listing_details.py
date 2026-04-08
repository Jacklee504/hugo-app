"""Sync live deal listing fields from Amazon PA-API into deal front matter.

Usage:
  python scripts/sync_listing_details.py

Required env vars:
  AMZ_PAAPI_ACCESS_KEY
  AMZ_PAAPI_SECRET_KEY
  AMZ_PARTNER_TAG
Optional:
  AMZ_MARKETPLACE (default: www.amazon.co.uk)

Behavior:
  - Scans content/deals/*.md (excluding _index.md)
  - Extracts ASIN from `asin`, `product_url`, or `affiliate_url`
  - Fetches item details via PA-API
  - Upserts listing-backed fields:
      listing_title
      listing_summary
      listing_image
      listing_url
      listing_sale_price
      listing_list_price
      listing_discount_pct
      listing_synced_at
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List

try:
    from amazon.paapi import AmazonApi, AmazonException
except ModuleNotFoundError:
    AmazonApi = Any  # type: ignore[assignment]

    class AmazonException(Exception):
        pass

ROOT = Path(__file__).resolve().parents[1]
DEALS_DIR = ROOT / "content" / "deals"

REQUIRED_ENVS = ["AMZ_PAAPI_ACCESS_KEY", "AMZ_PAAPI_SECRET_KEY", "AMZ_PARTNER_TAG"]


def has_required_env() -> bool:
    return all(os.getenv(key) for key in REQUIRED_ENVS)


def host_to_country(host: str) -> str:
    host = host.lower()
    if host.endswith(".co.uk") or host.endswith(".uk"):
        return "UK"
    if host.endswith(".de"):
        return "DE"
    if host.endswith(".fr"):
        return "FR"
    if host.endswith(".ca"):
        return "CA"
    if host.endswith(".co.jp") or host.endswith(".jp"):
        return "JP"
    if host.endswith(".in"):
        return "IN"
    if host.endswith(".it"):
        return "IT"
    if host.endswith(".es"):
        return "ES"
    if host.endswith(".com.au"):
        return "AU"
    if host.endswith(".com.br"):
        return "BR"
    if host.endswith(".com.mx"):
        return "MX"
    if host.endswith(".com.tr"):
        return "TR"
    if host.endswith(".ae") or host.endswith(".sa"):
        return "AE"
    if host.endswith(".nl"):
        return "NL"
    if host.endswith(".se"):
        return "SE"
    if host.endswith(".pl"):
        return "PL"
    if host.endswith(".eg"):
        return "EG"
    if host.endswith(".be"):
        return "BE"
    if host.endswith(".ie"):
        return "IE"
    return "US"


def client(country: str) -> AmazonApi:
    return AmazonApi(
        os.environ["AMZ_PAAPI_ACCESS_KEY"],
        os.environ["AMZ_PAAPI_SECRET_KEY"],
        os.environ["AMZ_PARTNER_TAG"],
        country=country,
    )


def batches(items: List[str], size: int = 10) -> Iterable[List[str]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def toml_escape(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def value_to_toml(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return f"{value:.6f}".rstrip("0").rstrip(".")
    return f'"{toml_escape(value)}"'


def split_front_matter(raw: str):
    if not raw.startswith("+++\n"):
        return None
    end = raw.find("\n+++\n", 4)
    if end == -1:
        return None
    front = raw[4:end]
    body = raw[end + 5 :]
    return front, body


def upsert_line(front: str, key: str, value: Any) -> str:
    rendered = f"{key} = {value_to_toml(value)}"
    pattern = re.compile(rf"^{re.escape(key)}\s*=.*$", re.MULTILINE)
    if pattern.search(front):
        return pattern.sub(rendered, front, count=1)
    front = front.rstrip("\n")
    return f"{front}\n{rendered}\n"


def extract_asin(text: str) -> str | None:
    m = re.search(r"\b([A-Z0-9]{10})\b", text)
    return m.group(1) if m else None


def asin_from_front(front: str) -> str | None:
    asin_line = re.search(r'^asin\s*=\s*"([A-Z0-9]{10})"\s*$', front, re.MULTILINE)
    if asin_line:
        return asin_line.group(1)

    for key in ("product_url", "affiliate_url", "listing_url"):
        m = re.search(rf'^{key}\s*=\s*"([^"]+)"\s*$', front, re.MULTILINE)
        if not m:
            continue
        url = m.group(1)
        path_asin = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})(?:[/?]|$)", url)
        if path_asin:
            return path_asin.group(1)
        fallback = extract_asin(url)
        if fallback:
            return fallback
    return None


def extract_prices(product: Any):
    raw_offers: Dict = (product.raw or {}).get("Offers") or {}
    listings = raw_offers.get("Listings") or []
    if not listings:
        return None, None, None

    listing = listings[0]
    price_info = listing.get("Price") or {}
    savings = price_info.get("Savings") or {}
    amount = price_info.get("Amount")
    basis = price_info.get("SavingsBasis")

    sale = amount if amount is not None else None
    list_price = basis if basis is not None else None
    if list_price is None and amount is not None and savings.get("Amount") is not None:
        list_price = amount + savings["Amount"]

    discount = None
    if sale is not None and list_price and list_price > 0:
        discount = max(0.0, min(1.0, 1 - (sale / list_price)))

    return sale, list_price, discount


def main() -> None:
    if not has_required_env():
        print("[sync_listing_details] Missing PA-API env vars; skipping")
        return

    if AmazonApi is Any:
        print("[sync_listing_details] python-amazon-paapi is not installed; skipping")
        return

    marketplace = os.getenv("AMZ_MARKETPLACE", "www.amazon.co.uk")
    country = host_to_country(marketplace)
    api = client(country)

    candidates: List[tuple[Path, str, str]] = []
    for path in sorted(DEALS_DIR.glob("*.md")):
        if path.name == "_index.md":
            continue
        raw = path.read_text()
        split = split_front_matter(raw)
        if not split:
            continue
        front, _ = split
        asin = asin_from_front(front)
        if asin:
            candidates.append((path, front, asin))

    if not candidates:
        print("[sync_listing_details] No deals with ASIN or Amazon URL found")
        return

    asins = sorted(set(asin for _, _, asin in candidates))
    by_asin: Dict[str, Any] = {}

    for batch in batches(asins):
        try:
            products = api.get_items(batch)
        except AmazonException as exc:
            print(f"[sync_listing_details] batch failed {batch}: {exc}")
            continue
        for product in products or []:
            by_asin[product.asin] = product

    synced = 0
    now_iso = datetime.now(timezone.utc).isoformat()

    for path, front, asin in candidates:
        product = by_asin.get(asin)
        if not product:
            print(f"[sync_listing_details] no listing data for {path.name} ({asin})")
            continue

        sale_price, list_price, discount_pct = extract_prices(product)
        next_front = front
        next_front = upsert_line(next_front, "asin", asin)
        next_front = upsert_line(next_front, "listing_title", product.title or "")
        next_front = upsert_line(next_front, "listing_summary", (product.features[0] if product.features else ""))
        next_front = upsert_line(next_front, "listing_image", (product.images.large if product.images else ""))
        next_front = upsert_line(next_front, "listing_url", product.url or "")
        next_front = upsert_line(next_front, "listing_sale_price", sale_price or 0)
        next_front = upsert_line(next_front, "listing_list_price", list_price or 0)
        next_front = upsert_line(next_front, "listing_discount_pct", discount_pct or 0)
        next_front = upsert_line(next_front, "listing_synced_at", now_iso)

        if next_front != front:
            raw = path.read_text()
            split = split_front_matter(raw)
            if not split:
                continue
            _, body = split
            path.write_text(f"+++\n{next_front.rstrip()}\n+++\n{body}")
            synced += 1
            print(f"[sync_listing_details] updated {path.relative_to(ROOT)}")

    print(f"[sync_listing_details] done: {synced} file(s) updated")


if __name__ == "__main__":
    main()
