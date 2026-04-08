"""Promote approved deal drafts into live content/deals.

Usage:
  python scripts/promote_deals.py --all
  python scripts/promote_deals.py --asin B012345678

Behavior:
  - Moves files from review-queue/deals/*.md to content/deals/*.md.
  - Switches `draft = true` to `draft = false` and sets `review_status = "approved"`.
  - Leaves non-targeted files untouched.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = ROOT / "review-queue" / "deals"
TARGET_DIR = ROOT / "content" / "deals"


def patch_frontmatter(text: str) -> str:
    if "+++" not in text:
        return text

    if re.search(r"^draft\s*=\s*true\s*$", text, flags=re.MULTILINE):
        text = re.sub(r"^draft\s*=\s*true\s*$", "draft = false", text, count=1, flags=re.MULTILINE)
    elif not re.search(r"^draft\s*=", text, flags=re.MULTILINE):
        text = text.replace("+++\n", "+++\ndraft = false\n", 1)

    if re.search(r'^review_status\s*=\s*"[^"]*"\s*$', text, flags=re.MULTILINE):
        text = re.sub(
            r'^review_status\s*=\s*"[^"]*"\s*$',
            'review_status = "approved"',
            text,
            count=1,
            flags=re.MULTILINE,
        )
    else:
        text = text.replace("+++\n", "+++\nreview_status = \"approved\"\n", 1)

    return text


def promote(path: Path) -> bool:
    if not path.exists():
        return False

    TARGET_DIR.mkdir(parents=True, exist_ok=True)
    destination = TARGET_DIR / path.name
    if destination.exists():
        stem = path.stem
        suffix = 2
        while (TARGET_DIR / f"{stem}-{suffix}.md").exists():
            suffix += 1
        destination = TARGET_DIR / f"{stem}-{suffix}.md"

    raw = path.read_text()
    patched = patch_frontmatter(raw)
    destination.write_text(patched)
    path.unlink()
    print(f"[promote_deals] promoted {path.name} -> {destination.relative_to(ROOT)}")
    return True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--asin", action="append", default=[], help="ASIN/stem to promote")
    parser.add_argument("--all", action="store_true", help="Promote all queued draft deals")
    args = parser.parse_args()

    if not SOURCE_DIR.exists():
        print("[promote_deals] no review queue found")
        return

    promoted = 0
    if args.all:
        for candidate in sorted(SOURCE_DIR.glob("*.md")):
            if promote(candidate):
                promoted += 1
    else:
        if not args.asin:
            print("[promote_deals] provide --all or one/more --asin values")
            return
        for asin in args.asin:
            candidate = SOURCE_DIR / f"{asin}.md"
            if promote(candidate):
                promoted += 1
            else:
                print(f"[promote_deals] not found: {candidate.relative_to(ROOT)}")

    print(f"[promote_deals] done: promoted {promoted} file(s)")


if __name__ == "__main__":
    main()
