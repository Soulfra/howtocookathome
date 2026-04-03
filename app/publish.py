#!/usr/bin/env python3
"""
PUBLISH.PY — Multi-tenant site generator.

Reads tenant configs + inventory + dishes → branded sites.
Each tenant gets: homepage, menu page, dish pages, blend page, API.

Usage:
  python3 -m app.publish                    # build all tenants
  python3 -m app.publish demo-taqueria      # build one tenant
  python3 -m app.publish --list             # show all tenants
"""
import yaml, json, os, sys, glob, sqlite3, math

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
CONTENT_DIR = os.path.join(BASE_DIR, "content")
TENANT_DIR = os.path.join(CONTENT_DIR, "tenants")
INV_DIR = os.path.join(CONTENT_DIR, "inventories")
USDA_DB = os.path.join(BASE_DIR, "data", "usda", "foundation.db")
OUTPUT_BASE = os.path.join(BASE_DIR, "output", "tenants")


def _fmt_amount(raw_amt, raw_unit, item_name=""):
    """Convert raw decimal amounts to human-readable imperial + metric.

    Returns (imperial_str, metric_str) — e.g. ("1.6 oz", "45 g").
    Handles: lb→oz, ea stays ea, pinch for tiny spice amounts.
    """
    if not raw_amt:
        return ("", "")

    try:
        amt = float(raw_amt)
    except (ValueError, TypeError):
        return (f"{raw_amt} {raw_unit}".strip(), "")

    unit = (raw_unit or "").strip().lower()
    name_lower = (item_name or "").lower()

    # --- ea (each) stays as-is ---
    if unit == "ea":
        imp = f"{int(amt)}" if amt == int(amt) else f"{amt}"
        return (f"{imp} ea", "")

    # --- lb conversions ---
    if unit == "lb":
        oz = amt * 16
        grams = amt * 453.592

        # Tiny amounts — context matters
        # Use word boundary check to avoid "salt" matching "unsalted"
        import re as _re
        _spice_words = ["salt", "pepper", "cumin", "paprika", "garlic", "oregano",
                        "thyme", "cayenne", "chili", "cinnamon", "nutmeg"]
        is_spice = any(_re.search(r'\b' + k + r'\b', name_lower) for k in _spice_words)
        metric_small = f"{grams:.0f} g" if grams >= 1 else f"{grams:.1f} g"

        if oz < 0.2 and is_spice:
            return ("pinch", metric_small)
        if oz < 0.5:
            # Butter/oil: use tsp (1 tsp butter ≈ 0.17 oz / ~5g)
            tsp = round(grams / 5)  # ~5g per tsp
            if tsp <= 0:
                tsp = 1
            if tsp == 1:
                return ("1 tsp", metric_small)
            if tsp == 2:
                return ("2 tsp", metric_small)
            # 3+ tsp → convert to tbsp
            tbsp = round(tsp / 3, 1)
            if tbsp == int(tbsp):
                tbsp = int(tbsp)
            return (f"{tbsp} tbsp", metric_small)

        # Normal oz range — use fractions where clean
        metric = f"{grams:.0f} g" if grams >= 10 else f"{grams:.1f} g"

        # Whole pounds
        if amt >= 1:
            if amt == int(amt):
                return (f"{int(amt)} lb", metric)
            lbs = int(amt)
            remainder_oz = (amt - lbs) * 16
            if remainder_oz < 0.5:
                return (f"{lbs} lb", metric)
            return (f"{lbs} lb {remainder_oz:.0f} oz", metric)

        # Sub-pound: display as oz
        # Round to nearest 0.5 oz for readability
        rounded_oz = round(oz * 2) / 2
        if rounded_oz == int(rounded_oz):
            return (f"{int(rounded_oz)} oz", metric)
        return (f"{rounded_oz:.1f} oz", metric)

    # --- Fallback: show raw ---
    return (f"{amt} {unit}".strip(), "")


def read_yaml(path):
    with open(path) as f:
        return yaml.safe_load(f)


def load_all_recipes():
    """Load all recipes from content/recipes/."""
    recipes = {}
    for f in glob.glob(os.path.join(CONTENT_DIR, "recipes", "*.yaml")):
        r = read_yaml(f)
        if r and r.get("slug"):
            recipes[r["slug"]] = r
    return recipes


def load_inventory(slug):
    """Load a tenant's vendor inventory if it exists."""
    path = os.path.join(INV_DIR, f"{slug}-inventory.yaml")
    if os.path.exists(path):
        return read_yaml(path)
    return None


def get_usda_stats():
    if not os.path.exists(USDA_DB):
        return None
    conn = sqlite3.connect(USDA_DB)
    stats = {}
    try:
        stats["foods"] = conn.execute("SELECT COUNT(*) FROM food").fetchone()[0]
        stats["nutrients"] = conn.execute("SELECT COUNT(*) FROM nutrient").fetchone()[0]
        meta = dict(conn.execute("SELECT key, value FROM import_meta").fetchall())
        stats["usda_release"] = meta.get("usda_release_date", "unknown")
    except:
        pass
    conn.close()
    return stats


def load_tenant(slug):
    path = os.path.join(TENANT_DIR, f"{slug}.yaml")
    if not os.path.exists(path):
        return None
    return read_yaml(path)


def list_tenants():
    tenants = []
    for f in sorted(glob.glob(os.path.join(TENANT_DIR, "*.yaml"))):
        t = read_yaml(f)
        if t:
            tenants.append(t)
    return tenants


# ============================================================
# SHARED HTML COMPONENTS
# ============================================================

def _font_imports():
    """Google Fonts link for Lora (warm serif) + DM Sans (clean sans)."""
    return '<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin><link href="https://fonts.googleapis.com/css2?family=DM+Sans:ital,opsz,wght@0,9..40,300;0,9..40,400;0,9..40,500;0,9..40,600;0,9..40,700;1,9..40,400&family=Lora:ital,wght@0,400;0,500;0,600;0,700;1,400&display=swap" rel="stylesheet">'


def _base_css(c, f):
    """Base CSS variables + reset for all pages."""
    # Use Google Fonts with tenant overrides as fallback
    heading = f.get("heading", "'Lora', Georgia, 'Times New Roman', serif")
    body = f.get("body", "'DM Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif")
    # Inject Lora/DM Sans as primary if tenant uses generic system fonts
    if heading.startswith("Georgia"):
        heading = f"'Lora', {heading}"
    if body.startswith("Arial"):
        body = f"'DM Sans', {body}"

    return f'''
:root {{
  --primary:{c.get("primary","#1A1A2E")};
  --accent:{c.get("accent","#C4975A")};
  --bg:{c.get("background","#FFFBF5")};
  --surface:{c.get("surface","#FFFFFF")};
  --text:{c.get("text","#1C1917")};
  --text-light:{c.get("text_light","#57534E")};
  --success:{c.get("success","#2D6A4F")};
  --heading:{heading};
  --body:{body};
  --radius: 16px;
  --shadow-sm: 0 1px 2px rgba(0,0,0,0.04), 0 1px 4px rgba(0,0,0,0.03);
  --shadow-md: 0 4px 12px rgba(0,0,0,0.06), 0 1px 3px rgba(0,0,0,0.04);
  --shadow-lg: 0 8px 24px rgba(0,0,0,0.08), 0 2px 6px rgba(0,0,0,0.04);
  --transition: 0.2s cubic-bezier(0.4, 0, 0.2, 1);
}}
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{
  font-family:var(--body);
  color:var(--text);
  background:var(--bg);
  line-height:1.7;
  -webkit-font-smoothing:antialiased;
  -moz-osx-font-smoothing:grayscale;
  font-size:15px;
}}
h1,h2,h3,h4 {{ font-family:var(--heading); font-weight:600; line-height:1.25; letter-spacing:-0.02em; }}
a {{ color:var(--accent); text-decoration:none; transition:color var(--transition); }}
a:hover {{ color:var(--primary); }}
img {{ max-width:100%; height:auto; }}
::selection {{ background:var(--accent); color:white; }}
'''


def _nav_css():
    return '''
nav {
  background:var(--primary);
  padding:0.875rem 2rem;
  display:flex;
  justify-content:space-between;
  align-items:center;
  position:sticky;
  top:0;
  z-index:100;
  box-shadow:0 1px 0 rgba(255,255,255,0.06);
  backdrop-filter:blur(12px);
}
nav .logo {
  font-family:var(--heading);
  color:var(--accent);
  font-size:1.35rem;
  text-decoration:none;
  font-weight:700;
  letter-spacing:-0.03em;
}
nav .links { display:flex; gap:1.75rem; align-items:center; }
nav .links a {
  color:rgba(255,255,255,0.7);
  text-decoration:none;
  font-size:0.82rem;
  font-weight:500;
  letter-spacing:0.01em;
  transition:all var(--transition);
  padding:0.25rem 0;
  border-bottom:2px solid transparent;
}
nav .links a:hover {
  color:var(--accent);
  text-decoration:none;
  border-bottom-color:var(--accent);
}
'''


def _card_css():
    return '''
.card {
  background:var(--surface);
  border-radius:var(--radius);
  padding:1.75rem;
  margin-bottom:1.25rem;
  box-shadow:var(--shadow-sm);
  border:1px solid rgba(0,0,0,0.04);
  transition:box-shadow var(--transition), transform var(--transition);
}
.card:hover { box-shadow:var(--shadow-md); }
.card h2 {
  font-family:var(--heading);
  font-size:1.1rem;
  color:var(--primary);
  margin-bottom:0.875rem;
  padding-bottom:0.5rem;
  border-bottom:2px solid var(--accent);
}
'''


def _footer_css():
    return '''
footer {
  background:var(--primary);
  color:rgba(255,255,255,0.45);
  padding:2.5rem 2rem;
  text-align:center;
  font-size:0.78rem;
  margin-top:4rem;
  letter-spacing:0.01em;
  line-height:1.8;
}
footer a { color:var(--accent); text-decoration:none; font-weight:500; }
footer a:hover { text-decoration:underline; }
'''


def _responsive_css():
    return '''
.container { max-width:960px; margin:0 auto; padding:2.5rem 2rem; }

@media(max-width:768px) {
  .grid-2col { grid-template-columns:1fr !important; }
  nav { padding:0.75rem 1rem; }
  nav .links { gap:1rem; }
  nav .links a { font-size:0.78rem; }
  .container { padding:1.75rem 1.25rem !important; }
  .hero-inner { padding:2.5rem 1.25rem !important; }
  h1 { font-size:1.75rem !important; }
}
@media(max-width:480px) {
  nav { flex-direction:column; gap:0.5rem; }
  nav .links { flex-wrap:wrap; justify-content:center; gap:0.75rem; }
}
'''


def _nav_html(tenant):
    links = ""
    for n in tenant.get("nav", []):
        links += f'<a href="{n["href"]}">{n["label"]}</a>'
    links += '<a href="menu.html">Menu Analysis</a>'
    links += '<a href="review.html">Review</a>'
    return f'''<nav>
  <a href="index.html" class="logo">{tenant.get("logo_text", tenant["name"])}</a>
  <div class="links">{links}</div>
</nav>'''


def _footer_html(tenant):
    c = tenant.get("colors", {})
    parts = [f'&copy; 2026 {tenant.get("footer", {}).get("copyright", tenant["name"])}']
    if tenant.get("footer", {}).get("powered_by"):
        parts.append(f'<a href="{tenant["footer"]["powered_by_href"]}">{tenant["footer"]["powered_by"]}</a>')
    parts.append('<a href="https://fdc.nal.usda.gov/">USDA FoodData Central</a>')
    return f'<footer>{" &middot; ".join(parts)}</footer>'


def _badge(text, bg, fg="#fff"):
    return f'<span style="display:inline-block;background:{bg};color:{fg};padding:0.15rem 0.55rem;font-size:0.7rem;font-weight:600;text-transform:uppercase;letter-spacing:0.04em;border-radius:4px;">{text}</span>'


# ============================================================
# DISH PAGE (replaces old recipe page)
# ============================================================

def render_dish_page(tenant, recipe, usda):
    """Render one dish/recipe page with full data."""
    c = tenant.get("colors", {})
    f = tenant.get("fonts", {})
    is_dish = recipe.get("type") == "dish"
    menu_pricing = recipe.get("menu_pricing", {})

    # --- Ingredients ---
    ing_rows = ""
    for ing in recipe.get("ingredients", []):
        # New dish format: usage_amount/usage_unit
        # Old recipe format: amount/unit
        raw_amt = ing.get("usage_amount") or ing.get("amount") or ""
        raw_unit = ing.get("usage_unit") or ing.get("unit") or ""
        name = ing.get("display_name", ing.get("item", ""))

        imperial, metric = _fmt_amount(raw_amt, raw_unit, name)

        item_cost = ing.get("item_cost")
        cost_tag = ""
        if item_cost and item_cost > 0:
            cost_tag = f'<span style="font-size:0.75rem;color:var(--success);font-weight:600;">${item_cost:.2f}</span>'

        # USDA match indicator
        fdc = ing.get("usda_fdc_id")
        src = ing.get("_match_source", "")
        usda_tag = ""
        if fdc:
            usda_tag = _badge("USDA", "#1565c0")
        elif src == "normalization":
            usda_tag = _badge("SYN", "#2e7d32")

        # Build amount display with both imperial and metric (metric hidden by default)
        amt_display = ""
        if imperial:
            metric_span = f'<span class="metric" style="display:none;">{metric}</span>' if metric else ''
            imperial_span = f'<span class="imperial">{imperial}</span>'
            amt_display = f'<span style="font-family:monospace;font-size:0.85rem;color:var(--accent);min-width:90px;display:inline-block;">{imperial_span}{metric_span}</span>'

        ing_rows += f'''<div style="display:flex;align-items:center;padding:0.6rem 0;border-bottom:1px solid rgba(0,0,0,0.05);gap:0.75rem;">
  {amt_display}
  <span style="flex:1;font-weight:500;">{name}</span>
  {cost_tag}
  {usda_tag}
</div>'''

    # --- Pricing card (for dishes with cost data) ---
    pricing_html = ""
    if is_dish and menu_pricing:
        food_cost = menu_pricing.get("food_cost", 0)
        menu_price = menu_pricing.get("menu_price", 0)
        margin = menu_price - food_cost if menu_price else 0
        margin_pct = (margin / menu_price * 100) if menu_price > 0 else 0

        pricing_html = f'''<div class="card">
  <h2>Pricing</h2>
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:1rem;">
    <div style="text-align:center;padding:1rem;background:rgba(0,0,0,0.02);border-radius:8px;">
      <div style="font-size:0.7rem;text-transform:uppercase;color:var(--text-light);letter-spacing:0.05em;">Food Cost</div>
      <div style="font-size:1.8rem;font-weight:700;color:var(--primary);">${food_cost:.2f}</div>
    </div>
    <div style="text-align:center;padding:1rem;background:rgba(0,0,0,0.02);border-radius:8px;">
      <div style="font-size:0.7rem;text-transform:uppercase;color:var(--text-light);letter-spacing:0.05em;">Menu Price</div>
      <div style="font-size:1.8rem;font-weight:700;color:var(--success);">${menu_price:.2f}</div>
    </div>
  </div>
  <div style="margin-top:1rem;padding:0.75rem;background:var(--success);color:white;border-radius:8px;text-align:center;">
    <span style="font-size:0.8rem;">Contribution Margin:</span>
    <strong style="font-size:1.1rem;margin-left:0.5rem;">${margin:.2f}</strong>
    <span style="font-size:0.8rem;opacity:0.8;margin-left:0.3rem;">({margin_pct:.0f}%)</span>
  </div>
</div>'''

    # --- Nutrition ---
    nutri = recipe.get("enrichment", {}).get("nutrition", {})
    details = nutri.get("details", {})
    # Build nutrition from enrichment data OR directly from ingredients
    nutri_html = ''
    if not details:
        # Try building from ingredients directly
        for ing in recipe.get("ingredients", []):
            fdc_id = ing.get("usda_fdc_id")
            if fdc_id:
                try:
                    from app.onboard import get_nutrition
                    nutri = get_nutrition(fdc_id)
                    if nutri:
                        details[ing.get("display_name", ing.get("item", ""))] = {
                            "fdc_id": fdc_id,
                            "per_100g": nutri,
                        }
                except Exception:
                    pass

    if details:
        nutri_html = '<div style="overflow-x:auto;"><table style="width:100%;font-size:0.8rem;border-collapse:collapse;table-layout:fixed;">'
        nutri_html += '<tr style="border-bottom:2px solid rgba(0,0,0,0.1);"><td style="padding:0.4rem;font-weight:700;width:40%;">Ingredient</td><td style="text-align:center;font-weight:700;width:15%;">Cal</td><td style="text-align:center;font-weight:700;width:15%;">Pro</td><td style="text-align:center;font-weight:700;width:15%;">Fat</td><td style="text-align:center;font-weight:700;width:15%;">Carbs</td></tr>'
        for key, data in details.items():
            p = data.get("per_100g", {})
            cal = str(p.get("calories") or "-")
            pro = str(p.get("protein_g") or "-")
            fat = str(p.get("fat_g") or "-")
            carb = str(p.get("carbs_g") or "-")
            # Truncate long ingredient names
            short_key = key[:20] + "..." if len(key) > 20 else key
            nutri_html += f'<tr style="border-bottom:1px solid rgba(0,0,0,0.04);"><td style="padding:0.4rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="{key}">{short_key}</td><td style="text-align:center;">{cal}</td><td style="text-align:center;">{pro}g</td><td style="text-align:center;">{fat}g</td><td style="text-align:center;">{carb}g</td></tr>'
        nutri_html += '</table></div>'
        nutri_html += '<div style="font-size:0.65rem;color:var(--text-light);margin-top:0.5rem;">Per 100g raw. Source: USDA FoodData Central.</div>'
    else:
        nutri_html = '<div style="color:var(--text-light);font-style:italic;font-size:0.85rem;">No USDA nutrition matches yet. As more ingredients are matched, nutrition data will appear here.</div>'

    # --- Flavor bars ---
    flavor = recipe.get("flavor_profile", {}).get("scores", {})
    # If no pre-computed profile, try building from ingredients at render time
    if not flavor:
        try:
            from app.onboard import build_dish_flavor_profile
            flavor = build_dish_flavor_profile(recipe.get("ingredients", []))
        except Exception:
            pass

    fcolors = {
        "salt":"#607d8b", "acid":"#4caf50", "fat":"#ff9800", "sweet":"#e91e63",
        "heat":"#f44336", "umami":"#9c27b0", "bitter":"#795548", "aromatic":"#00bcd4",
        "intensity":"#ff6f00", "rich":"#ff9800",
    }
    # Friendly labels
    flabels = {
        "salt":"Salty", "acid":"Bright/Acid", "fat":"Richness", "sweet":"Sweet",
        "heat":"Heat", "umami":"Savory/Umami", "bitter":"Bitter", "aromatic":"Aromatic",
        "intensity":"Intensity",
    }
    flavor_html = ""
    if flavor:
        # Sort by score descending for visual hierarchy
        sorted_dims = sorted(flavor.items(), key=lambda x: x[1], reverse=True)
        for dim, score in sorted_dims:
            # Clamp to 0-10 (should already be normalized, but safety)
            score = min(max(score, 0), 10)
            pct = (score / 10) * 100
            fc = fcolors.get(dim, "#999")
            label = flabels.get(dim, dim.title())
            flavor_html += f'''<div style="margin:0.35rem 0;">
  <div style="display:flex;justify-content:space-between;font-size:0.7rem;font-weight:600;text-transform:uppercase;color:var(--text-light);"><span>{label}</span><span>{score:.1f}/10</span></div>
  <div style="height:6px;background:rgba(0,0,0,0.06);border-radius:3px;"><div style="height:100%;width:{pct}%;background:{fc};border-radius:3px;transition:width 0.3s;"></div></div>
</div>'''

    # --- Steps ---
    steps_html = ""
    step_num = 0
    for step in recipe.get("steps", []):
        if isinstance(step, str) and step.strip() and step != "(Add your preparation steps here)":
            for sub in step.split(". "):
                sub = sub.strip().rstrip(".")
                if sub:
                    step_num += 1
                    steps_html += f'''<div style="display:flex;gap:0.8rem;padding:0.6rem 0;border-bottom:1px solid rgba(0,0,0,0.04);">
  <span style="flex-shrink:0;display:flex;align-items:center;justify-content:center;background:var(--accent);color:white;width:28px;height:28px;font-size:0.75rem;font-weight:700;border-radius:50%;">{step_num}</span>
  <span style="padding-top:3px;">{sub}.</span>
</div>'''

    if not steps_html:
        steps_html = '<div style="color:var(--text-light);font-style:italic;">Preparation steps will appear here once added.</div>'

    # --- Spice analysis ---
    spice_html = ""
    spice_data = recipe.get("spice_analysis", {})
    if spice_data and spice_data.get("spices"):
        spice_html = '<div class="card"><h2>Spice Profile</h2>'
        for s in spice_data["spices"]:
            val = s.get("value_score")
            val_tag = f' &middot; <span style="color:var(--success);font-weight:600;">{val:.1f} flavor pts/$</span>' if val else ""
            dims = ", ".join(s.get("flavor_dimensions", []))
            spice_html += f'''<div style="padding:0.5rem 0;border-bottom:1px solid rgba(0,0,0,0.04);">
  <div style="display:flex;justify-content:space-between;align-items:center;">
    <span style="font-weight:600;">{s["item"]}</span>
    <span style="font-size:0.8rem;color:var(--text-light);">Intensity: {s.get("flavor_intensity","?")}/10{val_tag}</span>
  </div>
  <div style="font-size:0.75rem;color:var(--text-light);margin-top:2px;">{dims}</div>
</div>'''
        if spice_data.get("best_value"):
            spice_html += f'<div style="margin-top:0.75rem;padding:0.5rem;background:rgba(45,106,79,0.08);border-radius:6px;font-size:0.85rem;"><strong>Best value:</strong> {spice_data["best_value"]}</div>'
        spice_html += '</div>'

    # --- Cuisine badge ---
    cuisine = recipe.get("cuisine", "")
    cuisine_badge = _badge(cuisine, c.get("accent", "#C4975A")) if cuisine else ""

    # --- Auto-generated notice ---
    auto_notice = ""
    if recipe.get("source", {}).get("type") == "auto_generated":
        auto_notice = f'<div style="background:#fff3cd;border:1px solid #ffc107;border-radius:8px;padding:0.75rem 1rem;margin-bottom:1rem;font-size:0.85rem;color:#856404;">Auto-generated from your vendor inventory. <strong>Edit your dishes</strong> to refine ingredients and amounts.</div>'

    usda_count = f'{usda["foods"]:,}' if usda else "84,673"

    return f'''<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{recipe["title"]} | {tenant["name"]}</title>
{_font_imports()}
<style>
{_base_css(c, f)}
{_nav_css()}
{_card_css()}
{_footer_css()}
.container {{ max-width:960px; margin:0 auto; padding:2rem; }}
.grid-2col {{ display:grid; grid-template-columns:1fr 320px; gap:1.5rem; }}
{_responsive_css()}
</style>
</head><body>
{_demo_banner(tenant)}
{_nav_html(tenant)}
<div style="background:var(--primary);color:white;padding:2.5rem 2rem;">
  <div class="hero-inner" style="max-width:960px;margin:0 auto;">
    {cuisine_badge}
    <h1 style="font-family:var(--heading);font-size:2rem;color:var(--accent);margin-top:0.5rem;">{recipe["title"]}</h1>
    <div style="font-size:0.75rem;opacity:0.4;margin-top:0.3rem;">Backed by {usda_count} foods from USDA FoodData Central</div>
  </div>
</div>
<div class="container">
  {auto_notice}
  <div class="grid-2col">
    <div>
      <div class="card"><div style="display:flex;justify-content:space-between;align-items:center;"><h2 style="margin:0;">Ingredients</h2><button onclick="toggleUnits(this)" style="font-size:0.7rem;padding:0.3rem 0.6rem;border:1px solid var(--accent);background:transparent;color:var(--accent);border-radius:4px;cursor:pointer;font-weight:600;">METRIC</button></div>{ing_rows}</div>
      <div class="card"><h2>Method</h2>{steps_html}</div>
    </div>
    <div>
      {pricing_html}
      <div class="card"><h2>Nutrition (per 100g)</h2>{nutri_html}</div>
      {"" if not flavor_html else f'<div class="card"><h2>Flavor Profile</h2>{flavor_html}</div>'}
      {spice_html}
    </div>
  </div>
</div>
{_footer_html(tenant)}
<script>
function toggleUnits(btn){{
  var card=btn.closest('.card');
  var imps=card.querySelectorAll('.imperial');
  var mets=card.querySelectorAll('.metric');
  var showMetric=imps[0]&&imps[0].style.display!=='none';
  imps.forEach(function(e){{e.style.display=showMetric?'none':'inline';}});
  mets.forEach(function(e){{e.style.display=showMetric?'inline':'none';}});
  btn.textContent=showMetric?'IMPERIAL':'METRIC';
}}
</script>
</body></html>'''


# ============================================================
# INDEX PAGE
# ============================================================

def _demo_banner(tenant):
    """Show a clear demo banner if this tenant is sample data."""
    if tenant.get("type") != "demo":
        return ""
    return '''<div style="background:#FEF3C7;border-bottom:2px solid #F59E0B;padding:0.6rem 1.5rem;text-align:center;font-size:0.82rem;color:#92400E;font-weight:500;letter-spacing:0.01em;">
  <strong>Sample Restaurant</strong> — This is demo data showing what your site could look like.
  <a href="/" style="color:#B45309;text-decoration:underline;margin-left:0.5rem;font-weight:600;">Get yours &rarr;</a>
</div>'''


def render_index(tenant, recipes, usda, inventory=None):
    """Render the tenant's homepage."""
    c = tenant.get("colors", {})
    f = tenant.get("fonts", {})

    # Inventory summary
    inv_html = ""
    if inventory:
        summary = inventory.get("summary", {})
        inv_html = f'''<div style="display:flex;gap:1rem;justify-content:center;margin-top:1.5rem;flex-wrap:wrap;">
  <div style="background:rgba(255,255,255,0.1);padding:0.6rem 1.2rem;border-radius:8px;text-align:center;">
    <div style="font-size:1.5rem;font-weight:700;color:var(--accent);">{summary.get("total_items", 0)}</div>
    <div style="font-size:0.7rem;opacity:0.7;">Inventory Items</div>
  </div>
  <div style="background:rgba(255,255,255,0.1);padding:0.6rem 1.2rem;border-radius:8px;text-align:center;">
    <div style="font-size:1.5rem;font-weight:700;color:var(--accent);">${summary.get("total_cost", 0):,.2f}</div>
    <div style="font-size:0.7rem;opacity:0.7;">Vendor Total</div>
  </div>
  <div style="background:rgba(255,255,255,0.1);padding:0.6rem 1.2rem;border-radius:8px;text-align:center;">
    <div style="font-size:1.5rem;font-weight:700;color:var(--accent);">{summary.get("usda_matched", 0)}</div>
    <div style="font-size:0.7rem;opacity:0.7;">USDA Matched</div>
  </div>
</div>'''

    # Dish cards
    cards = ""
    for r in recipes:
        if r.get("type") == "inventory":
            continue

        ing_count = len(r.get("ingredients", []))
        menu_pricing = r.get("menu_pricing", {})
        price = menu_pricing.get("menu_price", 0)
        food_cost = menu_pricing.get("food_cost", 0)

        price_tag = f'<span style="font-size:1.1rem;font-weight:700;color:var(--success);">${price:.2f}</span>' if price else ''
        cost_tag = f'<span style="font-size:0.75rem;color:var(--text-light);">Cost: ${food_cost:.2f}</span>' if food_cost else ''
        cuisine = r.get("cuisine", "")
        cuisine_tag = f'<span style="font-size:0.7rem;color:var(--accent);text-transform:uppercase;letter-spacing:0.05em;font-weight:600;">{cuisine}</span>' if cuisine else ''

        cards += f'''<a href="r_{r['slug']}.html" style="background:var(--surface);border-radius:var(--radius);padding:1.75rem;text-decoration:none;color:inherit;display:block;box-shadow:var(--shadow-sm);border:1px solid rgba(0,0,0,0.04);transition:transform var(--transition),box-shadow var(--transition);" onmouseover="this.style.transform='translateY(-3px)';this.style.boxShadow='var(--shadow-lg)'" onmouseout="this.style.transform='';this.style.boxShadow='var(--shadow-sm)'">
  {cuisine_tag}
  <h3 style="font-family:var(--heading);font-size:1.15rem;color:var(--primary);margin:0.4rem 0 0.5rem;letter-spacing:-0.01em;">{r["title"]}</h3>
  <div style="font-size:0.82rem;color:var(--text-light);display:flex;gap:0.75rem;align-items:center;margin-top:0.5rem;">
    <span>{ing_count} ingredients</span>
    {_badge("USDA", "#1565c0")}
  </div>
  <div style="display:flex;justify-content:space-between;align-items:center;margin-top:0.875rem;padding-top:0.75rem;border-top:1px solid rgba(0,0,0,0.04);">
    {price_tag}
    {cost_tag}
  </div>
</a>'''

    if not cards:
        cards = '<div style="text-align:center;padding:2rem;color:var(--text-light);font-style:italic;">No dishes yet. Submit your menu or vendor invoice to get started.</div>'

    usda_line = f'{usda["foods"]:,} foods' if usda else "84,673 foods"

    return f'''<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{tenant["name"]}</title>
{_font_imports()}
<style>
{_base_css(c, f)}
{_nav_css()}
{_card_css()}
{_footer_css()}
.container {{ max-width:960px; margin:0 auto; padding:2rem; }}
{_responsive_css()}
</style>
</head><body>
{_demo_banner(tenant)}
{_nav_html(tenant)}
<div style="background:var(--primary);color:white;padding:5rem 2rem 4rem;text-align:center;">
  <h1 style="font-family:var(--heading);font-size:2.6rem;color:var(--accent);letter-spacing:-0.03em;font-weight:700;">{tenant["name"]}</h1>
  <p style="opacity:0.8;margin-top:0.75rem;max-width:520px;margin-left:auto;margin-right:auto;font-size:1.1rem;line-height:1.6;font-weight:300;">{tenant.get("tagline","")}</p>
  <div style="font-size:0.72rem;opacity:0.3;margin-top:1.5rem;letter-spacing:0.03em;text-transform:uppercase;">Powered by {usda_line} from USDA FoodData Central</div>
  {inv_html}
</div>
<div class="container">
  <h2 style="font-family:var(--heading);color:var(--primary);margin-bottom:1.75rem;font-size:1.5rem;letter-spacing:-0.02em;">Menu</h2>
  <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:1.5rem;">
    {cards}
  </div>
</div>
{_footer_html(tenant)}
</body></html>'''


# ============================================================
# RESTAURANT DIRECTORY (platform-level page)
# ============================================================

def render_directory(tenants, usda):
    """Render a /restaurants page listing all non-platform tenants."""
    # Platform design tokens
    c = {"primary": "#1A1A2E", "accent": "#C4975A", "background": "#FFFBF5",
         "surface": "#FFFFFF", "text": "#1C1917", "text_light": "#57534E", "success": "#2D6A4F"}
    f = {}

    cards = ""
    live = [t for t in tenants if t.get("type") not in ("platform", "demo")]
    demos = [t for t in tenants if t.get("type") == "demo"]

    for t in live + demos:
        slug = t["slug"]
        tc = t.get("colors", {})
        accent = tc.get("accent", c["accent"])
        dish_count = len(t.get("recipes", []))
        is_demo = t.get("type") == "demo"
        demo_tag = '<span style="font-size:0.65rem;background:#FEF3C7;color:#92400E;padding:0.15rem 0.5rem;border-radius:4px;font-weight:600;text-transform:uppercase;letter-spacing:0.04em;">Demo</span> ' if is_demo else ''
        tagline = t.get("tagline", "")
        if len(tagline) > 80:
            tagline = tagline[:77] + "..."

        cards += f'''<a href="/t/{slug}/" style="background:var(--surface);border-radius:var(--radius);padding:1.75rem;text-decoration:none;color:inherit;display:block;box-shadow:var(--shadow-sm);border:1px solid rgba(0,0,0,0.04);border-top:4px solid {accent};transition:transform var(--transition),box-shadow var(--transition);" onmouseover="this.style.transform='translateY(-3px)';this.style.boxShadow='var(--shadow-lg)'" onmouseout="this.style.transform='';this.style.boxShadow='var(--shadow-sm)'">
  <div style="display:flex;justify-content:space-between;align-items:start;margin-bottom:0.5rem;">
    <h3 style="font-family:var(--heading);font-size:1.2rem;color:var(--primary);letter-spacing:-0.01em;">{t["name"]}</h3>
    {demo_tag}
  </div>
  <p style="font-size:0.85rem;color:var(--text-light);margin-bottom:0.75rem;line-height:1.5;">{tagline}</p>
  <div style="display:flex;gap:0.75rem;font-size:0.75rem;color:var(--text-light);">
    <span>{dish_count} dishes</span>
    <span style="color:var(--accent);font-weight:600;">View site &rarr;</span>
  </div>
</a>'''

    usda_line = f'{usda["foods"]:,} foods' if usda else "84,673 foods"

    return f'''<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Restaurants | HowToCookAtHome</title>
{_font_imports()}
<style>
{_base_css(c, f)}
{_nav_css()}
{_card_css()}
{_footer_css()}
.container {{ max-width:960px; margin:0 auto; padding:2rem; }}
{_responsive_css()}
</style>
</head><body>
<nav>
  <a class="logo" href="/">HowToCookAtHome</a>
  <div class="links">
    <a href="/restaurants">Restaurants</a>
    <a href="/onboard">Get Your Site</a>
  </div>
</nav>
<div style="background:var(--primary);color:white;padding:4.5rem 2rem 3.5rem;text-align:center;">
  <h1 style="font-family:var(--heading);font-size:2.4rem;color:var(--accent);letter-spacing:-0.03em;">Restaurants on the Platform</h1>
  <p style="opacity:0.75;margin-top:0.75rem;max-width:520px;margin-left:auto;margin-right:auto;font-size:1.05rem;line-height:1.6;font-weight:300;">Every restaurant gets a branded site with USDA-backed nutrition, menu analytics, and cookbook generation — powered by {usda_line}.</p>
  <div style="display:flex;gap:1rem;justify-content:center;margin-top:1.5rem;">
    <div style="background:rgba(255,255,255,0.08);padding:0.5rem 1.2rem;border-radius:8px;font-size:0.85rem;">
      <strong style="color:var(--accent);">{len(live)}</strong> live
    </div>
    <div style="background:rgba(255,255,255,0.08);padding:0.5rem 1.2rem;border-radius:8px;font-size:0.85rem;">
      <strong style="color:var(--accent);">{len(demos)}</strong> demos
    </div>
  </div>
</div>
<div class="container">
  <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:1.5rem;margin-top:1rem;">
    {cards}
  </div>
  <div style="text-align:center;margin-top:3rem;padding:2rem;background:var(--surface);border-radius:var(--radius);box-shadow:var(--shadow-sm);">
    <h3 style="font-family:var(--heading);color:var(--primary);margin-bottom:0.5rem;">Want your restaurant here?</h3>
    <p style="font-size:0.9rem;color:var(--text-light);margin-bottom:1rem;">Sign up free. Get a branded site with USDA nutrition data in minutes.</p>
    <a href="/onboard" style="display:inline-block;background:var(--accent);color:var(--primary);padding:0.75rem 2rem;font-weight:700;border-radius:8px;font-size:0.95rem;">Get Started &rarr;</a>
  </div>
</div>
<footer style="background:var(--primary);color:rgba(255,255,255,0.5);padding:2rem;text-align:center;font-size:0.8rem;margin-top:3rem;">
  &copy; 2026 HowToCookAtHome. Nutrition data from <a href="https://fdc.nal.usda.gov/" style="color:var(--accent);">USDA FoodData Central</a>.
</footer>
</body></html>'''


def build_platform_pages(tenants, usda):
    """Build platform-level pages (restaurant directory, etc.) into output/web/."""
    web_dir = os.path.join(BASE_DIR, "output", "web")
    os.makedirs(web_dir, exist_ok=True)

    # Restaurant directory
    html = render_directory(tenants, usda)
    with open(os.path.join(web_dir, "restaurants.html"), "w") as outf:
        outf.write(html)

    return 1  # number of pages generated


# ============================================================
# BUILD (per tenant)
# ============================================================

def build_tenant(tenant, all_recipes, usda):
    """Build a complete site for one tenant."""
    slug = tenant["slug"]
    out_dir = os.path.join(OUTPUT_BASE, slug)
    os.makedirs(out_dir, exist_ok=True)

    # Load inventory
    inventory = load_inventory(slug)

    # Filter recipes for this tenant
    recipe_slugs = tenant.get("dishes") or tenant.get("recipes") or []
    if recipe_slugs:
        recipes = []
        for s in recipe_slugs:
            if s in all_recipes:
                r = all_recipes[s]
                if r.get("type") != "inventory":
                    recipes.append(r)
    else:
        # Fallback: platform/test-kitchen get all content recipes;
        # restaurant tenants get slug-prefixed recipes
        skip_types = {"inventory", "deprecated_invoice"}
        if tenant.get("type") in ("platform", "restaurant") and not tenant.get("inventory"):
            # Platform-level tenant: show all content recipes
            recipes = [r for r in all_recipes.values() if r.get("type") not in skip_types]
        else:
            # Restaurant tenant: show only their own recipes
            recipes = [r for r in all_recipes.values()
                       if r["slug"].startswith(slug + "-") and r.get("type") not in skip_types]

    # Render index
    html = render_index(tenant, recipes, usda, inventory)
    with open(os.path.join(out_dir, "index.html"), "w") as outf:
        outf.write(html)

    # Render each dish
    page_count = 1
    for r in recipes:
        html = render_dish_page(tenant, r, usda)
        with open(os.path.join(out_dir, f"r_{r['slug']}.html"), "w") as outf:
            outf.write(html)
        page_count += 1

    # Generate menu wireframe if we have the engine
    try:
        from app.menu import analyze_menu, render_menu_wireframe
        analysis = analyze_menu(slug)
        if analysis and not analysis.get("error") and analysis.get("items"):
            wireframe = render_menu_wireframe(analysis, tenant.get("colors"))
            with open(os.path.join(out_dir, "menu.html"), "w") as outf:
                outf.write(wireframe)
            page_count += 1
    except Exception:
        pass

    # Generate Menu Advisor page
    try:
        from app.menu import analyze_menu_gaps, render_advisor_page
        if analysis and not analysis.get("error"):
            gap_analysis = analyze_menu_gaps(slug)
            if gap_analysis and not gap_analysis.get("error"):
                advisor_html = render_advisor_page(tenant, analysis, gap_analysis)
                with open(os.path.join(out_dir, "advisor.html"), "w") as outf:
                    outf.write(advisor_html)
                page_count += 1
    except Exception:
        pass

    # Generate review report
    try:
        from app.review import review_tenant as run_review, render_review_page
        review_result = run_review(slug)
        if review_result and not review_result.get("error"):
            review_html = render_review_page(review_result)
            with open(os.path.join(out_dir, "review.html"), "w") as outf:
                outf.write(review_html)
            page_count += 1
    except Exception:
        pass

    # API: tenant health
    api_dir = os.path.join(out_dir, "api")
    os.makedirs(api_dir, exist_ok=True)
    with open(os.path.join(api_dir, "health.json"), "w") as outf:
        json.dump({
            "tenant": slug,
            "name": tenant["name"],
            "type": tenant.get("type", "partner"),
            "dishes": len(recipes),
            "has_inventory": inventory is not None,
            "usda": usda,
        }, outf, indent=2)

    return page_count


def main():
    print("\n  TENANT PUBLISHER")
    print("  ================\n")

    all_recipes = load_all_recipes()
    usda = get_usda_stats()
    tenants = list_tenants()

    print(f"  Shared data:")
    print(f"    Recipes:  {len(all_recipes)}")
    if usda:
        print(f"    USDA:     {usda['foods']:,} foods")
    print(f"    Tenants:  {len(tenants)}")
    print()

    if "--list" in sys.argv:
        for t in tenants:
            rc = len(t.get("recipes", []))
            print(f"    {t['slug']:25} {t['name']:30} ({t.get('type','partner')}, {rc} dishes)")
        return

    target = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else None

    # Build platform-level pages (directory, etc.)
    platform_pages = build_platform_pages(tenants, usda)
    print(f"  {'[platform]':25} → {platform_pages} pages (restaurant directory)")

    for tenant in tenants:
        if target and tenant["slug"] != target:
            continue
        count = build_tenant(tenant, all_recipes, usda)
        print(f"  {tenant['slug']:25} → {count} pages ({tenant['name']})")

    # --- Platform-level health.json (read by server.py on startup) ---
    api_dir = os.path.join(BASE_DIR, "output", "api")
    os.makedirs(api_dir, exist_ok=True)

    demo_tenants = [t for t in tenants if t.get("type") == "demo"]
    live_tenants = [t for t in tenants if t.get("type") in ("restaurant", "partner")]

    platform_health = {
        "status": "ok",
        "domain": "howtocookathome.com",
        "recipes": len(all_recipes),
        "tenants": len(tenants),
        "demo_sites": len(demo_tenants),
        "live_sites": len(live_tenants),
        "usda": usda or {},
    }
    with open(os.path.join(api_dir, "health.json"), "w") as outf:
        json.dump(platform_health, outf, indent=2)

    print(f"\n  Output: output/tenants/")
    print(f"  Open any: output/tenants/<slug>/index.html\n")


if __name__ == "__main__":
    main()
