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
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

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
    listing_image: str
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
        listing_image = get_str(front, "listing_image")
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
                listing_image=listing_image,
                discount_pct=discount_pct,
                sale_price=sale_price,
                list_price=list_price,
                tags=tags,
            )
        )
    return deals


def compact_request(value: str) -> str:
    value = (value or "").strip()
    asin = extract_asin(value)
    if asin:
        host = urlparse(value).netloc or "amazon"
        return f"{host}/dp/{asin}"
    if len(value) > 90:
        return value[:87] + "..."
    return value


def compact_url(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return "-"
    parsed = urlparse(value)
    asin = extract_asin(value)
    if asin:
        return f"{parsed.netloc}/dp/{asin}"
    return parsed.netloc + parsed.path if parsed.netloc else value


def retailer_cta_label(url: str) -> str:
    lower = (url or "").lower()
    if "amazon." in lower or "amzn.to" in lower:
        return "View on Amazon"
    return "View retailer"


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


def parse_notes_preferences(notes: str) -> dict[str, Any]:
    preferences: dict[str, Any] = {
        "max_price": None,
        "min_price": None,
        "min_discount_pct": None,
        "exclude_terms": [],
        "prefer_terms": [],
    }
    if not notes:
        return preferences

    tokens = [t.strip() for t in re.split(r"[\n,;]+", notes) if t.strip()]
    for token in tokens:
        raw = token.strip()
        lower = raw.lower()
        normalized = normalize(raw)
        if not normalized:
            continue

        max_price_match = re.search(r"(?:under|below|max(?:imum)?|less than)\s*€?\s*([0-9]+(?:\.[0-9]+)?)", lower)
        if max_price_match:
            preferences["max_price"] = float(max_price_match.group(1))
            continue

        min_price_match = re.search(r"(?:over|above|min(?:imum)?|more than)\s*€?\s*([0-9]+(?:\.[0-9]+)?)", lower)
        if min_price_match:
            preferences["min_price"] = float(min_price_match.group(1))
            continue

        min_discount_match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*%", lower)
        if min_discount_match and (
            "+" in lower
            or "at least" in lower
            or "minimum" in lower
            or lower.startswith("min ")
            or "or more" in lower
        ):
            preferences["min_discount_pct"] = float(min_discount_match.group(1)) / 100.0
            continue

        excluded = ""
        if lower.startswith("no "):
            excluded = lower[3:].strip()
        elif lower.startswith("not "):
            excluded = lower[4:].strip()
        elif lower.startswith("-"):
            excluded = lower[1:].strip()
        if excluded:
            normalized_excluded = normalize(excluded)
            if len(normalized_excluded) >= 2:
                preferences["exclude_terms"].append(normalized_excluded)
            continue

        if len(normalized) >= 3:
            preferences["prefer_terms"].append(normalized)

    preferences["exclude_terms"] = list(dict.fromkeys(preferences["exclude_terms"]))
    preferences["prefer_terms"] = list(dict.fromkeys(preferences["prefer_terms"]))
    return preferences


def evaluate_notes_match(deal: Deal, preferences: dict[str, Any]) -> tuple[bool, int, list[str]]:
    sale_price = deal.sale_price if isinstance(deal.sale_price, (int, float)) else None
    discount_pct = float(deal.discount_pct or 0.0)
    max_price = preferences.get("max_price")
    min_price = preferences.get("min_price")
    min_discount_pct = preferences.get("min_discount_pct")
    exclude_terms = preferences.get("exclude_terms") or []
    prefer_terms = preferences.get("prefer_terms") or []

    if max_price is not None and sale_price is not None and sale_price > float(max_price):
        return False, 0, []
    if min_price is not None and sale_price is not None and sale_price < float(min_price):
        return False, 0, []
    if min_discount_pct is not None and discount_pct < float(min_discount_pct):
        return False, 0, []

    haystack = normalize(f"{deal.title} {deal.listing_title} {' '.join(deal.tags)}")
    padded_haystack = f" {haystack} "

    for term in exclude_terms:
        if not term:
            continue
        if f" {term} " in padded_haystack or term in haystack:
            return False, 0, []

    preferred_hits: list[str] = []
    for term in prefer_terms:
        if not term:
            continue
        if f" {term} " in padded_haystack or term in haystack:
            preferred_hits.append(term)

    return True, len(preferred_hits), preferred_hits[:3]


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
                "name": "",
                "email": email,
                "country": "",
                "cadence": "",
                "categories": "",
                "keywords": "",
                "notes": "",
                "exact_items": [],
                "updated_at": now_iso,
            },
        )
        slot["name"] = str(rec.get("name", slot.get("name", ""))).strip()
        slot["country"] = str(rec.get("country", slot.get("country", ""))).strip()
        slot["cadence"] = str(rec.get("cadence", slot.get("cadence", "")))
        slot["categories"] = str(rec.get("effective_categories", rec.get("categories", slot.get("categories", ""))))
        slot["keywords"] = str(rec.get("keywords", slot.get("keywords", "")))
        slot["notes"] = str(rec.get("notes", slot.get("notes", ""))).strip()

        merged = list(slot.get("exact_items", []))
        merged_set = {x.lower() for x in merged}
        for item in exact_items:
            if item.lower() not in merged_set:
                merged.append(item)
                merged_set.add(item.lower())
        slot["exact_items"] = merged
        slot["updated_at"] = now_iso
    return subs


def build_alert_links() -> tuple[str, str]:
    sender_email = (os.getenv("SMTP_FROM") or "contact@dealledger.eu").strip()
    sender_email = sender_email if "@" in sender_email else "contact@dealledger.eu"
    unsubscribe = os.getenv("ALERT_UNSUBSCRIBE_URL", "").strip()
    feedback = os.getenv("ALERT_FEEDBACK_URL", "").strip()

    if not unsubscribe:
        unsubscribe = (
            f"mailto:{sender_email}"
            f"?subject={quote('Unsubscribe from Deal Ledger alerts')}"
        )
    if not feedback:
        feedback = (
            f"mailto:{sender_email}"
            f"?subject={quote('Deal match feedback')}"
        )
    return unsubscribe, feedback


def pick_collection_query(matches: list[dict[str, Any]]) -> str:
    # Prefer preference signal (e.g. "headphones"), then the requested item text.
    for m in matches:
        hits = m.get("preference_hits") or []
        if isinstance(hits, list):
            for hit in hits:
                cleaned = normalize(str(hit))
                if cleaned:
                    return cleaned

    for m in matches:
        requested = normalize(str(m.get("requested_item", "")))
        if requested:
            return " ".join(requested.split()[:3])

    title = normalize(str(matches[0].get("title", ""))) if matches else ""
    if title:
        return " ".join(title.split()[:3])
    return "deals"


def build_collection_url(site_base: str, matches: list[dict[str, Any]]) -> str:
    query = pick_collection_query(matches)
    return f"{site_base.rstrip('/')}/deals/?q={quote(query)}"


def build_email_body(
    email: str,
    matches: list[dict[str, Any]],
    site_base: str,
    unsubscribe_url: str,
    feedback_url: str,
) -> str:
    collection_url = build_collection_url(site_base, matches)
    lines = [
        "Deal Ledger: Exact-item discount alert",
        "",
        f"We found {len(matches)} discounted match(es) for your tracked items:",
        "",
        f"See all matching deals: {collection_url}",
        "",
    ]
    for m in matches:
        pct = int(round(float(m["discount_pct"]) * 100))
        sale = m.get("sale_price")
        list_price = m.get("list_price")
        sale_txt = f"€{sale:.2f}" if isinstance(sale, (int, float)) else "-"
        list_txt = f"€{list_price:.2f}" if isinstance(list_price, (int, float)) else "-"
        lines.append(f"- Item: {m['title']}")
        lines.append(f"  Price: {sale_txt} (was {list_txt}, -{pct}%)")
        if m.get("preference_hits"):
            lines.append("  Preference match: " + ", ".join(m.get("preference_hits", [])))
        lines.append(f"  Retailer: {m['retailer_url']}")
        lines.append("")
    lines.extend(
        [
            "Thanks for using Deal Ledger.",
            "The Deal Ledger Team",
            "You received this because you requested exact item tracking.",
            f"Unsubscribe: {unsubscribe_url}",
        ]
    )
    return "\n".join(lines)


def build_email_html(
    matches: list[dict[str, Any]],
    site_base: str,
    unsubscribe_url: str,
    feedback_url: str,
) -> str:
    logo_url = f"{site_base.rstrip('/')}/images/brand/deal-ledger-logo.svg"
    collection_url = build_collection_url(site_base, matches)
    cards: list[str] = []
    for m in matches:
        pct = int(round(float(m["discount_pct"]) * 100))
        sale = m.get("sale_price")
        list_price = m.get("list_price")
        sale_txt = f"€{sale:.2f}" if isinstance(sale, (int, float)) else "-"
        list_txt = f"€{list_price:.2f}" if isinstance(list_price, (int, float)) else "-"
        retailer_url = str(m.get("retailer_url", "")).strip()
        cta_label = retailer_cta_label(retailer_url)
        img = (m.get("image_url") or "").strip()
        img_html = (
            f'<img src="{img}" alt="{m["title"]}" width="200" height="140" style="display:block;width:200px;height:140px;object-fit:cover;border-radius:10px;border:1px solid #e8ede8;background:#ffffff;">'
            if img
            else '<div style="width:200px;height:140px;border-radius:10px;border:1px solid #e8ede8;background:#f5f8f6;"></div>'
        )
        cards.append(
            f"""
            <tr>
              <td style="padding:14px 0;border-top:1px solid #edf1ed;">
                <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #e3ebe6;border-radius:12px;overflow:hidden;background:#ffffff;">
                  <tr>
                    <td style="width:220px;vertical-align:top;padding:10px;background:#f7faf8;">{img_html}</td>
                    <td style="vertical-align:top;padding:12px 14px;">
                      <h3 style="margin:0 0 8px;font-size:16px;line-height:1.35;color:#17332e;">{m['title']}</h3>
                      <p style="margin:0 0 10px;font-size:14px;color:#17332e;"><strong>{sale_txt}</strong> <span style="color:#6e7d75;">(was {list_txt}, -{pct}%)</span></p>
                      {
                        f'<p style="margin:0 0 8px;font-size:12px;color:#5d6f66;">Preference match: {", ".join(m.get("preference_hits", []))}</p>'
                        if m.get("preference_hits")
                        else ""
                      }
                      <p style="margin:0 0 8px;font-size:13px;"><a href="{retailer_url}" style="display:inline-block;background:#17332e;color:#fffdf9;text-decoration:none;padding:8px 12px;border-radius:999px;font-weight:700;">{cta_label}</a></p>
                      <p style="margin:0;font-size:12px;color:#6e7d75;">Retailer: {compact_url(retailer_url)}</p>
                    </td>
                  </tr>
                </table>
              </td>
            </tr>
            """
        )

    return f"""<!doctype html>
<html>
  <body style="margin:0;padding:0;background:#f6f8f6;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#17332e;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f6f8f6;padding:20px 10px;">
      <tr>
        <td align="center">
          <table role="presentation" width="640" cellpadding="0" cellspacing="0" style="width:100%;max-width:640px;background:#ffffff;border:1px solid #e8ede8;border-radius:14px;overflow:hidden;">
            <tr>
              <td style="padding:18px 20px;background:#17332e;">
                <img src="{logo_url}" alt="Deal Ledger" width="220" style="display:block;width:220px;max-width:100%;height:auto;">
              </td>
            </tr>
            <tr>
              <td style="padding:18px 20px;">
                <h2 style="margin:0 0 8px;font-size:20px;color:#17332e;">Exact-item discount alert</h2>
                <p style="margin:0 0 12px;font-size:14px;color:#4d5f57;">We found {len(matches)} discounted match(es) for your tracked items.</p>
                <p style="margin:0 0 12px;font-size:13px;"><a href="{collection_url}" style="display:inline-block;background:#edf4f1;color:#17332e;text-decoration:none;padding:7px 11px;border-radius:999px;border:1px solid #d9e4de;">See all matching deals</a></p>
                <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
                  {''.join(cards)}
                </table>
                <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:14px;border-top:1px solid #edf1ed;">
                  <tr>
                    <td style="padding-top:12px;font-size:12px;line-height:1.5;color:#5d6f66;">
                      <a href="{unsubscribe_url}" style="color:#0d4e46;text-decoration:none;">Unsubscribe</a>
                    </td>
                  </tr>
                </table>
                <p style="margin:12px 0 0;font-size:13px;color:#5d6f66;">Thanks for using Deal Ledger.<br>The Deal Ledger Team</p>
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
  </body>
</html>"""


def send_email(to_email: str, subject: str, body: str, html_body: str, unsubscribe_url: str = "") -> None:
    host = os.getenv("SMTP_HOST", "").strip()
    user = os.getenv("SMTP_USERNAME", "").strip()
    password = os.getenv("SMTP_PASSWORD", "").strip()
    sender = os.getenv("SMTP_FROM", "").strip()
    port = int(os.getenv("SMTP_PORT", "587"))
    use_tls = os.getenv("SMTP_USE_TLS", "true").strip().lower() != "false"

    if not (host and user and password and sender):
        raise RuntimeError("SMTP not configured. Set SMTP_HOST/SMTP_USERNAME/SMTP_PASSWORD/SMTP_FROM.")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to_email
    if unsubscribe_url:
        msg["List-Unsubscribe"] = f"<{unsubscribe_url}>"
    msg.attach(MIMEText(body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

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
    parser.add_argument("--test-email-to", default="", help="Send a single sample branded email to this address.")
    args = parser.parse_args()

    site_base = os.getenv("SITE_BASE_URL", "https://dealledger.eu").strip()
    unsubscribe_url, feedback_url = build_alert_links()
    requests_path = Path(args.requests)
    deals = load_deals()

    if args.test_email_to.strip():
        sample_deal = next((d for d in deals if d.discount_pct > 0), None)
        if not sample_deal:
            raise SystemExit("No discounted deals found to build sample email.")
        sample_match = {
            "requested_item": sample_deal.product_url or sample_deal.listing_url or sample_deal.title,
            "title": sample_deal.title or sample_deal.listing_title,
            "discount_pct": sample_deal.discount_pct,
            "sale_price": sample_deal.sale_price,
            "list_price": sample_deal.list_price,
            "retailer_url": sample_deal.product_url or sample_deal.listing_url,
            "deal_page_url": f"{site_base.rstrip('/')}/deals/{sample_deal.slug}/",
            "image_url": sample_deal.listing_image,
            "deal_slug": sample_deal.slug,
            "dedupe_key": "sample",
        }
        subject = "Deal Ledger: sample exact-item discount alert"
        body = build_email_body(
            email=args.test_email_to,
            matches=[sample_match],
            site_base=site_base,
            unsubscribe_url=unsubscribe_url,
            feedback_url=feedback_url,
        )
        html_body = build_email_html(
            matches=[sample_match],
            site_base=site_base,
            unsubscribe_url=unsubscribe_url,
            feedback_url=feedback_url,
        )
        if args.dry_run:
            print(f"[dry-run] sample email prepared for {args.test_email_to}")
        else:
            send_email(args.test_email_to.strip(), subject, body, html_body, unsubscribe_url=unsubscribe_url)
            print(f"[sent] sample email -> {args.test_email_to.strip()}")
        return

    requests_payload = read_json(requests_path, {"records": []})
    print(f"[send_exact_item_alerts] loaded_request_records={len(requests_payload.get('records', []))}")
    subscriptions = read_json(SUBS_PATH, {})
    notify_state = read_json(STATE_PATH, {"last_sent": {}})
    notify_map = notify_state.setdefault("last_sent", {})

    subscriptions = update_subscriptions(requests_payload, subscriptions)

    queued_by_email: dict[str, list[dict[str, Any]]] = {}
    now_iso = datetime.now(timezone.utc).isoformat()

    for email, sub in subscriptions.items():
        exact_items = parse_exact_items(", ".join(sub.get("exact_items", [])))
        if not exact_items:
            continue
        notes_preferences = parse_notes_preferences(str(sub.get("notes", "")))

        for requested_item in exact_items:
            for deal in deals:
                if deal.discount_pct <= 0:
                    continue
                if not deal_matches_exact_item(deal, requested_item):
                    continue
                notes_ok, note_score, preference_hits = evaluate_notes_match(deal, notes_preferences)
                if not notes_ok:
                    continue

                dedupe_key = f"{email}|{normalize(requested_item)}|{deal.slug}"
                last_sent_discount = notify_map.get(dedupe_key, {}).get("discount_pct")
                last_sent_discount = float(last_sent_discount) if last_sent_discount is not None else None

                # Send when a discount appears first time, or discount deepens.
                if last_sent_discount is not None and deal.discount_pct <= last_sent_discount + 0.001:
                    continue

                url = deal.product_url or deal.listing_url or f"{site_base.rstrip('/')}/deals/{deal.slug}/"
                deal_page_url = f"{site_base.rstrip('/')}/deals/{deal.slug}/"
                queued_by_email.setdefault(email, []).append(
                    {
                        "requested_item": requested_item,
                        "title": deal.title or deal.listing_title,
                        "discount_pct": deal.discount_pct,
                        "sale_price": deal.sale_price,
                        "list_price": deal.list_price,
                        "retailer_url": url,
                        "deal_page_url": deal_page_url,
                        "image_url": deal.listing_image,
                        "deal_slug": deal.slug,
                        "dedupe_key": dedupe_key,
                        "note_score": note_score,
                        "preference_hits": preference_hits,
                    }
                )

    sent_count = 0
    for email, matches in queued_by_email.items():
        matches.sort(key=lambda m: (int(m.get("note_score", 0)), float(m.get("discount_pct", 0.0))), reverse=True)
        subject = f"Deal Ledger: {len(matches)} exact-item discount match{'es' if len(matches) != 1 else ''}"
        body = build_email_body(
            email=email,
            matches=matches,
            site_base=site_base,
            unsubscribe_url=unsubscribe_url,
            feedback_url=feedback_url,
        )
        html_body = build_email_html(
            matches=matches,
            site_base=site_base,
            unsubscribe_url=unsubscribe_url,
            feedback_url=feedback_url,
        )

        if args.dry_run:
            print(f"[dry-run] would email {email} with {len(matches)} match(es)")
        else:
            send_email(email, subject, body, html_body, unsubscribe_url=unsubscribe_url)
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
