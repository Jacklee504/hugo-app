"""Sync listing fields by reading each deal's retailer URL.

Usage:
  python scripts/sync_listing_from_urls.py

Behavior:
  - Scans content/deals/*.md (excluding _index.md)
  - Reads product_url/affiliate_url/listing_url from front matter
  - Fetches page HTML and extracts visible metadata where possible
  - Upserts listing_* fields used by templates

Notes:
  - Works best with product URLs (not broad search-result URLs).
  - If a page blocks scraping, existing fields are kept unchanged.
"""
from __future__ import annotations

import html
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
    match = re.search(rf'^{re.escape(key)}\s*=\s*"([^"]+)"\s*$', front, re.MULTILINE)
    return match.group(1).strip() if match else None


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
      print(f"[sync_listing_from_urls] fetch failed for {url}: {exc}")
      return None


def extract_meta(html_doc: str, name: str) -> Optional[str]:
    patterns = [
        rf'<meta[^>]+property=["\']{re.escape(name)}["\'][^>]+content=["\']([^"\']+)["\']',
        rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']{re.escape(name)}["\']',
        rf'<meta[^>]+name=["\']{re.escape(name)}["\'][^>]+content=["\']([^"\']+)["\']',
    ]
    for pattern in patterns:
        m = re.search(pattern, html_doc, re.IGNORECASE)
        if m:
            return html.unescape(m.group(1).strip())
    return None


def extract_title(html_doc: str) -> Optional[str]:
    og = extract_meta(html_doc, "og:title")
    if og:
        return og
    m = re.search(r"<title>(.*?)</title>", html_doc, re.IGNORECASE | re.DOTALL)
    if not m:
        return None
    return html.unescape(re.sub(r"\s+", " ", m.group(1)).strip())


def extract_image(html_doc: str) -> Optional[str]:
    patterns = [
        r'<img[^>]+id=["\']imgTagWrapperId["\'][^>]+data-old-hires=["\']([^"\']+)["\']',
        r'<img[^>]+id=["\']imgTagWrapperId["\'][^>]+src=["\']([^"\']+)["\']',
        r'data-old-hires=["\']([^"\']*m\.media-amazon\.com[^"\']+)["\']',
        r'["\'](https://m\.media-amazon\.com/images/I/[^"\']+\.(?:jpg|jpeg|png))["\']',
    ]
    for pattern in patterns:
        m = re.search(pattern, html_doc, re.IGNORECASE)
        if m:
            return html.unescape(m.group(1).strip())
    return None


def parse_money(raw: str) -> Optional[float]:
    m = re.search(r"([0-9]+(?:[.,][0-9]{2})?)", raw)
    if not m:
        return None
    return float(m.group(1).replace(",", "."))


def extract_prices(html_doc: str):
    sale = None
    list_price = None

    patterns = [
        r'"priceToPay"\s*:\s*\{[^}]*"price"\s*:\s*([0-9]+(?:\.[0-9]+)?)',
        r'"price"\s*:\s*"?EUR\s*([0-9]+(?:[.,][0-9]+)?)"?',
        r'"displayPrice"\s*:\s*"([^"]+)"',
    ]
    for pattern in patterns:
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


def clean_summary(value: Optional[str], title: Optional[str]) -> str:
    if value:
        txt = re.sub(r"\s+", " ", value).strip()
        if txt:
            return txt[:220]
    if title:
        return title
    return ""


def main() -> None:
    updated = 0
    now_iso = datetime.now(timezone.utc).isoformat()

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

        listing_url = canonicalize_amazon_url(source_url)
        doc = fetch_html(listing_url)
        if not doc:
            continue

        title = extract_title(doc)
        summary = extract_meta(doc, "description")
        image = extract_meta(doc, "og:image") or extract_image(doc)
        sale, list_price, discount = extract_prices(doc)

        next_front = front
        next_front = upsert_line(next_front, "listing_url", listing_url)
        if title:
            next_front = upsert_line(next_front, "listing_title", title)
        next_front = upsert_line(next_front, "listing_summary", clean_summary(summary, title))
        if image:
            next_front = upsert_line(next_front, "listing_image", image)
        if sale is not None:
            next_front = upsert_line(next_front, "listing_sale_price", sale)
        if list_price is not None:
            next_front = upsert_line(next_front, "listing_list_price", list_price)
        if discount is not None:
            next_front = upsert_line(next_front, "listing_discount_pct", discount)
        next_front = upsert_line(next_front, "listing_synced_at", now_iso)

        if next_front != front:
            path.write_text(f"+++\n{next_front.rstrip()}\n+++\n{body}")
            updated += 1
            print(f"[sync_listing_from_urls] updated {path.relative_to(ROOT)}")

    print(f"[sync_listing_from_urls] done: {updated} file(s) updated")


if __name__ == "__main__":
    main()
