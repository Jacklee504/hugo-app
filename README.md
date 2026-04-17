# Deal Ledger (Hugo)

Static Hugo deals site with:
- automated Amazon PA-API intake
- manual approval workflow before publish
- GitHub Actions deploy to GitHub Pages

## Current workflow

### 1) Fetch candidate deals (not live)
`scripts/fetch_deals.py` pulls seeded ASINs and writes candidates to:
- `review-queue/deals/*.md`

These files are outside Hugo `content/`, so they cannot be published by mistake.

### 2) Optional local review page
To preview candidates in-browser:
1. Mirror queue into draft preview pages:
   ```bash
   python scripts/sync_review_preview.py
   ```
2. Run Hugo with drafts:
   ```bash
   hugo server -D
   ```
3. Open `/deals-review/`

### 3) Approve and publish
Promote approved candidates into live deals:
```bash
python scripts/promote_deals.py --asin B0XXXXXXX
# or
python scripts/promote_deals.py --all
```

Promotion moves files into `content/deals/` and sets:
- `draft = false`
- `review_status = "approved"`

### 4) Sync live listing fields
Update listing-backed metadata for existing live deals:
```bash
python scripts/sync_listing_from_urls.py
```

This upserts `listing_*` fields (title, summary, image, URL, prices, discount, sync time) by reading each deal's retailer URL.

### 5) Validate discount freshness
Check whether stored `listing_*` prices/discounts still match live listing pages:
```bash
python scripts/validate_discount_freshness.py
```

Auto-apply stale listing price/discount updates:
```bash
python scripts/validate_discount_freshness.py --apply
```

### 5b) Review tag relevance (title/category/url based)
Suggest tags that match each item based on product signals (without using description text):
```bash
python scripts/review_tags.py
```

Apply suggested tags:
```bash
python scripts/review_tags.py --apply
```

### 6) Parse Discord alert submissions
If Formspree is connected to a Discord webhook channel, parse incoming messages into a clean queue:
```bash
DISCORD_BOT_TOKEN=xxx DISCORD_CHANNEL_ID=123 python scripts/parse_discord_alerts.py --output review-queue/alerts.json
```

CSV output:
```bash
DISCORD_BOT_TOKEN=xxx DISCORD_CHANNEL_ID=123 python scripts/parse_discord_alerts.py --format csv --output review-queue/alerts.csv
```

Incremental mode (only fetch messages after the last processed message ID):
```bash
DISCORD_BOT_TOKEN=xxx DISCORD_CHANNEL_ID=123 python scripts/parse_discord_alerts.py --incremental --output review-queue/alerts.json
```

### 7) Exact-item instant email alerts
Send email alerts when deals matching `exact_items` requests are discounted:
```bash
SMTP_HOST=smtp.example.com SMTP_PORT=587 SMTP_USERNAME=user SMTP_PASSWORD=pass SMTP_FROM=alerts@dealledger.eu python scripts/send_exact_item_alerts.py
```

Dry run:
```bash
python scripts/send_exact_item_alerts.py --dry-run
```

State files used:
- `.state/exact-item-subscriptions.json` (request registry)
- `.state/exact-item-alert-state.json` (dedupe / last sent discount)

Automation:
- `.github/workflows/exact-item-alerts.yml` runs every 30 minutes.
- It parses new Discord submissions, sends exact-item alert emails, and persists `.state` updates on the `state` branch (not `main`).
- `.github/workflows/sample-exact-item-email.yml` is a manual test workflow to send a branded sample email to any recipient.
- `.github/workflows/sample-signup-option-email.yml` is a manual test workflow for category, keyword, and weekly-digest sample emails.

## Setup

### Required repo secrets
Set in `Settings -> Secrets and variables -> Actions`:
- `AMZ_PAAPI_ACCESS_KEY`
- `AMZ_PAAPI_SECRET_KEY`
- `AMZ_PARTNER_TAG`
- `AMZ_MARKETPLACE` (example: `www.amazon.co.uk`)
- `DISCORD_BOT_TOKEN`
- `DISCORD_CHANNEL_ID`
- `SMTP_HOST`
- `SMTP_PORT`
- `SMTP_USERNAME`
- `SMTP_PASSWORD`
- `SMTP_FROM`

### Seed ASINs
Edit:
- `scripts/seeds.json`
- `scripts/quality_policy.json`

Replace `EDIT_ME_*` entries with real ASINs.

Quality policy controls automated intake filters:
- allow reputable brands only (`allowed_brands`)
- block low-trust brand terms (`blocked_brand_terms`)
- require fulfilled-by-Amazon or trusted seller terms

## Local run
```bash
cd /Users/jacklee/Other/Me/Projects/mysite
python3 -m venv .venv
source .venv/bin/activate
pip install -r scripts/requirements.txt
AMZ_PAAPI_ACCESS_KEY=xxx AMZ_PAAPI_SECRET_KEY=yyy AMZ_PARTNER_TAG=zzz AMZ_MARKETPLACE=www.amazon.co.uk python scripts/fetch_deals.py
python scripts/sync_review_preview.py
hugo server -D
```

## Search and alerts behavior
- Deals search uses local fuzzy matching (Fuse.js), synonym expansion, and fallback recommendations.
- Alerts form supports hidden inferred categories on submit:
  - `inferred_categories`
  - `effective_categories`
  - `exact_items`
  This is backend-facing only (no visible suggestion UI).
- Deal cards/single pages prefer `listing_*` fields when present, then fall back to manual front matter values.

## Deploy
- `.github/workflows/hugo.yml` builds and deploys to `gh-pages`.
- GitHub Pages should serve from `gh-pages` root.

## Future additions (agreed)
- Add a `Country` field to the alerts signup form so requests are region-aware.
- Use selected country + inferred categories to shape alert sends (not exact-tag only matching).
- Start with Ireland-first operations, then add additional Amazon Associate programs by country in phases.
- Add country-aware retailer URL routing (same product intent, different locale/store links).
- Keep affiliate-first publishing as the long-term direction while still allowing selected placeholders when needed.
- Add queue hygiene automation (example: auto-expire or archive unapproved deal candidates after X days).
- Add URL-driven search entry points from home/category chips and keep refining fuzzy relevance.
- Keep current deal-detail subpage templates available and re-enable item subpages later if deeper product pages are needed again.
- Continue publishing evergreen, manually written posts for SEO and internal linking.
- Diversify affiliate stack beyond Amazon over time (for example Awin/Partnerize/CJ), while keeping Amazon as primary initially.
- Revisit repo privacy/deploy architecture later (private source repo + public build-output repo) if code visibility becomes a higher priority.
