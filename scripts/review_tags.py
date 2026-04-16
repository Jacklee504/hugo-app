"""Review and normalize deal tags from product signals (not descriptions).

Usage:
  python scripts/review_tags.py
  python scripts/review_tags.py --apply
  python scripts/review_tags.py --apply --include-review-queue

Behavior:
  - Scans live deals in content/deals/*.md (excluding _index.md)
  - Optionally scans review queue files in review-queue/deals/*.md
  - Suggests tags from title/listing_title/URLs/categories/brand terms
  - Never uses summary/description text as a tag source
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
LIVE_DEALS_DIR = ROOT / "content" / "deals"
REVIEW_QUEUE_DIR = ROOT / "review-queue" / "deals"

BRAND_TAGS = [
    "amazon",
    "anker",
    "apple",
    "bontec",
    "corsair",
    "elived",
    "lego",
    "logitech",
    "samsung",
    "sony",
    "soundcore",
    "ultimea",
]

TAG_RULES: list[tuple[list[str], str]] = [
    (["headphone", "headset", "earbud", "soundbar", "speaker", "noise cancelling", "anc"], "audio"),
    (["keyboard", "mouse", "monitor", "desk", "office", "workspace", "riser"], "home office"),
    (["charger", "charging", "magsafe", "power bank", "wireless charger", "qi"], "charging"),
    (["tv", "streaming", "fire tv", "stick", "wall bracket", "wall mount"], "home entertainment"),
    (["lego", "toy", "champions", "collectible"], "toys"),
    (["gaming", "ps5", "xbox", "pc", "rgb"], "gaming"),
    (["psu", "power supply", "atx", "pcie", "modular"], "pc components"),
    (["travel", "portable", "compact", "foldable"], "portable"),
]

CATEGORY_TAGS = {
    "electronics": "tech",
    "home": "home",
    "productivity": "productivity",
}

MAX_TAGS = 6


def split_front_matter(raw: str) -> tuple[str, str] | None:
    if not raw.startswith("+++\n"):
        return None
    end = raw.find("\n+++\n", 4)
    if end == -1:
        return None
    return raw[4:end], raw[end + 5 :]


def get_str(front: str, key: str) -> str:
    m = re.search(rf'^{re.escape(key)}\s*=\s*"([^"]*)"\s*$', front, re.MULTILINE)
    return m.group(1).strip() if m else ""


def get_array(front: str, key: str) -> list[str]:
    m = re.search(rf"^{re.escape(key)}\s*=\s*\[([^\]]*)\]\s*$", front, re.MULTILINE)
    if not m:
        return []
    return [s.strip().strip('"').strip("'") for s in m.group(1).split(",") if s.strip()]


def upsert_array(front: str, key: str, values: list[str]) -> str:
    rendered = f"{key} = {json.dumps(values, ensure_ascii=False)}"
    pattern = re.compile(rf"^{re.escape(key)}\s*=.*$", re.MULTILINE)
    if pattern.search(front):
        return pattern.sub(rendered, front, count=1)
    front = front.rstrip("\n")
    return f"{front}\n{rendered}\n"


def normalize(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", " ", value.lower())).strip()


def contains_term(text: str, term: str) -> bool:
    n_text = f" {normalize(text)} "
    n_term = normalize(term)
    if not n_term:
        return False
    if " " in n_term:
        return n_term in n_text
    return f" {n_term} " in n_text


def source_text(front: str) -> str:
    title = get_str(front, "title")
    listing_title = get_str(front, "listing_title")
    product_url = get_str(front, "product_url")
    listing_url = get_str(front, "listing_url")
    categories = " ".join(get_array(front, "categories"))
    # Intentionally exclude summary/listing_summary/description fields.
    return " ".join([title, listing_title, product_url, listing_url, categories]).strip()


def dedupe_keep_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        cleaned = value.strip().lower()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        ordered.append(cleaned)
    return ordered


def suggest_tags(front: str) -> list[str]:
    src = source_text(front)
    categories = [c.lower() for c in get_array(front, "categories")]
    suggested: list[str] = []

    for brand in BRAND_TAGS:
        if contains_term(src, brand):
            suggested.append(brand)

    for terms, tag in TAG_RULES:
        if any(contains_term(src, term) for term in terms):
            suggested.append(tag)

    for category in categories:
        mapped = CATEGORY_TAGS.get(category)
        if mapped:
            suggested.append(mapped)

    if not suggested:
        fallback = [c for c in categories if c]
        suggested.extend(fallback[:3])

    return dedupe_keep_order(suggested)[:MAX_TAGS]


def iter_deal_files(include_review_queue: bool) -> list[Path]:
    files = [p for p in sorted(LIVE_DEALS_DIR.glob("*.md")) if p.name != "_index.md"]
    if include_review_queue and REVIEW_QUEUE_DIR.exists():
        files.extend(sorted(REVIEW_QUEUE_DIR.glob("*.md")))
    return files


def process_file(path: Path, apply: bool) -> tuple[bool, str]:
    raw = path.read_text(encoding="utf-8")
    split = split_front_matter(raw)
    if not split:
        return False, "missing front matter"
    front, body = split

    existing = dedupe_keep_order(get_array(front, "tags"))
    suggested = suggest_tags(front)

    if existing == suggested:
        return False, "unchanged"

    if apply:
        updated_front = upsert_array(front, "tags", suggested)
        path.write_text(f"+++\n{updated_front.rstrip()}\n+++\n{body}", encoding="utf-8")

    return True, f"tags {existing} -> {suggested}"


def main() -> None:
    parser = argparse.ArgumentParser(description="Review and normalize deal tags from product signals.")
    parser.add_argument("--apply", action="store_true", help="Write suggested tags back to files.")
    parser.add_argument(
        "--include-review-queue",
        action="store_true",
        help="Also process review-queue/deals files.",
    )
    args = parser.parse_args()

    files = iter_deal_files(include_review_queue=args.include_review_queue)
    if not files:
        print("[review_tags] no deal files found")
        return

    changed = 0
    for path in files:
        did_change, detail = process_file(path, apply=args.apply)
        if did_change:
            changed += 1
            print(f"[review_tags] {path.relative_to(ROOT)}: {detail}")

    mode = "applied" if args.apply else "suggested"
    print(f"[review_tags] done: {mode} changes for {changed} file(s)")


if __name__ == "__main__":
    main()
