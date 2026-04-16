"""Fetch discounted products from Amazon PA-API into a draft approval queue.

Usage:
  Requires env vars: AMZ_PAAPI_ACCESS_KEY, AMZ_PAAPI_SECRET_KEY, AMZ_PARTNER_TAG
  Optional env: AMZ_MARKETPLACE (domain like www.amazon.co.uk or www.amazon.com)

Behavior:
  - If any required env is missing, exits cleanly (so CI won't fail while unconfigured).
  - Reads seeds.json for ASINs and per-ASIN tags; skip if seeds are still placeholders.
  - Calls PA-API in batches and writes draft markdown files to
    review-queue/deals/<asin>.md for manual approval.
  - Prioritizes items that already have an affiliate-capable URL.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

try:
    from amazon.paapi import AmazonApi, AmazonException
except ModuleNotFoundError:
    AmazonApi = Any  # type: ignore[assignment]

    class AmazonException(Exception):
        pass

ROOT = Path(__file__).resolve().parents[1]
SEEDS_PATH = ROOT / "scripts" / "seeds.json"
QUALITY_POLICY_PATH = ROOT / "scripts" / "quality_policy.json"
EXACT_SUBSCRIPTIONS_PATH = ROOT / ".state" / "exact-item-subscriptions.json"
OUTPUT_DIR = ROOT / "review-queue" / "deals"

REQUIRED_ENVS = ["AMZ_PAAPI_ACCESS_KEY", "AMZ_PAAPI_SECRET_KEY", "AMZ_PARTNER_TAG"]
MIN_DISCOUNT = 0.20
MIN_SALE_PRICE = 25.0


DEFAULT_QUALITY_POLICY = {
    "require_reputable_brand": True,
    "allowed_brands": [
        "Amazon", "Anker", "Sony", "Logitech", "Corsair", "LEGO", "Soundcore",
        "Apple", "Samsung", "Philips", "TP-Link", "Bose", "JBL", "SanDisk"
    ],
    "blocked_brand_terms": [
        "generic", "unknown", "no brand", "unbranded"
    ],
    "require_fulfilled_by_amazon_or_amazon_seller": True,
    "trusted_seller_terms": ["amazon"],
}


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


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return default


def load_quality_policy() -> dict:
    if not QUALITY_POLICY_PATH.exists():
        return DEFAULT_QUALITY_POLICY
    try:
        user = json.loads(QUALITY_POLICY_PATH.read_text())
        merged = dict(DEFAULT_QUALITY_POLICY)
        merged.update(user)
        return merged
    except json.JSONDecodeError:
        print("[fetch_deals] quality_policy.json invalid; using defaults")
        return DEFAULT_QUALITY_POLICY


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


def batches(iterable: List[str], size: int = 10):
    for i in range(0, len(iterable), size):
        yield iterable[i : i + size]


def is_affiliate_ready(url: str) -> bool:
    if not url:
        return False
    normalized = url.lower()
    return "tag=" in normalized or "amzn.to" in normalized


def extract_asin(value: str) -> str | None:
    m = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})(?:[/?]|$)", value, re.IGNORECASE)
    if m:
        return m.group(1).upper()
    m = re.search(r"\b([A-Z0-9]{10})\b", value or "", re.IGNORECASE)
    if m:
        return m.group(1).upper()
    return None


def dedupe_keep_order(values: List[str]) -> List[str]:
    seen: set[str] = set()
    ordered: List[str] = []
    for value in values:
        key = (value or "").strip().upper()
        if not key or key in seen:
            continue
        seen.add(key)
        ordered.append(key)
    return ordered


def parse_exact_item_inputs(raw_items: Any) -> List[str]:
    if isinstance(raw_items, list):
        values = raw_items
    else:
        values = re.split(r"[\n,;]+", str(raw_items or ""))
    parsed: List[str] = []
    seen: set[str] = set()
    for value in values:
        item = str(value or "").strip()
        if not item:
            continue
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        parsed.append(item)
    return parsed


def load_exact_item_requests(path: Path = EXACT_SUBSCRIPTIONS_PATH) -> tuple[List[str], List[str]]:
    payload = load_json(path, {})
    if not isinstance(payload, dict):
        return [], []

    direct_asins: List[str] = []
    queries: List[str] = []
    for _, record in payload.items():
        if not isinstance(record, dict):
            continue
        for item in parse_exact_item_inputs(record.get("exact_items", [])):
            asin = extract_asin(item)
            if asin:
                direct_asins.append(asin)
            elif len(item.strip()) >= 3:
                queries.append(item.strip())

    return dedupe_keep_order(direct_asins), dedupe_keep_order(queries)


def search_asins_for_queries(api: AmazonApi, queries: List[str], max_queries: int = 12) -> List[str]:
    resolved: List[str] = []

    for query in queries[:max_queries]:
        products = None
        attempts = [
            {"keywords": query, "search_index": "All", "item_count": 5},
            {"keywords": query, "search_index": "All"},
            {"keywords": query},
        ]
        for kwargs in attempts:
            try:
                products = api.search_items(**kwargs)
                if products:
                    break
            except TypeError:
                continue
            except AmazonException as exc:
                print(f"[fetch_deals] exact-item search failed for '{query}': {exc}")
                products = []
                break
            except Exception as exc:  # noqa: BLE001
                print(f"[fetch_deals] exact-item search error for '{query}': {exc}")
                products = []
                break

        for product in products or []:
            asin = str(getattr(product, "asin", "") or "").strip().upper()
            if asin:
                resolved.append(asin)

    return dedupe_keep_order(resolved)


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", " ", (value or "").lower())).strip()


def contains_any(text: str, terms: List[str]) -> bool:
    n = normalize_text(text)
    for term in terms or []:
        if normalize_text(term) in n:
            return True
    return False


def extract_brand(product) -> str:
    raw = product.raw or {}
    brand = ""
    byline = (((raw.get("ItemInfo") or {}).get("ByLineInfo") or {}).get("Brand") or {})
    if isinstance(byline, dict):
        brand = byline.get("DisplayValue") or ""
    if not brand:
        brand = getattr(product, "brand", "") or ""
    return str(brand).strip()


def is_reputable_brand(brand: str, policy: dict) -> bool:
    if not policy.get("require_reputable_brand", True):
        return True
    if not brand:
        return False
    if contains_any(brand, policy.get("blocked_brand_terms", [])):
        return False
    allowed = policy.get("allowed_brands", [])
    if not allowed:
        return True
    return contains_any(brand, allowed)


def is_trusted_fulfillment(listing: dict, policy: dict) -> bool:
    if not policy.get("require_fulfilled_by_amazon_or_amazon_seller", True):
        return True

    if listing.get("IsFulfilledByAmazon") is True:
        return True

    merchant_name = ((listing.get("MerchantInfo") or {}).get("Name") or "")
    if contains_any(merchant_name, policy.get("trusted_seller_terms", ["amazon"])):
        return True

    # Some responses include an OfferProgramEligibility dict with Prime flags.
    ope = listing.get("OfferProgramEligibility") or {}
    if isinstance(ope, dict) and ope.get("IsPrimeExclusive") is True:
        return True

    return False


def toml_escape(value: str) -> str:
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", " ")
    )


def write_deal(asin: str, payload: dict, tags: List[str]) -> bool:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    title = payload.get("title", asin)
    url = payload.get("url", "")
    image = payload.get("image", "")
    price = payload.get("price")
    list_price = payload.get("list_price")
    summary = payload.get("summary", "")

    discount = 0.0
    if price and list_price and list_price > 0:
        discount = max(0.0, min(1.0, 1 - (price / list_price)))

    if discount <= MIN_DISCOUNT:
        print(f"[fetch_deals] skip {asin}: discount is not above {MIN_DISCOUNT:.0%}")
        return False

    if not isinstance(price, (int, float)):
        print(f"[fetch_deals] skip {asin}: missing sale price")
        return False

    if price <= MIN_SALE_PRICE:
        print(f"[fetch_deals] skip {asin}: sale price is not above €{MIN_SALE_PRICE:.2f}")
        return False

    featured = discount >= 0.3
    affiliate_ready = is_affiliate_ready(url)

    now_iso = datetime.utcnow().isoformat() + "Z"
    body_summary = summary or "Candidate deal from automated pull."

    fm_lines = [
        "+++",
        f'title = "{toml_escape(title)}"',
        f'date = "{now_iso}"',
        "draft = true",
        'review_status = "pending"',
        f'asin = "{toml_escape(asin)}"',
        f"affiliate_ready = {str(affiliate_ready).lower()}",
        f"list_price = {list_price if list_price is not None else 0}",
        f"sale_price = {price if price is not None else 0}",
        f"discount_pct = {discount:.6f}",
        f"featured = {str(featured).lower()}",
        f"tags = {json.dumps(tags)}",
        'categories = ["deals"]',
        f'affiliate_url = "{toml_escape(url)}"',
        f'image = "{toml_escape(image)}"',
        f'summary = "{toml_escape(body_summary)}"',
        'time_left = "2d"',
        "+++",
        "",
        body_summary,
        "",
        "Review notes:",
        "- Confirm pricing and availability before promoting.",
        "- Keep only products you would genuinely recommend.",
        "",
    ]

    outfile = OUTPUT_DIR / f"{asin}.md"
    outfile.write_text("\n".join(fm_lines))
    print(
        f"[fetch_deals] queued {outfile} "
        f"(discount={discount:.0%}, affiliate_ready={affiliate_ready})"
    )
    return True


def main():
    if not has_required_env():
        print("[fetch_deals] Missing PA-API env vars; skipping")
        return

    if AmazonApi is Any:
        print("[fetch_deals] python-amazon-paapi is not installed; skipping")
        return

    seeds = load_seeds()
    if not seeds:
        return

    marketplace, asins, tags_map = seeds
    quality_policy = load_quality_policy()
    country = host_to_country(marketplace)
    api = client(country)

    exact_asins, exact_queries = load_exact_item_requests()
    searched_asins = search_asins_for_queries(api, exact_queries) if exact_queries else []
    prioritized_exact_asins = dedupe_keep_order(exact_asins + searched_asins)

    if prioritized_exact_asins:
        print(
            f"[fetch_deals] prioritized {len(prioritized_exact_asins)} exact-item ASIN(s) "
            f"({len(exact_asins)} direct, {len(searched_asins)} from search)"
        )
    else:
        print("[fetch_deals] no exact-item ASINs found for prioritization")

    asins = dedupe_keep_order(prioritized_exact_asins + asins)
    prioritized_exact_set = set(prioritized_exact_asins)

    written = 0
    for batch in batches(asins, size=10):
        try:
            products = api.get_items(batch)
        except AmazonException as exc:
            print(f"[fetch_deals] batch {batch} failed: {exc}")
            continue

        if not products:
            print(f"[fetch_deals] no items returned for {batch}")
            continue

        prioritized = sorted(
            products,
            key=lambda p: (
                0 if str(getattr(p, "asin", "") or "").strip().upper() in prioritized_exact_set else 1,
                0 if is_affiliate_ready(getattr(p, "url", "") or "") else 1,
            ),
        )

        for product in prioritized:
            asin = product.asin
            raw_offers: Dict = (product.raw or {}).get("Offers") or {}
            listings = raw_offers.get("Listings") or []
            brand = extract_brand(product)

            if not is_reputable_brand(brand, quality_policy):
                print(f"[fetch_deals] skip {asin}: non-reputable or blocked brand '{brand or '-'}'")
                continue

            price = None
            list_price = None
            if listings:
                listing = listings[0]
                if is_trusted_fulfillment(listing, quality_policy):
                    pass
                else:
                    print(f"[fetch_deals] skip {asin}: seller/fulfillment not trusted")
                    continue
                price_info = listing.get("Price") or {}
                savings = price_info.get("Savings") or {}
                amount = price_info.get("Amount")
                basis = price_info.get("SavingsBasis")
                if amount is not None:
                    price = amount
                if basis is not None:
                    list_price = basis
                elif amount is not None and savings.get("Amount") is not None:
                    list_price = amount + savings["Amount"]

            payload = {
                "title": product.title or asin,
                "url": product.url or "",
                "image": (product.images.large if product.images else ""),
                "price": price,
                "list_price": list_price,
                "summary": (product.features[0] if product.features else ""),
            }
            if write_deal(asin, payload, tags_map.get(asin, [])):
                written += 1

    print(f"[fetch_deals] done: queued {written} candidate(s) in {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
