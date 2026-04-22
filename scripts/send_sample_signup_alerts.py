"""Send production-style preview emails for non-exact signup options.

Usage:
  python scripts/send_sample_signup_alerts.py --to you@example.com --type category --query audio
  python scripts/send_sample_signup_alerts.py --to you@example.com --type keyword --query keyboard
  python scripts/send_sample_signup_alerts.py --to you@example.com --type weekly_digest
  python scripts/send_sample_signup_alerts.py --to you@example.com --type category --query audio --dry-run --preview-dir review-queue

Required env:
  SMTP_HOST, SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD, SMTP_FROM
Optional env:
  SITE_BASE_URL (default: https://dealledger.eu)
"""
from __future__ import annotations

import argparse
import os
import re
import smtplib
from dataclasses import dataclass
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

ROOT = Path(__file__).resolve().parents[1]
DEALS_DIR = ROOT / "content" / "deals"


@dataclass
class Deal:
    slug: str
    title: str
    summary: str
    product_url: str
    listing_url: str
    listing_image: str
    discount_pct: float
    sale_price: float | None
    list_price: float | None
    tags: list[str]
    categories: list[str]


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
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", " ", (value or "").lower())).strip()


def resolve_deal_prices(front: str, discount_pct: float) -> tuple[float | None, float | None]:
    listing_sale = get_float(front, "listing_sale_price")
    listing_list = get_float(front, "listing_list_price")
    base_sale = get_float(front, "sale_price")
    base_list = get_float(front, "list_price")

    sale_price = listing_sale if listing_sale is not None else base_sale
    list_price = listing_list if listing_list is not None else base_list

    # Coupon-backed listings often keep listing_sale_price == listing_list_price.
    # Prefer the effective sale when available, otherwise derive it from discount.
    if (
        isinstance(sale_price, (int, float))
        and isinstance(list_price, (int, float))
        and list_price > 0
        and abs(sale_price - list_price) < 0.01
        and discount_pct > 0
    ):
        if isinstance(base_sale, (int, float)) and base_sale > 0 and base_sale < list_price:
            sale_price = base_sale
        else:
            derived_sale = round(list_price * (1.0 - float(discount_pct)), 2)
            if derived_sale > 0 and derived_sale < list_price:
                sale_price = derived_sale

    return sale_price, list_price


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
        discount_pct = get_float(front, "listing_discount_pct")
        if discount_pct is None:
            discount_pct = get_float(front, "discount_pct")
        if discount_pct is None:
            discount_pct = 0.0
        sale_price, list_price = resolve_deal_prices(front, discount_pct)
        deals.append(
            Deal(
                slug=slug,
                title=get_str(front, "title") or get_str(front, "listing_title"),
                summary=get_str(front, "summary") or get_str(front, "listing_summary"),
                product_url=get_str(front, "product_url"),
                listing_url=get_str(front, "listing_url"),
                listing_image=get_str(front, "listing_image"),
                discount_pct=discount_pct,
                sale_price=sale_price,
                list_price=list_price,
                tags=get_array(front, "tags"),
                categories=get_array(front, "categories"),
            )
        )
    return deals


def compact_url(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return "-"
    parsed = urlparse(value)
    return parsed.netloc + parsed.path if parsed.netloc else value


def retailer_cta_label(url: str) -> str:
    lower = (url or "").lower()
    if "amazon." in lower or "amzn.to" in lower:
        return "View on Amazon"
    return "View retailer"


def retailer_display_name(url: str) -> str:
    lower = (url or "").lower()
    if "amazon." in lower or "amzn.to" in lower:
        return "Amazon"
    parsed = urlparse(url or "")
    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host or "Retailer"


def build_discover_url(
    site_base: str,
    categories: list[str],
    tags: list[str],
    title: str,
    default_query: str = "",
) -> str:
    query = normalize(default_query)
    if not query:
        if categories:
            query = normalize(categories[0])
    if not query:
        if tags:
            query = normalize(tags[0])
    if not query:
        query = " ".join(normalize(title).split()[:2]) or "deals"
    return f"{site_base.rstrip('/')}/deals/?q={quote(query)}"


def build_unsubscribe_page_url(site_base: str, email: str) -> str:
    email_q = quote((email or "").strip())
    return f"{site_base.rstrip('/')}/alerts/unsubscribe/?email={email_q}&type=general"


def pick_deals(deals: list[Deal], sample_type: str, query: str) -> list[Deal]:
    q = normalize(query)
    if sample_type == "weekly_digest":
        return sorted(deals, key=lambda d: d.discount_pct, reverse=True)[:4]

    # category / keyword
    matched = []
    for d in deals:
        hay = normalize(f"{d.title} {d.summary} {' '.join(d.tags)} {' '.join(d.categories)}")
        if q and q in hay:
            matched.append(d)
    if matched:
        return sorted(matched, key=lambda d: d.discount_pct, reverse=True)[:4]
    return sorted(deals, key=lambda d: d.discount_pct, reverse=True)[:4]


def build_subject(sample_type: str, query: str, deal_count: int) -> str:
    clean_query = query.strip() or ("category" if sample_type == "category" else "keyword")
    if sample_type == "category":
        return f"Deal Ledger: {deal_count} new deals in {clean_query}"
    if sample_type == "keyword":
        return f"Deal Ledger: {deal_count} new matches for {clean_query}"
    if sample_type == "weekly_digest":
        return f"Deal Ledger: weekly digest ({deal_count} picks)"
    return "Deal Ledger: deal alerts"


def build_text(sample_type: str, query: str, deals: list[Deal], site_base: str, unsubscribe_url: str) -> str:
    lines = ["Deal Ledger alert", ""]
    if sample_type == "weekly_digest":
        lines.append(f"Here are this week's top {len(deals)} picks:")
    else:
        lines.append(f"We found {len(deals)} deal match(es) for your alert.")
    lines.append("")
    if query:
        lines.append(f"Filter: {query}")
        lines.append("")
    for d in deals:
        pct = int(round(d.discount_pct * 100))
        sale = f"€{d.sale_price:.2f}" if isinstance(d.sale_price, (int, float)) else "-"
        list_price = f"€{d.list_price:.2f}" if isinstance(d.list_price, (int, float)) else "-"
        retailer_url = d.product_url or d.listing_url or f"{site_base.rstrip('/')}/deals/{d.slug}/"
        lines.extend(
            [
                f"- {d.title}",
                f"  Price: {sale} (was {list_price}, -{pct}%)",
                f"  Retailer: {retailer_url}",
                "",
            ]
        )
    lines.extend(
        [
            "Thanks for using Deal Ledger.",
            "The Deal Ledger Team",
            f"Unsubscribe: {unsubscribe_url}",
        ]
    )
    return "\n".join(lines)


def build_html(sample_type: str, query: str, deals: list[Deal], site_base: str, unsubscribe_url: str) -> str:
    logo_url = f"{site_base.rstrip('/')}/images/brand/deal-ledger-logo.svg"
    cards = []
    for d in deals:
        pct = int(round(d.discount_pct * 100))
        sale = f"€{d.sale_price:.2f}" if isinstance(d.sale_price, (int, float)) else "-"
        list_price = f"€{d.list_price:.2f}" if isinstance(d.list_price, (int, float)) else "-"
        retailer_url = d.product_url or d.listing_url or f"{site_base.rstrip('/')}/deals/{d.slug}/"
        cta_label = retailer_cta_label(retailer_url)
        discover_url = build_discover_url(site_base, d.categories, d.tags, d.title, query)
        img = d.listing_image.strip()
        img_html = (
            f'<img class="dl-media-img" src="{img}" alt="{d.title}" width="280" height="220" style="display:block;width:280px;height:220px;object-fit:contain;border-radius:10px;border:1px solid #e8ede8;background:#ffffff;">'
            if img
            else '<div class="dl-media-ph" style="width:280px;height:220px;border-radius:10px;border:1px solid #e8ede8;background:#f5f8f6;"></div>'
        )
        cards.append(
            f"""
            <tr>
              <td style="padding:14px 0;border-top:1px solid #edf1ed;">
                <table class="dl-card" role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #e3ebe6;border-radius:12px;overflow:hidden;background:#ffffff;">
                  <tr>
                    <td class="dl-media" style="width:290px;vertical-align:top;padding:10px 8px 10px 10px;line-height:0;">{img_html}</td>
                    <td class="dl-content" style="vertical-align:top;padding:12px 14px;">
                      <h3 style="margin:0 0 8px;font-size:16px;line-height:1.35;color:#17332e;">{d.title}</h3>
                      <p style="margin:0 0 10px;font-size:14px;color:#17332e;"><strong>{sale}</strong> <span style="color:#6e7d75;">(was {list_price}, -{pct}%)</span></p>
                    </td>
                  </tr>
                  <tr>
                    <td colspan="2" style="padding:0 10px 12px 10px;">
                      <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
                        <tr>
                          <td class="dl-action-col" width="50%" style="width:50%;padding-right:6px;vertical-align:top;">
                            <a class="dl-btn" href="{retailer_url}" style="display:block;text-align:center;background:#17332e;color:#fffdf9;text-decoration:none;padding:10px 12px;border-radius:999px;border:1px solid #17332e;font-weight:700;font-size:13px;line-height:1.2;box-sizing:border-box;width:100%;">{cta_label}</a>
                          </td>
                          <td class="dl-action-col" width="50%" style="width:50%;padding-left:6px;vertical-align:top;">
                            <a class="dl-btn" href="{discover_url}" style="display:block;text-align:center;background:#edf4f1;color:#17332e;text-decoration:none;padding:10px 12px;border-radius:999px;border:1px solid #d9e4de;font-weight:700;font-size:13px;line-height:1.2;box-sizing:border-box;width:100%;">View category on Deal Ledger</a>
                          </td>
                        </tr>
                      </table>
                    </td>
                  </tr>
                </table>
              </td>
            </tr>
            """
        )

    if sample_type == "weekly_digest":
        heading = "Weekly digest"
        intro = f"Here are this week's top {len(deals)} picks."
    elif sample_type == "category":
        heading = "Category deal alert"
        intro = f"We found {len(deals)} deal match(es) for your category alert."
    else:
        heading = "Keyword deal alert"
        intro = f"We found {len(deals)} deal match(es) for your keyword alert."

    filter_line = f"<p style='margin:0 0 12px;font-size:14px;color:#4d5f57;'>Filter: {query}</p>" if query else ""
    return f"""<!doctype html>
<html>
  <head>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
      @media only screen and (max-width: 620px) {{
        .dl-wrap {{ padding: 12px 6px !important; }}
        .dl-shell {{ width: 100% !important; max-width: 100% !important; border-radius: 0 !important; }}
        .dl-card td {{ display: block !important; width: 100% !important; box-sizing: border-box !important; }}
        .dl-media {{ padding: 10px 10px 0 10px !important; }}
        .dl-content {{ padding: 10px 12px !important; }}
        .dl-media-img {{ width: 100% !important; height: auto !important; max-width: none !important; max-height: 260px !important; }}
        .dl-media-ph {{ width: 100% !important; height: 220px !important; }}
        .dl-action-col {{ display: block !important; width: 100% !important; padding: 0 0 8px 0 !important; }}
        .dl-btn {{ width: 100% !important; display: block !important; }}
      }}
    </style>
  </head>
  <body style="margin:0;padding:0;background:#f6f8f6;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#17332e;">
    <table class="dl-wrap" role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f6f8f6;padding:20px 10px;">
      <tr>
        <td align="center">
          <table class="dl-shell" role="presentation" width="640" cellpadding="0" cellspacing="0" style="width:100%;max-width:640px;background:#ffffff;border:1px solid #e8ede8;border-radius:14px;overflow:hidden;">
            <tr>
              <td style="padding:18px 20px;background:#17332e;">
                <img src="{logo_url}" alt="Deal Ledger" width="220" style="display:block;width:220px;max-width:100%;height:auto;">
              </td>
            </tr>
            <tr>
              <td style="padding:18px 20px;">
                <h2 style="margin:0 0 8px;font-size:20px;color:#17332e;">{heading}</h2>
                <p style="margin:0 0 12px;font-size:14px;color:#4d5f57;">{intro}</p>
                {filter_line}
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


def send_email(to_email: str, subject: str, text_body: str, html_body: str, unsubscribe_url: str = "") -> None:
    host = (os.getenv("SMTP_HOST") or "").strip()
    user = (os.getenv("SMTP_USERNAME") or "").strip()
    password = (os.getenv("SMTP_PASSWORD") or "").strip()
    sender = (os.getenv("SMTP_FROM") or "").strip()
    port = int((os.getenv("SMTP_PORT") or "587").strip())
    use_tls = (os.getenv("SMTP_USE_TLS") or "true").strip().lower() != "false"
    if not (host and user and password and sender):
        raise RuntimeError("SMTP not configured. Set SMTP_HOST/SMTP_USERNAME/SMTP_PASSWORD/SMTP_FROM.")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to_email
    if unsubscribe_url:
        msg["List-Unsubscribe"] = f"<{unsubscribe_url}>"
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP(host, port, timeout=25) as smtp:
        smtp.ehlo()
        if use_tls:
            smtp.starttls()
            smtp.ehlo()
        smtp.login(user, password)
        smtp.sendmail(sender, [to_email], msg.as_string())


def main() -> None:
    parser = argparse.ArgumentParser(description="Send production-style signup-option email preview.")
    parser.add_argument("--to", required=True, help="Recipient email")
    parser.add_argument("--type", required=True, choices=["category", "keyword", "weekly_digest"])
    parser.add_argument("--query", default="", help="Category/keyword query for category/keyword sample types")
    parser.add_argument("--dry-run", action="store_true", help="Render output without sending email.")
    parser.add_argument(
        "--preview-dir",
        default="",
        help="Directory to write rendered preview files when --dry-run is set.",
    )
    args = parser.parse_args()

    site_base = (os.getenv("SITE_BASE_URL") or "https://dealledger.eu").strip()
    deals = load_deals()
    selected = pick_deals(deals, args.type, args.query)
    if not selected:
        raise SystemExit("No deals available for sample.")

    unsubscribe_url = build_unsubscribe_page_url(site_base, args.to.strip())
    subject = build_subject(args.type, args.query.strip(), len(selected))
    text_body = build_text(args.type, args.query.strip(), selected, site_base, unsubscribe_url)
    html_body = build_html(args.type, args.query.strip(), selected, site_base, unsubscribe_url)
    if args.dry_run:
        print(f"[dry-run] rendered {args.type} email for {args.to.strip()} ({len(selected)} card(s))")
        print(f"[dry-run] subject: {subject}")
        for deal in selected:
            print(f"[dry-run] card: {deal.title}")

        if args.preview_dir.strip():
            preview_dir = Path(args.preview_dir.strip())
            preview_dir.mkdir(parents=True, exist_ok=True)
            slug = args.type if args.type == "weekly_digest" else f"{args.type}-{normalize(args.query.strip() or 'default').replace(' ', '-')}"
            text_path = preview_dir / f"email-preview-{slug}.txt"
            html_path = preview_dir / f"email-preview-{slug}.html"
            text_path.write_text(text_body + "\n", encoding="utf-8")
            html_path.write_text(html_body + "\n", encoding="utf-8")
            print(f"[dry-run] wrote text preview: {text_path}")
            print(f"[dry-run] wrote html preview: {html_path}")
        return

    send_email(args.to.strip(), subject, text_body, html_body, unsubscribe_url=unsubscribe_url)
    print(f"[sent] {args.type} email -> {args.to.strip()} ({len(selected)} card(s))")


if __name__ == "__main__":
    main()
