"""Send sample Deal Ledger emails locally without GitHub Actions.

Usage examples:
  python scripts/send_sample_email.py --to you@example.com --type exact
  python scripts/send_sample_email.py --to you@example.com --type category --query audio
  python scripts/send_sample_email.py --to you@example.com --type keyword --query headphones
  python scripts/send_sample_email.py --to you@example.com --type weekly_digest

Required env:
  SMTP_HOST, SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD, SMTP_FROM
Optional env:
  SMTP_USE_TLS (default true)
  SITE_BASE_URL (default https://dealledger.eu)
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXACT_SCRIPT = ROOT / "scripts" / "send_exact_item_alerts.py"
SIGNUP_SCRIPT = ROOT / "scripts" / "send_sample_signup_alerts.py"


def require_smtp_env() -> None:
    required = ("SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD", "SMTP_FROM")
    missing = [name for name in required if not (os.getenv(name) or "").strip()]
    if missing:
        raise SystemExit(
            "Missing SMTP env vars: "
            + ", ".join(missing)
            + "\nSet these first, then run again."
        )


def run_cmd(cmd: list[str]) -> None:
    print("[run]", " ".join(cmd))
    subprocess.run(cmd, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Send sample Deal Ledger emails locally.")
    parser.add_argument("--to", required=True, help="Recipient email address")
    parser.add_argument(
        "--type",
        required=True,
        choices=["exact", "category", "keyword", "weekly_digest"],
        help="Sample email type",
    )
    parser.add_argument(
        "--query",
        default="",
        help="Required for category/keyword sample type (example: audio, headphones)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Render preview without sending email.")
    parser.add_argument(
        "--preview-dir",
        default="review-queue",
        help="Directory for preview files when --dry-run is used.",
    )
    args = parser.parse_args()

    if not args.dry_run:
        require_smtp_env()

    if args.type in {"category", "keyword"} and not args.query.strip():
        raise SystemExit("--query is required when --type is category or keyword.")

    if args.type == "exact":
        cmd = [sys.executable, str(EXACT_SCRIPT), "--test-email-to", args.to.strip()]
        if args.dry_run:
            cmd.append("--dry-run")
        run_cmd(cmd)
        return

    cmd = [
        sys.executable,
        str(SIGNUP_SCRIPT),
        "--to",
        args.to.strip(),
        "--type",
        args.type,
    ]
    if args.type in {"category", "keyword"}:
        cmd.extend(["--query", args.query.strip()])
    if args.dry_run:
        cmd.extend(["--dry-run", "--preview-dir", args.preview_dir.strip()])
    run_cmd(cmd)


if __name__ == "__main__":
    main()
