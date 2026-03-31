"""Fetch deals from Amazon PA-API and write Hugo content files.

Usage:
  Requires env vars: AMZ_PAAPI_ACCESS_KEY, AMZ_PAAPI_SECRET_KEY, AMZ_PARTNER_TAG
  Optional env: AMZ_MARKETPLACE (default from seeds.json or www.amazon.com)

Behavior:
  - If any required env is missing, exits cleanly (so CI won't fail while unconfigured).
  - Reads seeds.json for ASINs and per-ASIN tags; skip if seeds are still placeholders.
  - Calls PA-API GetItems in batches, writes markdown to content/deals/generated/<asin>.md
    with front matter compatible with existing templates.
"""
from __future__ import annotations
import json
import os
from datetime import datetime
from pathlib import Path
from typing import List
from xmlrpc.client import DateTime

import paapi5_python_sdk
from paapi5_python_sdk.api.default_api import DefaultApi
from paapi5_python_sdk.api_client import ApiClient
from paapi5_python_sdk.configuration import Configuration
from paapi5_python_sdk.models.get_items_request import GetItemsRequest
from paapi5_python_sdk.models.partner_type import PartnerType

ROOT = Path(__file__).resolve().parents[1]
SEEDS_PATH = ROOT / "scripts" / "seeds.json"
OUTPUT_DIR = ROOT / "content" / "deals" / "generated"

REQUIRED_ENVS = ["AMZ_PAAPI_ACCESS_KEY", "AMZ_PAAPI_SECRET_KEY", "AMZ_PARTNER_TAG"]


def has_required_env() -> bool:
    return all(os.getenv(key) for key in REQUIRED_ENVS)


def load_seeds():
    if not SEEDS_PATH.exists():
        print("[fetch_deals] seeds.json not found; skipping")
        return None
    data = json.loads(SEEDS_PATH.read_text())
    asins = data.get("asins", [])
    if not asins or any(x.startswith("EDIT_ME_") for x in asins):
        print("[fetch_deals] seeds.json not configured with real ASINs; skipping")
        return None
    marketplace = os.getenv("AMZ_MARKETPLACE") or data.get("marketplace") or "www.amazon.com"
    tags_map = data.get("tags", {})
    return marketplace, asins, tags_map


def client(marketplace: str) -> DefaultApi:
    config = Configuration()
    config.access_key = os.environ["AMZ_PAAPI_ACCESS_KEY"]
    config.secret_key = os.environ["AMZ_PAAPI_SECRET_KEY"]
    config.host = marketplace
    return DefaultApi(ApiClient(config))


def batches(iterable: List[str], size: int = 10):
    for i in range(0, len(iterable), size):
        yield iterable[i : i + size]


def write_deal(asin: str, payload: dict, tags: List[str]):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    title = payload.get("title", asin)
    url = payload.get("url", "")
    image = payload.get("image", "")
    price = payload.get("price")
    list_price = payload.get("list_price")
    discount = None
    if price and list_price and list_price > 0:
        discount = max(0.0, min(1.0, 1 - (price / list_price)))
    featured = bool(discount and discount >= 0.3)
    summary = payload.get("summary", "")
    now_iso = datetime.now(DateTime.UTC).isoformat() + "Z"
    fm_lines = [
        "+++",
        f"title = \"{title}\"",
        f"date = \"{now_iso}\"",
        f"list_price = {list_price if list_price is not None else 0}",
        f"sale_price = {price if price is not None else 0}",
        f"discount_pct = {discount if discount is not None else 0}",
        f"featured = {str(featured).lower()}",
        f"tags = {json.dumps(tags)}",
        f"categories = [\"deals\"]",
        f"affiliate_url = \"{url}\"",
        f"image = \"{image}\"",
        f"summary = \"{summary}\"",
        "+++",
        "",
        summary or "A curated pick from the latest feed.",
        "",
    ]
    outfile = OUTPUT_DIR / f"{asin}.md"
    outfile.write_text("\n".join(fm_lines))
    print(f"[fetch_deals] wrote {outfile}")


def main():
    if not has_required_env():
        print("[fetch_deals] Missing PA-API env vars; skipping")
        return
    seeds = load_seeds()
    if not seeds:
        return
    marketplace, asins, tags_map = seeds
    api = client(marketplace)
    partner_tag = os.environ["AMZ_PARTNER_TAG"]

    for batch in batches(asins, size=10):
        try:
            request = GetItemsRequest(
                partner_tag=partner_tag,
                partner_type=PartnerType.ASSOCIATES,
                marketplace=marketplace,
                item_ids=batch,
            )
            response = api.get_items(get_items_request=request)
        except Exception as exc:  # noqa: BLE001
            print(f"[fetch_deals] batch {batch} failed: {exc}")
            continue

        if not response.items_result:
            print(f"[fetch_deals] No items returned for {batch}")
            continue

        for item in response.items_result.items:
            asin = item.asin
            info = item.item_info
            offers = item.offers.listings if item.offers and item.offers.listings else []
            price = None
            list_price = None
            if offers:
                listing = offers[0]
                if listing.price and listing.price.amount is not None:
                    price = listing.price.amount
                if listing.price and listing.price.savings and listing.price.savings.amount is not None and listing.price.savings_basis is not None:
                    list_price = listing.price.savings_basis
                elif listing.price and listing.price.amount is not None and listing.price.savings and listing.price.savings.amount is not None:
                    list_price = listing.price.amount + listing.price.savings.amount
            payload = {
                "title": info.title.display_value if info and info.title else asin,
                "url": item.detail_page_url or "",
                "image": (item.images.primary.large.url if item.images and item.images.primary and item.images.primary.large else ""),
                "price": price,
                "list_price": list_price,
                "summary": info.features.display_values[0] if info and info.features and info.features.display_values else "",
            }
            write_deal(asin, payload, tags_map.get(asin, []))


if __name__ == "__main__":
    main()

