# Hugo Deals Site

Static Hugo site that surfaces Amazon deals. Includes a scheduled GitHub Action to pull ASIN details via PA-API, write deal pages, build Hugo, and deploy to GitHub Pages.

## What’s ready
- Hugo layouts for featured, best discounts, new, expiring, tags, and deal detail.
- PA-API fetch scaffold (`scripts/fetch_deals.py`) that writes `content/deals/generated/*.md`.
- Workflow `.github/workflows/hugo.yml` with push/manual/cron triggers, Python + Hugo steps, and graceful no-op if secrets/seeds aren’t set.
- Required disclosure/contact/privacy pages are present.

## Quick start (after you have keys)
1) Add repo secrets (Settings → Secrets → Actions):
   - `AMZ_PAAPI_ACCESS_KEY`
   - `AMZ_PAAPI_SECRET_KEY`
   - `AMZ_PARTNER_TAG` (your affiliate/partner tag)
   - `AMZ_MARKETPLACE` (e.g., `www.amazon.co.uk` or `webservices.amazon.com`)
2) Configure `scripts/seeds.json` with real ASINs and optional tags; remove the `EDIT_ME_*` placeholders.
3) (Optional) Test locally:
   ```bash
   cd /Users/jacklee/mysite
   python -m venv .venv && source .venv/bin/activate
   pip install -r scripts/requirements.txt
   AMZ_PAAPI_ACCESS_KEY=xxx AMZ_PAAPI_SECRET_KEY=yyy AMZ_PARTNER_TAG=zzz AMZ_MARKETPLACE=www.amazon.co.uk python scripts/fetch_deals.py
   hugo
   ```
4) Push to `main` or run the workflow manually; the cron will also run daily to refresh deals.

## Content notes
- Manual sample deals live in `content/deals/`; generated deals land in `content/deals/generated/`.
- Replace placeholder images/affiliate URLs with real ones if you keep manual entries.
- Disclosure page (`/disclosure`) already includes the Amazon-required sentence.

## Deployment
- GitHub Action builds and deploys to `gh-pages`. Pages settings should point to `gh-pages` (root).
