"""Validate (and optionally refresh) listing prices/discounts for live deals.

Usage:
  python scripts/validate_discount_freshness.py
  python scripts/validate_discount_freshness.py --apply

Behavior:
  - Scans content/deals/*.md (excluding _index.md)
  - Fetches each deal's Amazon listing URL
  - Extracts live sale/list prices and discount
  - Compares against listing_* values in front matter
  - Prints a stale report
  - Optional --apply writes updated listing_* price fields
"""
from __future__ import annotations

import argparse
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
DEALS_DIR = ROOT / "content" / "deals"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def split_front_matter(raw: str):
    if not raw.startswith("+++\n"):
        return None
    end = raw.find("\n+++\n", 4)
    if end == -1:
        return None
    front = raw[4:end]
    body = raw[end + 5 :]
    return front, body


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


def upsert_line(front: str, key: str, value: Any) -> str:
    rendered = f"{key} = {value_to_toml(value)}"
    pattern = re.compile(rf"^{re.escape(key)}\s*=.*$", re.MULTILINE)
    if pattern.search(front):
        return pattern.sub(rendered, front, count=1)
    front = front.rstrip("\n")
    return f"{front}\n{rendered}\n"


def get_front_value(front: str, key: str) -> Optional[str]:
    quoted = re.search(rf'^{re.escape(key)}\s*=\s*"([^"]+)"\s*$', front, re.MULTILINE)
    if quoted:
        return quoted.group(1).strip()
    numeric = re.search(rf"^{re.escape(key)}\s*=\s*([0-9]+(?:\.[0-9]+)?)\s*$", front, re.MULTILINE)
    if numeric:
        return numeric.group(1)
    return None


def canonicalize_amazon_url(url: str) -> str:
    parsed = urlparse(url)
    if "amazon." not in parsed.netloc:
        return url
    asin_match = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})(?:[/?]|$)", url)
    if not asin_match:
        return url
    asin = asin_match.group(1)
    return f"{parsed.scheme or 'https'}://{parsed.netloc}/dp/{asin}"


def fetch_html(url: str) -> Optional[str]:
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "en-IE,en-US;q=0.9,en;q=0.8"})
    try:
        with urlopen(req, timeout=20) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            return resp.read().decode(charset, errors="replace")
    except (HTTPError, URLError, TimeoutError) as exc:
        print(f"[validate_discount_freshness] fetch failed for {url}: {exc}")
        return None


def parse_money(raw: str) -> Optional[float]:
    m = re.search(r"([0-9]+(?:[.,][0-9]{2})?)", raw)
    if not m:
        return None
    return float(m.group(1).replace(",", "."))


def extract_prices(html_doc: str):
    sale = None
    list_price = None

    sale_patterns = [
        r'"priceToPay"\s*:\s*\{[^}]*"price"\s*:\s*([0-9]+(?:\.[0-9]+)?)',
        r'"price"\s*:\s*"?EUR\s*([0-9]+(?:[.,][0-9]+)?)"?',
        r'"displayPrice"\s*:\s*"([^"]+)"',
    ]
    for pattern in sale_patterns:
        m = re.search(pattern, html_doc)
        if m:
            sale = parse_money(m.group(1))
            if sale is not None:
                break

    list_patterns = [
        r'"basisPrice"\s*:\s*\{[^}]*"price"\s*:\s*([0-9]+(?:\.[0-9]+)?)',
        r'"listPrice"\s*:\s*\{[^}]*"amount"\s*:\s*([0-9]+(?:\.[0-9]+)?)',
        r'"priceWas"\s*:\s*"([^"]+)"',
    ]
    for pattern in list_patterns:
        m = re.search(pattern, html_doc)
        if m:
            list_price = parse_money(m.group(1))
            if list_price is not None:
                break

    discount = None
    if sale is not None and list_price and list_price > 0:
        discount = max(0.0, min(1.0, 1 - (sale / list_price)))
    return sale, list_price, discount


def to_float(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def changed(a: Optional[float], b: Optional[float], tol: float) -> bool:
    if a is None or b is None:
        return a != b
    return abs(a - b) > tol


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Write updated listing price fields when stale.")
    parser.add_argument("--tolerance", type=float, default=0.01, help="Price delta tolerance before flagging stale.")
    args = parser.parse_args()

    now_iso = datetime.now(timezone.utc).isoformat()
    checked = 0
    stale = 0
    updated = 0

    for path in sorted(DEALS_DIR.glob("*.md")):
        if path.name == "_index.md":
            continue

        raw = path.read_text()
        split = split_front_matter(raw)
        if not split:
            continue
        front, body = split

        source_url = (
            get_front_value(front, "listing_url")
            or get_front_value(front, "product_url")
            or get_front_value(front, "affiliate_url")
        )
        if not source_url:
            continue

        checked += 1
        listing_url = canonicalize_amazon_url(source_url)
        html_doc = fetch_html(listing_url)
        if not html_doc:
            continue

        live_sale, live_list, live_discount = extract_prices(html_doc)
        current_sale = to_float(get_front_value(front, "listing_sale_price"))
        current_list = to_float(get_front_value(front, "listing_list_price"))
        current_discount = to_float(get_front_value(front, "listing_discount_pct"))

        sale_changed = changed(current_sale, live_sale, args.tolerance)
        list_changed = changed(current_list, live_list, args.tolerance)
        discount_changed = changed(current_discount, live_discount, 0.001)
        is_stale = sale_changed or list_changed or discount_changed

        if not is_stale:
            continue

        stale += 1
        print(f"[stale] {path.relative_to(ROOT)}")
        print(
            f"  sale: {current_sale if current_sale is not None else '-'} -> "
            f"{live_sale if live_sale is not None else '-'}"
        )
        print(
            f"  list: {current_list if current_list is not None else '-'} -> "
            f"{live_list if live_list is not None else '-'}"
        )
        print(
            f"  discount: {current_discount if current_discount is not None else '-'} -> "
            f"{live_discount if live_discount is not None else '-'}"
        )

        if args.apply:
            next_front = front
            if live_sale is not None:
                next_front = upsert_line(next_front, "listing_sale_price", live_sale)
            if live_list is not None:
                next_front = upsert_line(next_front, "listing_list_price", live_list)
            if live_discount is not None:
                next_front = upsert_line(next_front, "listing_discount_pct", live_discount)
            next_front = upsert_line(next_front, "listing_synced_at", now_iso)

            if next_front != front:
                path.write_text(f"+++\n{next_front.rstrip()}\n+++\n{body}")
                updated += 1

    print(
        f"[validate_discount_freshness] checked={checked} stale={stale} "
        f"updated={updated if args.apply else 0} apply={args.apply}"
    )


if __name__ == "__main__":
    main()
