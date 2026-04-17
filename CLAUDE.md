# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run the development server (port 3050, or $PORT env var)
python3 -m app.server

# Build all tenant sites (generates output/)
python3 -m app.publish

# Build a single tenant
python3 -m app.publish <tenant-slug>

# List all tenants
python3 -m app.publish --list

# Run tests
python3 tests/run_tests.py
pytest tests/ -v
pytest tests/test_db.py -v

# Platform health evaluation
python3 -m app.eval
python3 -m app.eval --quick

# Build USDA database (one-time, downloads ~450MB)
./scripts/build_db.sh

# Weekly "yap" newsletter pipeline
python3 scripts/build_yap.py                 # auto-draft content/yap/{YYYY-Www}.yaml
python3 scripts/render_yap.py                # render PDF to output/yap/{week}.pdf
SMTP_DRY_RUN=1 python3 scripts/send_yap.py   # dry-run writes .eml to data/mail_log/
python3 scripts/send_yap.py                  # real send (requires SMTP_* env vars)
python3 scripts/send_yap.py --bucket patron  # send to only one bucket
```

## Architecture

**Multi-tenant B2B SaaS for restaurants + a consumer-facing "cook-at-home dividend" loop.** Each tenant (restaurant) gets a branded static site with nutrition data, menu engineering, and flavor profiles derived from USDA data. The public-facing side (tip-better / next-show) captures home cooks who save money by cooking and redirect it toward hospitality tipping or experiences.

### Data Flow

```
YAML configs (source of truth)
  → app/onboard.py  (processes restaurant form submissions)
  → app/usda.py     (7-strategy ingredient matching against USDA database)
  → app/menu.py     (Star/Plowhorse/Puzzle/Dog classification)
  → app/db.py       (stores in htcah.db)
  → app/publish.py  (generates HTML for output/tenants/<slug>/ and output/web/)
  → app/server.py   (serves static HTML + JSON API, routes by subdomain)
```

### Two SQLite Databases

- **`data/htcah.db`** — Read-write app database. Tables include tenants, dishes, dish_ingredients, inventory_items, flavor_scores, nutrition, reviews, orders, and **subscribers** (email newsletter list with unsubscribe tokens).
- **`data/usda/foundation.db`** — Read-only USDA reference (84K+ foods, ~2.4M nutrient records, 30MB). ATTACHed to htcah.db as the `usda` schema for cross-database joins.

```python
from app.db import DB
db = DB()  # Opens htcah.db and ATTACHes USDA as 'usda' schema
```

### Key Modules

| Module | Role |
|--------|------|
| `app/server.py` | WSGI entry point; subdomain routing; includes POST /api/subscribe and GET /unsubscribe/<token> |
| `app/onboard.py` | Restaurant onboarding pipeline (form → site generation) |
| `app/db.py` | All database operations; dual-DB layer with WAL mode + FK; subscriber helpers |
| `app/usda.py` | USDA ingredient matching (exact → synonym → substring → fuzzy → category → phonetic → regional) |
| `app/menu.py` | Menu engineering: food cost %, contribution margin, dish classification |
| `app/publish.py` | Static site generator using Jinja2 templates |
| `app/eval.py` | Platform health checks |
| `app/cookbook.py` | Per-tenant cookbook PDF generation via ReportLab |
| `app/review.py` | Versioned audit/review engine |
| `app/ai.py` | Local AI via Ollama |
| `app/mail.py` | SMTP abstraction (env-parameterized: SMTP_HOST/PORT/USER/PASS/FROM, SMTP_DRY_RUN); builds multipart/alternative + attachments, writes .eml files in dry-run |
| `app/checkout.py` | Stripe checkout session + webhook handling |
| `app/branded.py` | Tenant branding helpers used by publish |
| `app/foundation.py` | USDA foundation database loader / attach helpers |
| `app/market.py` | Market / pricing helpers |
| `app/migrate.py` | Schema migration runner |

### Weekly "Yap" Newsletter Pipeline

A PDF-as-email consumer newsletter that drives the tip-better / next-show loops.

| Script | Role |
|--------|------|
| `scripts/build_yap.py` | Auto-drafts `content/yap/{YYYY-Www}.yaml` (deterministic 3-recipe pick via `random.Random(week_id)`). Matt edits before send. |
| `scripts/render_yap.py` | YAML → branded PDF at `output/yap/{week}.pdf` (ReportLab, red/black aesthetic, savings math rendered) |
| `scripts/send_yap.py` | Sends HTML+text multipart email with PDF attached. Per-recipient unsubscribe token, RFC 8058 one-click headers. Args: `--week`, `--bucket {patron|experience}`, `--only`, `--limit`, `--throttle`. |

Scheduled tasks:
- Saturday 10am — `scripts/build_yap.py` auto-draft (Matt edits over weekend)
- Sunday 9am — `scripts/send_yap.py` ships it to active subscribers

### Content / Configuration

All in `content/` as YAML (source of truth, git-tracked):
- `content/tenants/*.yaml` — Per-tenant branding, colors, nav, footer
- `content/recipes/*.yaml` — Recipe definitions
- `content/inventories/*.yaml` — Vendor pricing per tenant
- `content/episodes/*.yaml` — Cooking episode / technique content
- `content/yap/*.yaml` — Weekly newsletter drafts (YYYY-Www naming)
- `content/normalization.yaml` — Ingredient synonyms and USDA FDC ID overrides
- `content/platform.yaml` — Platform-wide settings
- `content/tiers.yaml` — Pricing tiers (Seed/$0, Sprout/$9/mo, Harvest/$29/mo)
- `content/brand.yaml`, `content/identity.yaml` — Brand / identity system
- `content/intake.yaml` — Restaurant intake form schema
- `content/domains.yaml` — Known tenant domains / subdomains
- `content/architecture.yaml`, `content/database.yaml`, `content/connectors.yaml`, `content/public-data.yaml` — System-of-record configs referenced by eval / publish

### Public-Facing Landing Pages

Under `output/web/`:
- `tip-better.html` — Captures `bucket=patron` subscribers (live tip calculator, redirects cook-at-home savings into tipping)
- `next-show.html` — Captures `bucket=experience` subscribers (goal selector: concert/festival/flight/trip/date/custom, weeks-to-goal progress math)
- `recipe_*.html` — 30+ how-to technique pages (boil water, dice an onion, make pan sauce, etc.) — the SEO + utility backbone
- `index.html`, `restaurants.html`, `shop.html`, `episodes.html` — Platform pages

Both landing pages POST to `/api/subscribe` (email + bucket + optional goal/weekly_saving), mint a per-subscriber unsubscribe token, and land on `subscribers` table.

### Menu Classification

Dishes classified on a 2×2 grid:
- **Star** — high margin + high popularity → Feature
- **Plowhorse** — low margin + high popularity → Reprice
- **Puzzle** — high margin + low popularity → Promote
- **Dog** — low margin + low popularity → Rethink

### Security Model

- No cookies; token-based identity via URL/path parameters
- Per-subscriber unsubscribe tokens (`secrets.token_urlsafe(18)`) for RFC 8058 one-click unsubscribe
- No IP tracking, no third-party scripts
- Security headers in `app/server.py`: CSP, HSTS, X-Frame-Options, X-Content-Type-Options
- Reserved subdomains protected (www, api, admin, etc.)

### Deployment

Configured for Render.com (`render.yaml`):
- Build: `pip install -r requirements.txt && python3 -m app.publish`
- Start: `gunicorn app.server:app --bind 0.0.0.0:$PORT --workers 2`
- Health check: `/api/health`

Required env vars for production send:
- `SMTP_HOST`, `SMTP_PORT` (465 SMTPS or 587 STARTTLS), `SMTP_USER`, `SMTP_PASS`, `SMTP_FROM`
- `YAP_BASE_URL` (default `https://howtocookathome.com`)
- SPF + DKIM + DMARC DNS records must be set on the sending domain or Gmail will spam-fold everything

### What's Built vs Spec

**Built:**
- Multi-tenant publishing, USDA matching, menu classification, tenant cookbooks
- Subscriber capture (tip-better, next-show) with per-subscriber unsubscribe tokens
- Weekly yap pipeline (draft → render PDF → SMTP send with attached PDF + RFC 8058 unsubscribe)
- Stripe checkout scaffolding in `app/checkout.py`

**Spec only (not yet enforced):**
- Tier enforcement on routes (`@require_tier` decorator)
- Admin dashboard at `/admin` with token auth
- Value dashboard for tenants
- Photo-to-recipe intake
- Flavor profiler UI
- Physical mail via Lob (Stage 2 of onboarding)
- Event cohort windows (Stage 3 of onboarding)
- Render persistent disk attachment (prerequisite for Postgres migration)
- Postgres migration (blocker: SQLite ATTACH DATABASE pattern needs replacing with Postgres schemas)
