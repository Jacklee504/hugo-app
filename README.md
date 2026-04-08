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

## Setup

### Required repo secrets
Set in `Settings -> Secrets and variables -> Actions`:
- `AMZ_PAAPI_ACCESS_KEY`
- `AMZ_PAAPI_SECRET_KEY`
- `AMZ_PARTNER_TAG`
- `AMZ_MARKETPLACE` (example: `www.amazon.co.uk`)

### Seed ASINs
Edit:
- `scripts/seeds.json`

Replace `EDIT_ME_*` entries with real ASINs.

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
