"""Send sample emails for non-exact signup options.

Usage:
  python scripts/send_sample_signup_alerts.py --to you@example.com --type category --query audio
  python scripts/send_sample_signup_alerts.py --to you@example.com --type keyword --query keyboard
  python scripts/send_sample_signup_alerts.py --to you@example.com --type weekly_digest

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
from urllib.parse import urlparse

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
        deals.append(
            Deal(
                slug=slug,
                title=get_str(front, "title") or get_str(front, "listing_title"),
                summary=get_str(front, "summary") or get_str(front, "listing_summary"),
                product_url=get_str(front, "product_url"),
                listing_url=get_str(front, "listing_url"),
                listing_image=get_str(front, "listing_image"),
                discount_pct=(get_float(front, "listing_discount_pct") or get_float(front, "discount_pct") or 0.0),
                sale_price=(get_float(front, "listing_sale_price") or get_float(front, "sale_price")),
                list_price=(get_float(front, "listing_list_price") or get_float(front, "list_price")),
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


def build_subject(sample_type: str, query: str) -> str:
    if sample_type == "category":
        return f"Deal Ledger sample: category alerts ({query or 'category'})"
    if sample_type == "keyword":
        return f"Deal Ledger sample: keyword alerts ({query or 'keyword'})"
    if sample_type == "weekly_digest":
        return "Deal Ledger sample: weekly digest"
    return "Deal Ledger sample alert"


def build_text(sample_type: str, query: str, deals: list[Deal], site_base: str) -> str:
    lines = [f"Deal Ledger sample email ({sample_type.replace('_', ' ')})", ""]
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
    lines.extend(["Thanks for using Deal Ledger.", "The Deal Ledger Team"])
    return "\n".join(lines)


def build_html(sample_type: str, query: str, deals: list[Deal], site_base: str) -> str:
    logo_url = f"{site_base.rstrip('/')}/images/brand/deal-ledger-logo.svg"
    cards = []
    for d in deals:
        pct = int(round(d.discount_pct * 100))
        sale = f"€{d.sale_price:.2f}" if isinstance(d.sale_price, (int, float)) else "-"
        list_price = f"€{d.list_price:.2f}" if isinstance(d.list_price, (int, float)) else "-"
        retailer_url = d.product_url or d.listing_url or f"{site_base.rstrip('/')}/deals/{d.slug}/"
        cta_label = retailer_cta_label(retailer_url)
        img = d.listing_image.strip()
        img_html = (
            f'<img src="{img}" alt="{d.title}" width="200" height="140" style="display:block;width:200px;height:140px;object-fit:cover;border-radius:10px;border:1px solid #e8ede8;background:#ffffff;">'
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
                      <h3 style="margin:0 0 8px;font-size:16px;line-height:1.35;color:#17332e;">{d.title}</h3>
                      <p style="margin:0 0 10px;font-size:14px;color:#17332e;"><strong>{sale}</strong> <span style="color:#6e7d75;">(was {list_price}, -{pct}%)</span></p>
                      <p style="margin:0 0 8px;font-size:13px;"><a href="{retailer_url}" style="display:inline-block;background:#17332e;color:#fffdf9;text-decoration:none;padding:8px 12px;border-radius:999px;font-weight:700;">{cta_label}</a></p>
                      <p style="margin:0;font-size:12px;color:#6e7d75;">Retailer: {compact_url(retailer_url)}</p>
                    </td>
                  </tr>
                </table>
              </td>
            </tr>
            """
        )

    filter_line = f"<p style='margin:0 0 12px;font-size:14px;color:#4d5f57;'>Filter: {query}</p>" if query else ""
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
                <h2 style="margin:0 0 8px;font-size:20px;color:#17332e;">Sample alert: {sample_type.replace('_', ' ')}</h2>
                {filter_line}
                <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
                  {''.join(cards)}
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


def send_email(to_email: str, subject: str, text_body: str, html_body: str) -> None:
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
    parser = argparse.ArgumentParser(description="Send sample signup-option email.")
    parser.add_argument("--to", required=True, help="Recipient email")
    parser.add_argument("--type", required=True, choices=["category", "keyword", "weekly_digest"])
    parser.add_argument("--query", default="", help="Category/keyword query for category/keyword sample types")
    args = parser.parse_args()

    site_base = (os.getenv("SITE_BASE_URL") or "https://dealledger.eu").strip()
    deals = load_deals()
    selected = pick_deals(deals, args.type, args.query)
    if not selected:
        raise SystemExit("No deals available for sample.")

    subject = build_subject(args.type, args.query.strip())
    text_body = build_text(args.type, args.query.strip(), selected, site_base)
    html_body = build_html(args.type, args.query.strip(), selected, site_base)
    send_email(args.to.strip(), subject, text_body, html_body)
    print(f"[sent] sample {args.type} email -> {args.to.strip()} ({len(selected)} card(s))")


if __name__ == "__main__":
    main()
