"""Sync external review queue into local draft pages for Hugo preview.

Usage:
  python scripts/sync_review_preview.py

Behavior:
  - Reads candidates from review-queue/deals/*.md
  - Mirrors them to content/deals-review/generated/*.md for local `hugo -D` preview
  - Removes stale mirrored files that no longer exist in queue
  - Keeps mirrored files as drafts, so they never publish in production
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
QUEUE_DIR = ROOT / "review-queue" / "deals"
PREVIEW_DIR = ROOT / "content" / "deals-review" / "generated"


def ensure_draft(text: str) -> str:
    if "+++" not in text:
        return text
    if "\ndraft = " not in text:
        return text.replace("+++\n", "+++\ndraft = true\n", 1)
    return text.replace("draft = false", "draft = true")


def main() -> None:
    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)

    queue_files = sorted(QUEUE_DIR.glob("*.md")) if QUEUE_DIR.exists() else []
    mirrored_names = set()

    for src in queue_files:
        dst = PREVIEW_DIR / src.name
        dst.write_text(ensure_draft(src.read_text()))
        mirrored_names.add(src.name)
        print(f"[sync_review_preview] mirrored {src.name}")

    for stale in PREVIEW_DIR.glob("*.md"):
        if stale.name not in mirrored_names:
            stale.unlink()
            print(f"[sync_review_preview] removed stale {stale.name}")

    print(f"[sync_review_preview] done: {len(mirrored_names)} candidate(s) mirrored")


if __name__ == "__main__":
    main()
