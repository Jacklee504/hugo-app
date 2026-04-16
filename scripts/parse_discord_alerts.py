"""Fetch and parse Formspree alert submissions posted to a Discord channel.

Usage:
  DISCORD_BOT_TOKEN=... DISCORD_CHANNEL_ID=... python scripts/parse_discord_alerts.py
  DISCORD_BOT_TOKEN=... DISCORD_CHANNEL_ID=... python scripts/parse_discord_alerts.py --format csv --output /tmp/alerts.csv
  DISCORD_BOT_TOKEN=... DISCORD_CHANNEL_ID=... python scripts/parse_discord_alerts.py --incremental

Notes:
  - Requires a Discord bot token with read access to the target channel.
  - Expects message content format from your Formspree -> Discord webhook.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE = ROOT / ".state" / "discord-alerts-last-id.txt"
DISCORD_API = "https://discord.com/api/v10"
ALERT_PREFIX = "New Deal Ledger alert request"


def ensure_state_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def canonical_key(key: str) -> str:
    k = key.strip().lower()
    # Strip common Discord markdown / quote markers.
    k = k.replace("**", "").replace("`", "")
    k = re.sub(r"^[>\-\s]+", "", k)
    k = re.sub(r"[^a-z0-9\s_]", "", k)
    k = re.sub(r"\s+", "_", k).strip("_")
    return k


def parse_kv_lines(content: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for line in content.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        ckey = canonical_key(key)
        if not ckey:
            continue
        parsed[ckey] = value.strip()
    return parsed


def parse_embed_fields(msg: dict[str, Any]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    embeds = msg.get("embeds") or []
    for embed in embeds:
        title = str(embed.get("title", "")).strip()
        if title and canonical_key(title) not in parsed:
            parsed[canonical_key(title)] = str(embed.get("description", "")).strip()
        for field in embed.get("fields") or []:
            name = canonical_key(str(field.get("name", "")))
            value = str(field.get("value", "")).strip()
            if name:
                parsed[name] = value
    return parsed


def parse_message(msg: dict[str, Any]) -> dict[str, Any] | None:
    content = str(msg.get("content", "")).strip()
    embeds = msg.get("embeds") or []
    has_alert_marker = ALERT_PREFIX.lower() in content.lower()
    if not has_alert_marker:
        for embed in embeds:
            title = str(embed.get("title", "")).lower()
            desc = str(embed.get("description", "")).lower()
            if ALERT_PREFIX.lower() in title or ALERT_PREFIX.lower() in desc:
                has_alert_marker = True
                break
    if not has_alert_marker:
        return None

    kv = parse_kv_lines(content)
    embed_kv = parse_embed_fields(msg)
    for k, v in embed_kv.items():
        if k not in kv and v:
            kv[k] = v

    return {
        "message_id": msg.get("id", ""),
        "created_at": msg.get("timestamp", ""),
        "author": (msg.get("author") or {}).get("username", ""),
        "name": kv.get("name", ""),
        "email": kv.get("email", ""),
        "country": kv.get("country", ""),
        "cadence": kv.get("cadence", ""),
        "categories": kv.get("categories", ""),
        "keywords": kv.get("keywords", ""),
        "exact_items": kv.get("exact_items", ""),
        "inferred_categories": kv.get("inferred_categories", ""),
        "effective_categories": kv.get("effective_categories", ""),
        "notes": kv.get("notes", ""),
        "raw_content": content,
    }


def fetch_channel_messages(
    token: str,
    channel_id: str,
    limit: int,
    before: str | None = None,
    after: str | None = None,
) -> list[dict[str, Any]]:
    params: dict[str, str] = {"limit": str(limit)}
    if before:
        params["before"] = before
    if after:
        params["after"] = after

    url = f"{DISCORD_API}/channels/{channel_id}/messages?{urlencode(params)}"
    req = Request(
        url,
        headers={
            "Authorization": f"Bot {token}",
            "User-Agent": "deal-ledger-discord-parser/1.0",
        },
    )
    try:
        with urlopen(req, timeout=25) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Discord API HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"Discord API network error: {exc}") from exc


def newest_message_id(records: list[dict[str, Any]]) -> str | None:
    ids = [str(r.get("message_id", "")).strip() for r in records if r.get("message_id")]
    ids = [mid for mid in ids if mid.isdigit()]
    if not ids:
        return None
    # Discord snowflakes are sortable as integers.
    return max(ids, key=lambda s: int(s))


def write_csv(records: list[dict[str, Any]], output_path: Path) -> None:
    fieldnames = [
        "message_id",
        "created_at",
        "author",
        "name",
        "email",
        "country",
        "cadence",
        "categories",
        "keywords",
        "exact_items",
        "inferred_categories",
        "effective_categories",
        "notes",
        "raw_content",
    ]
    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for rec in records:
            writer.writerow(rec)


def write_json(records: list[dict[str, Any]], output_path: Path) -> None:
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(records),
        "records": records,
    }
    output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def read_last_id(path: Path) -> str | None:
    if not path.exists():
        return None
    value = path.read_text(encoding="utf-8").strip()
    return value or None


def write_last_id(path: Path, message_id: str) -> None:
    ensure_state_dir(path)
    path.write_text(f"{message_id}\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Parse Formspree alerts from Discord channel messages.")
    parser.add_argument("--token", default=os.getenv("DISCORD_BOT_TOKEN", ""), help="Discord bot token (or set DISCORD_BOT_TOKEN).")
    parser.add_argument("--channel-id", default=os.getenv("DISCORD_CHANNEL_ID", ""), help="Discord channel ID (or set DISCORD_CHANNEL_ID).")
    parser.add_argument("--limit", type=int, default=100, help="Max messages to fetch (1-100).")
    parser.add_argument("--before", default="", help="Fetch messages before this message ID.")
    parser.add_argument("--after", default="", help="Fetch messages after this message ID.")
    parser.add_argument("--incremental", action="store_true", help="Use a saved last message ID as --after and update it after run.")
    parser.add_argument("--state-file", default=str(DEFAULT_STATE), help="Path for incremental last-message state.")
    parser.add_argument("--format", choices=["json", "csv"], default="json", help="Output format.")
    parser.add_argument("--output", default="", help="Output file path. If omitted, print JSON to stdout.")
    args = parser.parse_args()

    token = args.token.strip()
    channel_id = args.channel_id.strip()
    if not token:
        raise SystemExit("Missing token. Set --token or DISCORD_BOT_TOKEN.")
    if not channel_id:
        raise SystemExit("Missing channel ID. Set --channel-id or DISCORD_CHANNEL_ID.")

    if args.limit < 1 or args.limit > 100:
        raise SystemExit("--limit must be between 1 and 100.")

    state_path = Path(args.state_file)
    after_id = args.after.strip() or None
    if args.incremental and not after_id:
        after_id = read_last_id(state_path)

    messages = fetch_channel_messages(
        token=token,
        channel_id=channel_id,
        limit=args.limit,
        before=(args.before.strip() or None),
        after=after_id,
    )

    parsed = []
    for msg in messages:
        row = parse_message(msg)
        if row:
            parsed.append(row)

    # API returns newest-first; reverse for chronological processing.
    parsed.sort(key=lambda r: int(r["message_id"]))

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        if args.format == "csv":
            write_csv(parsed, out)
        else:
            write_json(parsed, out)
        print(f"[parse_discord_alerts] wrote {len(parsed)} record(s) to {out}")
    else:
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "count": len(parsed),
            "records": parsed,
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))

    if args.incremental:
        # Only advance checkpoint when we actually parsed alert submissions.
        # This avoids skipping valid records if the bot cannot read message content yet.
        latest = newest_message_id(parsed)
        if latest:
            write_last_id(state_path, latest)
            print(f"[parse_discord_alerts] updated state: {state_path} -> {latest}")
        else:
            print("[parse_discord_alerts] no parsed alert records; state not advanced.")


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        print(f"[parse_discord_alerts] {exc}", file=sys.stderr)
        raise SystemExit(1)
