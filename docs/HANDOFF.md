# HTCAH Platform — Developer Handoff

## What This Is
A multi-tenant food platform. Restaurants/creators/families onboard, get USDA-matched nutrition data, 
flavor profiles, menu engineering, and a branded static site. All from a 30MB SQLite database and YAML configs.

## Architecture (3 layers)
1. **Metadata** (`source/`) — YAML files define everything. Git-tracked, human-readable.
2. **Database** (`data/usda/foundation.db`) — 84,673 USDA foods + all tenant/inventory/dish data. Single SQLite file.
3. **Connectors** (Python scripts) — Bridge YAML ↔ DB ↔ HTML output.

## Entry Points
- `server.py` — WSGI app. Serves static HTML + JSON API. Handles subdomain + path-based tenant routing.
- `onboard_api.py` — Onboarding pipeline. Form data → tenant config → USDA matching → flavor profile → site.
- `tenant_publish.py` — Static site generator. YAML → HTML pages per tenant.
- `eval_engine.py` — Platform health (30 checks, 8 categories). Currently at 90%.
- `local_ai.py` — Ollama integration. Points a local model at the SQLite DB for natural-language queries.

## Run Locally
```bash
pip install -r requirements.txt
python3 tenant_publish.py   # Build all sites
python3 server.py           # Serve on :8080
```

## Deploy to Production
```bash
# render.yaml is configured. Push to GitHub, connect to Render.
# For subdomains: add *.howtocookathome.com CNAME to your-app.onrender.com
# Or: any VPS with Nginx + Let's Encrypt + gunicorn.
```

## Key Specs (from xlsx files)
- `HTCAH_Platform_Scope_Gap_Analysis.xlsx` — Build plan, 4 phases, 12 weeks
- `HTCAH_Episode_Matrix_v1.xlsx` — Business loop, partner intake, build priority
- `HowToCookAtHome_Content_Bible.xlsx` — Series bible, revenue roadmap, script templates

## Tier System (source/tiers.yaml)
- **Seed** ($0) — Subdomain, 10 pages, 3 products, JSON API, value dashboard
- **Sprout** ($9/mo) — Custom email, checkout, 50 pages, 25 products, PDF export
- **Harvest** ($29/mo) — Custom domain, unlimited, analytics, white-label

## What's Built vs What's Spec
| Feature | Status |
|---------|--------|
| Onboarding (name → site) | ✅ Built |
| USDA matching (7-strategy) | ✅ Built |
| Subdomain routing | ✅ Built |
| Flavor profiling | ✅ Built |
| Menu engineering | ✅ Built |
| Tenant static sites | ✅ Built |
| Platform health eval | ✅ Built (90%) |
| Local AI (Ollama) | ✅ Built |
| Returning-user memory | ✅ Built |
| Tier enforcement (limits) | ❌ Spec only |
| Stripe checkout | ❌ Spec only |
| Value dashboard | ❌ Spec only |
| Contribution unlocks | ❌ Not started |
| Photo-to-recipe intake | ❌ Not started |
| Flavor profiler UI | ❌ Not started |
| Email sequences | ❌ Not started |

## File Count
~3,500 lines of Python across 10 files. 30 episode configs. 10 tenant configs. 3 dependencies.

## Philosophy
- No cookies. No tracking. No third-party scripts.
- YAML is the source of truth. Change YAML, regenerate everything.
- Death2Data: local-first, privacy-first, SQLite + Ollama on-device.
- Value walls, not paywalls. Show the value before asking for money.
