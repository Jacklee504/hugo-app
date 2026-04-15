"""Send email alerts for exact-item requests when discounted deals are found.

Usage:
  python scripts/send_exact_item_alerts.py
  python scripts/send_exact_item_alerts.py --requests review-queue/alerts.json

Required env for live email:
  SMTP_HOST
  SMTP_PORT (default: 587)
  SMTP_USERNAME
  SMTP_PASSWORD
  SMTP_FROM

Optional:
  SITE_BASE_URL (default: https://dealledger.eu)
  SMTP_USE_TLS (default: true)

Behavior:
  - Reads parsed Discord submissions (review-queue/alerts.json)
  - Persists exact-item subscriptions in .state/exact-item-subscriptions.json
  - Matches exact items against current deals in content/deals/*.md
  - Sends email only when a qualifying discount is present and not previously sent
  - Persists dedupe state in .state/exact-item-alert-state.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import smtplib
from dataclasses import dataclass
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
DEALS_DIR = ROOT / "content" / "deals"
DEFAULT_REQUESTS = ROOT / "review-queue" / "alerts.json"
SUBS_PATH = ROOT / ".state" / "exact-item-subscriptions.json"
STATE_PATH = ROOT / ".state" / "exact-item-alert-state.json"


@dataclass
class Deal:
    slug: str
    title: str
    listing_title: str
    summary: str
    product_url: str
    listing_url: str
    discount_pct: float
    sale_price: float | None
    list_price: float | None
    tags: list[str]


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def split_front_matter(raw: str):
    if not raw.startswith("+++\n"):
        return None
    end = raw.find("\n+++\n", 4)
    if end == -1:
        return None
    return raw[4:end], raw[end + 5 :]


def get_str(front: str, key: str) -> str:
    m = re.search(rf'^{re.escape(key)}\s*=\s*"([^"]*)"\s*$', front, re.MULTILINE)
    return m.group(1).strip() if m else ""


def get_float(front: str, key: str) -> float | None:
    m = re.search(rf"^{re.escape(key)}\s*=\s*([0-9]+(?:\.[0-9]+)?)\s*$", front, re.MULTILINE)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def get_array(front: str, key: str) -> list[str]:
    m = re.search(rf"^{re.escape(key)}\s*=\s*\[([^\]]*)\]\s*$", front, re.MULTILINE)
    if not m:
        return []
    return [s.strip().strip('"').strip("'") for s in m.group(1).split(",") if s.strip()]


def normalize(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", " ", value.lower())).strip()


def extract_asin(value: str) -> str | None:
    m = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})(?:[/?]|$)", value, re.IGNORECASE)
    return m.group(1) if m else None


def parse_exact_items(raw: str) -> list[str]:
    if not raw:
        return []
    parts = re.split(r"[\n,;]+", raw)
    clean = []
    seen = set()
    for part in parts:
        item = part.strip()
        if not item:
            continue
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        clean.append(item)
    return clean


def load_deals() -> list[Deal]:
    deals: list[Deal] = []
    for path in sorted(DEALS_DIR.glob("*.md")):
        if path.name == "_index.md":
            continue
        raw = path.read_text(encoding="utf-8")
        split = split_front_matter(raw)
        if not split:
            continue
        front, _ = split
        slug = path.stem
        title = get_str(front, "title")
        listing_title = get_str(front, "listing_title")
        summary = get_str(front, "summary") or get_str(front, "listing_summary")
        product_url = get_str(front, "product_url")
        listing_url = get_str(front, "listing_url")
        discount_pct = (
            get_float(front, "listing_discount_pct")
            or get_float(front, "discount_pct")
            or 0.0
        )
        sale_price = get_float(front, "listing_sale_price") or get_float(front, "sale_price")
        list_price = get_float(front, "listing_list_price") or get_float(front, "list_price")
        tags = get_array(front, "tags")
        deals.append(
            Deal(
                slug=slug,
                title=title,
                listing_title=listing_title,
                summary=summary,
                product_url=product_url,
                listing_url=listing_url,
                discount_pct=discount_pct,
                sale_price=sale_price,
                list_price=list_price,
                tags=tags,
            )
        )
    return deals


def deal_matches_exact_item(deal: Deal, item: str) -> bool:
    asin = extract_asin(item)
    if asin:
        hay = (deal.product_url + " " + deal.listing_url).upper()
        return asin.upper() in hay

    n_item = normalize(item)
    if len(n_item) < 3:
        return False

    haystack = normalize(
        f"{deal.title} {deal.listing_title} {deal.summary} {' '.join(deal.tags)} {deal.product_url} {deal.listing_url}"
    )
    return n_item in haystack or haystack in n_item


def update_subscriptions(requests_payload: dict[str, Any], subs: dict[str, Any]) -> dict[str, Any]:
    now_iso = datetime.now(timezone.utc).isoformat()
    records = requests_payload.get("records", [])
    for rec in records:
        email = str(rec.get("email", "")).strip().lower()
        if not email or "@" not in email:
            continue
        exact_raw = str(rec.get("exact_items", "")).strip()
        exact_items = parse_exact_items(exact_raw)
        if not exact_items:
            continue

        slot = subs.setdefault(
            email,
            {
                "email": email,
                "cadence": "",
                "categories": "",
                "keywords": "",
                "exact_items": [],
                "updated_at": now_iso,
            },
        )
        slot["cadence"] = str(rec.get("cadence", slot.get("cadence", "")))
        slot["categories"] = str(rec.get("effective_categories", rec.get("categories", slot.get("categories", ""))))
        slot["keywords"] = str(rec.get("keywords", slot.get("keywords", "")))

        merged = list(slot.get("exact_items", []))
        merged_set = {x.lower() for x in merged}
        for item in exact_items:
            if item.lower() not in merged_set:
                merged.append(item)
                merged_set.add(item.lower())
        slot["exact_items"] = merged
        slot["updated_at"] = now_iso
    return subs


def build_email_body(email: str, matches: list[dict[str, Any]], site_base: str) -> str:
    lines = [
        "Your exact-item deal alert from Deal Ledger",
        "",
        f"We found {len(matches)} discounted match(es) for your tracked items:",
        "",
    ]
    for m in matches:
        pct = int(round(float(m["discount_pct"]) * 100))
        sale = m.get("sale_price")
        list_price = m.get("list_price")
        sale_txt = f"€{sale:.2f}" if isinstance(sale, (int, float)) else "-"
        list_txt = f"€{list_price:.2f}" if isinstance(list_price, (int, float)) else "-"
        lines.extend(
            [
                f"- Request: {m['requested_item']}",
                f"  Item: {m['title']}",
                f"  Price: {sale_txt} (was {list_txt}, -{pct}%)",
                f"  Link: {m['url']}",
                "",
            ]
        )
    lines.extend(
        [
            f"Browse more deals: {site_base.rstrip('/')}/deals/",
            "",
            "You received this because you requested exact item tracking on Deal Ledger.",
        ]
    )
    return "\n".join(lines)


def send_email(to_email: str, subject: str, body: str) -> None:
    host = os.getenv("SMTP_HOST", "").strip()
    user = os.getenv("SMTP_USERNAME", "").strip()
    password = os.getenv("SMTP_PASSWORD", "").strip()
    sender = os.getenv("SMTP_FROM", "").strip()
    port = int(os.getenv("SMTP_PORT", "587"))
    use_tls = os.getenv("SMTP_USE_TLS", "true").strip().lower() != "false"

    if not (host and user and password and sender):
        raise RuntimeError("SMTP not configured. Set SMTP_HOST/SMTP_USERNAME/SMTP_PASSWORD/SMTP_FROM.")

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to_email

    with smtplib.SMTP(host, port, timeout=25) as smtp:
        smtp.ehlo()
        if use_tls:
            smtp.starttls()
            smtp.ehlo()
        smtp.login(user, password)
        smtp.sendmail(sender, [to_email], msg.as_string())


def main() -> None:
    parser = argparse.ArgumentParser(description="Send exact-item discount alerts by email.")
    parser.add_argument("--requests", default=str(DEFAULT_REQUESTS), help="Parsed alerts JSON path.")
    parser.add_argument("--dry-run", action="store_true", help="Print would-send emails without sending.")
    args = parser.parse_args()

    site_base = os.getenv("SITE_BASE_URL", "https://dealledger.eu").strip()
    requests_path = Path(args.requests)

    requests_payload = read_json(requests_path, {"records": []})
    print(f"[send_exact_item_alerts] loaded_request_records={len(requests_payload.get('records', []))}")
    subscriptions = read_json(SUBS_PATH, {})
    notify_state = read_json(STATE_PATH, {"last_sent": {}})
    notify_map = notify_state.setdefault("last_sent", {})

    subscriptions = update_subscriptions(requests_payload, subscriptions)
    deals = load_deals()

    queued_by_email: dict[str, list[dict[str, Any]]] = {}
    now_iso = datetime.now(timezone.utc).isoformat()

    for email, sub in subscriptions.items():
        exact_items = parse_exact_items(", ".join(sub.get("exact_items", [])))
        if not exact_items:
            continue

        for requested_item in exact_items:
            for deal in deals:
                if deal.discount_pct <= 0:
                    continue
                if not deal_matches_exact_item(deal, requested_item):
                    continue

                dedupe_key = f"{email}|{normalize(requested_item)}|{deal.slug}"
                last_sent_discount = notify_map.get(dedupe_key, {}).get("discount_pct")
                last_sent_discount = float(last_sent_discount) if last_sent_discount is not None else None

                # Send when a discount appears first time, or discount deepens.
                if last_sent_discount is not None and deal.discount_pct <= last_sent_discount + 0.001:
                    continue

                url = deal.product_url or deal.listing_url or f"{site_base.rstrip('/')}/deals/{deal.slug}/"
                queued_by_email.setdefault(email, []).append(
                    {
                        "requested_item": requested_item,
                        "title": deal.title or deal.listing_title,
                        "discount_pct": deal.discount_pct,
                        "sale_price": deal.sale_price,
                        "list_price": deal.list_price,
                        "url": url,
                        "deal_slug": deal.slug,
                        "dedupe_key": dedupe_key,
                    }
                )

    sent_count = 0
    for email, matches in queued_by_email.items():
        subject = f"Deal Ledger: {len(matches)} exact-item discount match{'es' if len(matches) != 1 else ''}"
        body = build_email_body(email=email, matches=matches, site_base=site_base)

        if args.dry_run:
            print(f"[dry-run] would email {email} with {len(matches)} match(es)")
        else:
            send_email(email, subject, body)
            print(f"[sent] {email} ({len(matches)} match(es))")

        sent_count += 1
        for m in matches:
            notify_map[m["dedupe_key"]] = {
                "discount_pct": m["discount_pct"],
                "sent_at": now_iso,
                "deal_slug": m["deal_slug"],
                "email": email,
                "requested_item": m["requested_item"],
            }

    write_json(SUBS_PATH, subscriptions)
    write_json(STATE_PATH, notify_state)

    print(
        f"[send_exact_item_alerts] subscriptions={len(subscriptions)} "
        f"emails_queued={len(queued_by_email)} emails_processed={sent_count}"
    )


if __name__ == "__main__":
    main()
