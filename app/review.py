#!/usr/bin/env python3
"""
REVIEW_ENGINE.PY — Automated tenant audit + feedback system.

Runs a full diagnostic on a tenant's data and generates:
  1. Data quality score (0-100)
  2. Specific issues found
  3. Prioritized action items
  4. Comparison to platform benchmarks
  5. Revenue optimization opportunities

This replaces manual QA. Every tenant gets reviewed automatically
on onboarding and can re-run anytime.

Usage:
  python3 -m app.review                    # review all tenants
  python3 -m app.review smokehouse-joe     # review one tenant
"""
import yaml, json, os, sys, glob

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
CONTENT_DIR = os.path.join(BASE_DIR, "content")
TENANT_DIR = os.path.join(CONTENT_DIR, "tenants")
INV_DIR = os.path.join(CONTENT_DIR, "inventories")
RECIPE_DIR = os.path.join(CONTENT_DIR, "recipes")
OUTPUT_BASE = os.path.join(BASE_DIR, "output", "tenants")


# ============================================================
# REVIEW CHECKS — each returns (score 0-10, issues[], actions[])
# ============================================================

def check_inventory(tenant, inventory):
    """Does the tenant have a complete vendor inventory?"""
    score = 0
    issues = []
    actions = []

    if not inventory:
        return 0, ["No vendor inventory submitted."], ["Submit your vendor invoice or ingredient list to unlock cost analysis and menu optimization."]

    items = inventory.get("items", [])
    summary = inventory.get("summary", {})

    if not items:
        return 0, ["Inventory file exists but contains no items."], ["Re-submit your vendor invoice."]

    # Item count
    count = len(items)
    if count >= 12:
        score += 3
    elif count >= 6:
        score += 2
        issues.append(f"Only {count} inventory items. Most restaurants stock 15-30+ ingredients.")
        actions.append("Add more ingredients from your full vendor order to get a complete picture.")
    else:
        score += 1
        issues.append(f"Only {count} inventory items — too few for accurate analysis.")
        actions.append("Submit your complete vendor invoice for proper cost analysis.")

    # Pricing coverage
    priced = summary.get("priced_items", 0)
    if priced == count and count > 0:
        score += 3
        # Check for suspiciously round prices (might be estimates)
        round_prices = sum(1 for i in items if i.get("vendor_price") and i["vendor_price"] == round(i["vendor_price"]))
        if round_prices == count:
            issues.append("All prices are round numbers — are these estimates or actual vendor prices?")
            actions.append("Use exact prices from your actual invoice for accurate cost calculations.")
    elif priced > 0:
        score += 1
        missing = count - priced
        issues.append(f"{missing}/{count} ingredients have no pricing data.")
        actions.append(f"Add vendor prices for all {missing} unpriced items to unlock full profitability analysis.")
    else:
        issues.append("No pricing data on any ingredient.")
        actions.append("Submit a vendor invoice with prices (not just an ingredient list) for cost analysis.")

    # USDA match rate
    matched = summary.get("usda_matched", 0)
    match_rate = (matched / count * 100) if count > 0 else 0
    if match_rate >= 60:
        score += 2
    elif match_rate >= 30:
        score += 1
        issues.append(f"Only {matched}/{count} ingredients matched to USDA ({match_rate:.0f}%). Nutrition data is incomplete.")
        actions.append("Check spelling of unmatched ingredients — USDA uses formal names (e.g., 'Spices, paprika' not 'Paprika').")
    else:
        issues.append(f"Low USDA match rate: {matched}/{count} ({match_rate:.0f}%). Most nutrition data is missing.")
        actions.append("Many common ingredients aren't matching. We're working on improving our synonym database.")

    # Package size data
    has_pkg = sum(1 for i in items if i.get("package_size"))
    if has_pkg == count and count > 0:
        score += 2
    elif has_pkg > 0:
        score += 1
        issues.append(f"Only {has_pkg}/{count} items have package sizes. Unit pricing is incomplete.")
        actions.append("Include package sizes (e.g., '50lb', '1gal') from your invoice for accurate per-unit costs.")
    elif priced > 0:
        issues.append("No package size data — can't calculate unit prices (cost per lb, per oz, etc.).")
        actions.append("Add package sizes to convert bulk prices into per-unit costs.")

    return min(score, 10), issues, actions


def check_dishes(tenant, recipes):
    """Does the tenant have well-defined menu dishes?"""
    score = 0
    issues = []
    actions = []

    if not recipes:
        return 0, ["No menu dishes defined."], ["Submit your menu items or let us auto-generate dishes from your inventory."]

    dish_count = len(recipes)

    # Dish count
    if dish_count >= 6:
        score += 2
    elif dish_count >= 3:
        score += 1
        actions.append(f"Only {dish_count} dishes. Add more menu items for a complete menu engineering analysis.")
    else:
        issues.append(f"Only {dish_count} dish(es) — not enough for meaningful menu analysis.")
        actions.append("Add at least 4-6 menu items. Include appetizers, sides, and mains for a real menu layout.")

    # Dish quality checks
    has_steps = 0
    has_pricing = 0
    has_nutrition = 0
    has_flavor = 0
    auto_generated = 0

    for r in recipes:
        steps = r.get("steps", [])
        if steps and steps != ["(Add your preparation steps here)"] and len(steps) > 1:
            has_steps += 1

        mp = r.get("menu_pricing", {})
        if mp.get("food_cost") and mp["food_cost"] > 0:
            has_pricing += 1

        enrich = r.get("enrichment", {}).get("nutrition", {}).get("details", {})
        if enrich:
            has_nutrition += 1

        fl = r.get("flavor_profile", {}).get("scores", {})
        if fl:
            has_flavor += 1

        if r.get("source", {}).get("type") == "auto_generated":
            auto_generated += 1

    # Steps
    if has_steps == dish_count:
        score += 2
    elif has_steps > 0:
        score += 1
        missing = dish_count - has_steps
        issues.append(f"{missing}/{dish_count} dishes have no preparation steps.")
        actions.append("Add cooking instructions to your dishes — customers and staff need them.")
    else:
        issues.append("No dishes have preparation steps.")
        actions.append("Add cooking instructions. Even basic steps help with consistency and training.")

    # Pricing
    if has_pricing == dish_count:
        score += 2
    elif has_pricing > 0:
        score += 1
        issues.append(f"Only {has_pricing}/{dish_count} dishes have cost data.")
    else:
        issues.append("No dishes have food cost data. Menu engineering is impossible without costs.")
        actions.append("Submit vendor pricing to calculate per-dish food costs.")

    # Nutrition
    if has_nutrition > 0:
        score += 1

    # Flavor profiles
    if has_flavor > 0:
        score += 1

    # Auto-generated warning
    if auto_generated == dish_count and dish_count > 0:
        issues.append(f"All {dish_count} dishes were auto-generated from your inventory — they're estimates.")
        actions.append("Review and customize your auto-generated dishes. Adjust ingredient amounts, add real menu prices, and add your actual preparation steps.")
    elif auto_generated > 0:
        issues.append(f"{auto_generated}/{dish_count} dishes are auto-generated estimates.")
        actions.append("Refine auto-generated dishes with your actual recipes and menu prices.")

    return min(score, 10), issues, actions


def check_menu_engineering(tenant, recipes):
    """Is the menu optimized for profitability?"""
    score = 0
    issues = []
    actions = []

    priced_dishes = [r for r in recipes if r.get("menu_pricing", {}).get("food_cost", 0) > 0]

    if len(priced_dishes) < 2:
        return 0, ["Not enough priced dishes for menu engineering."], ["Add vendor pricing and at least 4 menu items for menu optimization."]

    # Check classification diversity
    try:
        from app.menu import analyze_menu
        analysis = analyze_menu(tenant["slug"])
        if analysis and not analysis.get("error"):
            summary = analysis.get("summary", {})
            stars = summary.get("stars", 0)
            plowhorses = summary.get("plowhorses", 0)
            puzzles = summary.get("puzzles", 0)
            dogs = summary.get("dogs", 0)

            if stars > 0:
                score += 3
            else:
                issues.append("No Star items (high margin + high popularity). Your menu has no clear money makers.")
                actions.append("Adjust pricing or reduce costs on your most popular item to create at least one Star.")

            if plowhorses > 0:
                score += 1
                actions.append(f"You have {plowhorses} Plowhorse(s) — popular but low margin. Raise their price $1-2 or reduce ingredient costs.")

            if puzzles > 0:
                score += 1
                actions.append(f"You have {puzzles} Puzzle(s) — good margin but undersold. Feature them with 'Chef's Pick' tags and better descriptions.")

            if dogs > 0:
                actions.append(f"You have {dogs} Dog(s) — low margin AND low popularity. Consider removing or reimagining them.")

            # Food cost check
            for item in analysis.get("items", []):
                fc_pct = item.get("food_cost_pct")
                if fc_pct and fc_pct > 35:
                    issues.append(f"'{item['title']}' has {fc_pct}% food cost (target: under 30%).")
                    actions.append(f"Reduce ingredient costs or raise menu price on '{item['title']}' — food cost is too high.")
                    break

            # Menu price reasonableness
            for item in analysis.get("items", []):
                if item.get("price_source") == "estimated_30pct":
                    issues.append("Menu prices are estimated (not your real prices). Analysis may not reflect reality.")
                    actions.append("Enter your actual menu prices for accurate profitability analysis.")
                    score -= 1
                    break

            if stars + plowhorses + puzzles > 0:
                score += 2  # has actionable data
    except Exception:
        pass

    return min(max(score, 0), 10), issues, actions


def check_flavor_profile(tenant, recipes):
    """Are flavor profiles well-balanced and backed by data?"""
    score = 0
    issues = []
    actions = []

    profiled = [r for r in recipes if r.get("flavor_profile", {}).get("scores")]
    if not profiled:
        return 0, ["No flavor profiles generated."], ["Submit ingredients with USDA-matchable names for flavor analysis."]

    score += 2  # has profiles

    # Check for profiles driven by USDA data vs just spice lookup
    usda_driven = 0
    for r in profiled:
        ings = r.get("ingredients", [])
        fdc_count = sum(1 for i in ings if i.get("usda_fdc_id"))
        if fdc_count >= 2:
            usda_driven += 1

    if usda_driven == len(profiled):
        score += 3
    elif usda_driven > 0:
        score += 1
        issues.append(f"Only {usda_driven}/{len(profiled)} dish profiles use USDA nutrient data. Others rely on spice category estimates.")
        actions.append("Improve ingredient naming to get better USDA matches — this makes flavor profiles more accurate.")
    else:
        issues.append("All flavor profiles are based on spice category estimates, not USDA nutrient data.")
        actions.append("Better USDA matching would give you science-backed flavor profiles instead of estimates.")

    # Check for monotone profiles (everything tastes the same)
    if len(profiled) >= 2:
        first_dims = set(profiled[0].get("flavor_profile", {}).get("scores", {}).keys())
        all_same = all(
            set(r.get("flavor_profile", {}).get("scores", {}).keys()) == first_dims
            for r in profiled[1:]
        )
        if all_same and len(profiled) >= 3:
            issues.append("All dishes have identical flavor dimension sets — your menu may lack variety.")
            actions.append("Add dishes with different ingredient profiles (e.g., a citrus-forward dish vs a smoky one) for menu diversity.")

    # Dimension count
    for r in profiled:
        dims = len(r.get("flavor_profile", {}).get("scores", {}))
        if dims >= 5:
            score += 1
            break
        elif dims <= 2:
            issues.append(f"'{r.get('title', '?')}' only has {dims} flavor dimensions — very one-note.")
            actions.append("Add more diverse ingredients (acids, aromatics, etc.) to create more complex flavor profiles.")
            break

    # Balance check — is one dimension dominating everything?
    for r in profiled:
        scores = r.get("flavor_profile", {}).get("scores", {})
        if scores:
            vals = list(scores.values())
            if max(vals) >= 9 and min(vals) <= 1 and len(vals) >= 4:
                score += 2  # well-differentiated
                break

    return min(score, 10), issues, actions


def check_blend_readiness(tenant):
    """Is this tenant part of any production-ready spice blends?"""
    score = 0
    issues = []
    actions = []

    try:
        from app.onboard import aggregate_demand_with_ratios
        data = aggregate_demand_with_ratios()
        slug = tenant["slug"]

        tenant_blends = [
            o for o in data.get("opportunities", [])
            if slug in o.get("tenants", []) and o.get("threshold_met")
        ]

        if tenant_blends:
            score += 5
            top = tenant_blends[0]
            ratio = top.get("tenant_ratios", {}).get(slug, {})
            source = ratio.get("ratio_source", "balanced")
            if source in ("recipe_amounts", "package_sizes"):
                score += 3
            elif source == "flavor_intensity":
                score += 1
                issues.append("Your blend ratios are estimated from flavor intensity — not your actual usage amounts.")
                actions.append("Add specific amounts to your recipes so we can calculate your exact blend ratios.")

            blend_count = len(tenant_blends)
            actions.append(f"You're matched in {blend_count} production-ready blend(s). Your custom ratios are being calculated.")
        else:
            issues.append("No production-ready blend matches yet.")
            actions.append("As more restaurants join, shared spice blends will be identified and offered at volume pricing.")

    except Exception:
        issues.append("Blend analysis unavailable.")

    return min(score, 10), issues, actions


def check_site_quality(tenant):
    """Is the generated site complete and navigable?"""
    score = 0
    issues = []
    actions = []

    slug = tenant["slug"]
    site_dir = os.path.join(OUTPUT_BASE, slug)

    if not os.path.exists(site_dir):
        return 0, ["No site generated."], ["Run the site builder to generate your branded pages."]

    files = glob.glob(os.path.join(site_dir, "*.html"))
    page_count = len(files)

    if page_count == 0:
        return 0, ["Site directory exists but has no HTML pages."], ["Rebuild your tenant site."]

    has_index = os.path.exists(os.path.join(site_dir, "index.html"))
    has_menu = os.path.exists(os.path.join(site_dir, "menu.html"))
    has_api = os.path.exists(os.path.join(site_dir, "api", "health.json"))
    dish_pages = [f for f in files if os.path.basename(f).startswith("r_")]

    if has_index:
        score += 2
    else:
        issues.append("Missing index.html — no homepage.")

    if has_menu:
        score += 2
    else:
        issues.append("No menu analysis page generated.")
        actions.append("Add vendor pricing to generate a menu engineering report.")

    if has_api:
        score += 1

    if dish_pages:
        score += 2
        if len(dish_pages) >= 4:
            score += 1
    else:
        issues.append("No individual dish pages.")
        actions.append("Add menu items to generate dish detail pages with nutrition and pricing.")

    # Check brand customization
    colors = tenant.get("colors", {})
    default_primary = "#1A1A2E"
    if colors.get("primary") != default_primary:
        score += 2
    else:
        issues.append("Using default brand colors — site looks generic.")
        actions.append("Customize your brand colors in the onboarding form to make your site look like yours.")

    return min(score, 10), issues, actions


# ============================================================
# OVERALL REVIEW
# ============================================================

def review_tenant(slug):
    """Run full automated review on a tenant. Returns structured report."""
    tenant_path = os.path.join(TENANT_DIR, f"{slug}.yaml")
    if not os.path.exists(tenant_path):
        return {"error": f"Tenant '{slug}' not found."}

    tenant = yaml.safe_load(open(tenant_path))
    if not tenant:
        return {"error": "Empty tenant config."}

    # Load inventory
    inv_ref = tenant.get("inventory")
    inventory = None
    if inv_ref:
        inv_path = os.path.join(INV_DIR, f"{inv_ref}.yaml")
        if os.path.exists(inv_path):
            inventory = yaml.safe_load(open(inv_path))

    # Load recipes
    recipes = []
    for rs in tenant.get("recipes", []):
        rp = os.path.join(RECIPE_DIR, f"{rs}.yaml")
        if os.path.exists(rp):
            r = yaml.safe_load(open(rp))
            if r:
                recipes.append(r)

    # Run all checks
    checks = {
        "inventory":        check_inventory(tenant, inventory),
        "dishes":           check_dishes(tenant, recipes),
        "menu_engineering":  check_menu_engineering(tenant, recipes),
        "flavor_profile":   check_flavor_profile(tenant, recipes),
        "blend_readiness":  check_blend_readiness(tenant),
        "site_quality":     check_site_quality(tenant),
    }

    # Calculate overall score
    total_score = 0
    total_max = 0
    all_issues = []
    all_actions = []
    check_results = {}

    # Weights: menu engineering and inventory matter most
    weights = {
        "inventory": 2.0,
        "dishes": 1.5,
        "menu_engineering": 2.0,
        "flavor_profile": 1.0,
        "blend_readiness": 1.0,
        "site_quality": 1.0,
    }

    for name, (score, issues, actions) in checks.items():
        w = weights.get(name, 1.0)
        total_score += score * w
        total_max += 10 * w
        all_issues.extend(issues)
        all_actions.extend(actions)
        check_results[name] = {
            "score": score,
            "max": 10,
            "weight": w,
            "weighted_score": round(score * w, 1),
            "issues": issues,
            "actions": actions,
            "grade": _grade(score),
        }

    overall_pct = round((total_score / total_max * 100) if total_max > 0 else 0)
    overall_grade = _grade_pct(overall_pct)

    # Prioritize actions: most impactful first
    priority_actions = _prioritize_actions(check_results)

    return {
        "tenant": slug,
        "tenant_name": tenant.get("name", slug),
        "overall_score": overall_pct,
        "overall_grade": overall_grade,
        "checks": check_results,
        "total_issues": len(all_issues),
        "total_actions": len(all_actions),
        "priority_actions": priority_actions,
        "summary": _generate_summary(tenant, overall_pct, check_results, priority_actions),
    }


def _grade(score):
    """Score (0-10) → letter grade."""
    if score >= 9: return "A"
    if score >= 7: return "B"
    if score >= 5: return "C"
    if score >= 3: return "D"
    return "F"


def _grade_pct(pct):
    if pct >= 90: return "A"
    if pct >= 75: return "B"
    if pct >= 60: return "C"
    if pct >= 40: return "D"
    return "F"


def _prioritize_actions(check_results):
    """Sort all actions by impact. Weighted checks with low scores get priority."""
    scored_actions = []
    for name, data in check_results.items():
        weight = data["weight"]
        score = data["score"]
        urgency = weight * (10 - score)  # higher weight + lower score = more urgent
        for action in data["actions"]:
            scored_actions.append({
                "action": action,
                "category": name,
                "urgency": round(urgency, 1),
                "current_grade": data.get("grade", _grade(data.get("score", 0))),
            })

    scored_actions.sort(key=lambda x: x["urgency"], reverse=True)
    return scored_actions[:8]  # Top 8 most impactful actions


def _generate_summary(tenant, overall_pct, checks, priority_actions):
    """Generate a human-readable summary."""
    name = tenant.get("name", tenant.get("slug", "Restaurant"))

    # Strengths
    strengths = []
    for cname, data in checks.items():
        if data["score"] >= 7:
            labels = {
                "inventory": "strong vendor inventory",
                "dishes": "well-defined menu dishes",
                "menu_engineering": "optimized menu pricing",
                "flavor_profile": "detailed flavor profiles",
                "blend_readiness": "matched with production blends",
                "site_quality": "complete branded site",
            }
            strengths.append(labels.get(cname, cname))

    # Weakest areas
    weakest = sorted(checks.items(), key=lambda x: x[1]["score"])[:2]
    weak_labels = {
        "inventory": "vendor inventory data",
        "dishes": "menu dish definitions",
        "menu_engineering": "menu profitability optimization",
        "flavor_profile": "flavor profile accuracy",
        "blend_readiness": "spice blend matching",
        "site_quality": "branded site completeness",
    }

    lines = [f"{name}: Overall Score {overall_pct}/100 (Grade: {_grade_pct(overall_pct)})"]

    if strengths:
        lines.append(f"Strengths: {', '.join(strengths)}.")

    if weakest and weakest[0][1]["score"] < 5:
        weak_names = [weak_labels.get(w[0], w[0]) for w in weakest if w[1]["score"] < 5]
        if weak_names:
            lines.append(f"Needs work: {', '.join(weak_names)}.")

    if priority_actions:
        lines.append(f"Top priority: {priority_actions[0]['action']}")

    return " ".join(lines)


# ============================================================
# REPORT RENDERING
# ============================================================

def render_review_page(review, tenant_colors=None):
    """Generate an HTML review report page using shared tenant CSS system."""
    from datetime import datetime
    try:
        from app.publish import _base_css, _nav_css, _card_css, _footer_css, _responsive_css, _nav_html, _footer_html
        has_shared = True
    except ImportError:
        has_shared = False

    c = tenant_colors or {
        "primary": "#1A1A2E", "accent": "#C4975A", "background": "#F5F0E8",
        "surface": "#FFFFFF", "text": "#333333", "text_light": "#666666", "success": "#2D6A4F",
    }
    f = {"heading": "'Lora', Georgia, 'Times New Roman', serif", "body": "'DM Sans', -apple-system, BlinkMacSystemFont, sans-serif"}

    score = review["overall_score"]
    grade = review["overall_grade"]
    name = review["tenant_name"]
    slug = review.get("slug", "")
    now = datetime.now()
    review_ts = now.strftime("%B %d, %Y at %I:%M %p")
    review_ver = now.strftime("v%Y.%m.%d")

    gc = {"A": "#2D6A4F", "B": "#1565c0", "C": "#ff9800", "D": "#f44336", "F": "#b71c1c"}.get(grade, "#666")

    # Build tenant dict for shared nav/footer helpers
    tenant_obj = {
        "name": name, "slug": slug,
        "logo_text": name,
        "colors": c, "fonts": f,
        "nav": [],
        "footer": {"copyright": name, "powered_by": "HowToCookAtHome", "powered_by_href": "/"},
    }

    # --- Check cards ---
    check_labels = {
        "inventory":        ("Vendor Inventory",   "INV"),
        "dishes":           ("Menu Dishes",        "DISH"),
        "menu_engineering": ("Menu Optimization",  "MENU"),
        "flavor_profile":   ("Flavor Profiles",    "FLVR"),
        "blend_readiness":  ("Blend Matching",     "BLND"),
        "site_quality":     ("Branded Site",       "SITE"),
    }

    check_html = ""
    for cname, data in review["checks"].items():
        label, icon = check_labels.get(cname, (cname, ""))
        g = data.get("grade", _grade(data.get("score", 0)))
        g_color = {"A":"#2D6A4F","B":"#1565c0","C":"#ff9800","D":"#f44336","F":"#b71c1c"}.get(g, "#666")
        bar_pct = data["score"] * 10

        details = ""
        for iss in data["issues"]:
            details += f'<div class="review-issue">&#x26A0;&#xFE0F; {iss}</div>'
        for act in data["actions"]:
            details += f'<div class="review-action">&#x2192; {act}</div>'

        check_html += f'''
    <div class="check-card">
      <div class="check-header">
        <span class="check-icon">{icon}</span>
        <h3>{label}</h3>
        <span class="check-grade" style="color:{g_color};">{g}</span>
      </div>
      <div class="progress-track">
        <div class="progress-fill" style="width:{bar_pct}%;background:{g_color};"></div>
      </div>
      <div class="check-meta">{data["score"]}/10 &middot; weight {data["weight"]}x</div>
      {details}
    </div>'''

    # --- Priority actions ---
    action_html = ""
    for i, pa in enumerate(review["priority_actions"]):
        cat_label = check_labels.get(pa["category"], (pa["category"],))[0]
        action_html += f'''
      <div class="action-row">
        <span class="action-num">{i+1}</span>
        <div class="action-body">
          <div class="action-text">{pa["action"]}</div>
          <div class="action-meta">{cat_label} &middot; Currently: {pa["current_grade"]}</div>
        </div>
      </div>'''

    # --- Scorecard sidebar stats ---
    checks = review["checks"]
    n_pass = sum(1 for d in checks.values() if d["score"] >= 7)
    n_warn = sum(1 for d in checks.values() if 4 <= d["score"] < 7)
    n_fail = sum(1 for d in checks.values() if d["score"] < 4)
    n_actions = len(review["priority_actions"])

    # Use shared CSS if available, else inline the essentials
    if has_shared:
        base_styles = _base_css(c, f) + _nav_css() + _card_css() + _footer_css() + _responsive_css()
        nav = _nav_html(tenant_obj)
        footer = _footer_html(tenant_obj)
    else:
        base_styles = f'''
:root {{ --primary:{c["primary"]}; --accent:{c["accent"]}; --bg:{c["background"]}; --surface:{c["surface"]}; --text:{c["text"]}; --text-light:{c["text_light"]}; --success:{c["success"]}; --heading:{f["heading"]}; --body:{f["body"]}; }}
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family:var(--body); color:var(--text); background:var(--bg); line-height:1.6; }}
a {{ color:var(--accent); text-decoration:none; }}
nav {{ background:var(--primary); padding:0.75rem 2rem; display:flex; justify-content:space-between; align-items:center; position:sticky; top:0; z-index:100; }}
nav .logo {{ font-family:var(--heading); color:var(--accent); font-size:1.3rem; font-weight:bold; text-decoration:none; }}
nav .links {{ display:flex; gap:1.5rem; }}
nav .links a {{ color:rgba(255,255,255,0.8); text-decoration:none; font-size:0.85rem; }}
footer {{ background:var(--primary); color:rgba(255,255,255,0.5); padding:1.5rem 2rem; text-align:center; font-size:0.75rem; margin-top:3rem; }}
footer a {{ color:var(--accent); }}
'''
        nav = f'''<nav><a href="index.html" class="logo">{name}</a><div class="links"><a href="index.html">Menu</a><a href="menu.html">Analysis</a><a href="review.html" style="color:var(--accent);">Review</a></div></nav>'''
        footer = f'<footer>&copy; 2026 {name} &middot; Review by HowToCookAtHome &middot; <a href="https://fdc.nal.usda.gov/">USDA FoodData Central</a></footer>'

    review_css = '''
/* ===== Review-specific styles ===== */
.review-container { max-width:1140px; margin:0 auto; padding:2.5rem; }
.review-layout { display:grid; grid-template-columns:320px 1fr; gap:2rem; align-items:start; }

/* Score sidebar — fixed on desktop */
.score-sidebar { position:sticky; top:80px; }
.score-ring-wrap { background:var(--surface); border-radius:16px; padding:2rem; text-align:center; box-shadow:0 2px 8px rgba(0,0,0,0.08); margin-bottom:1.5rem; }
.score-ring { width:140px; height:140px; border-radius:50%; border:8px solid rgba(0,0,0,0.06); display:flex; align-items:center; justify-content:center; margin:0 auto 1rem; position:relative; }
.score-num { font-size:2.8rem; font-weight:800; line-height:1; }
.score-label { font-size:0.8rem; color:var(--text-light); margin-top:2px; }
.score-grade { font-family:var(--heading); font-size:1.4rem; font-weight:700; margin-top:0.75rem; }
.score-version { font-size:0.7rem; color:var(--text-light); margin-top:1rem; padding-top:0.75rem; border-top:1px solid rgba(0,0,0,0.08); }

.stat-grid { display:grid; grid-template-columns:1fr 1fr; gap:0.75rem; }
.stat-box { background:var(--surface); border-radius:10px; padding:1rem; text-align:center; box-shadow:0 1px 3px rgba(0,0,0,0.06); }
.stat-num { font-size:1.4rem; font-weight:700; }
.stat-label { font-size:0.7rem; color:var(--text-light); text-transform:uppercase; letter-spacing:0.05em; margin-top:0.2rem; }

/* Main content */
.review-main { min-width:0; }
.section-title { font-family:var(--heading); color:var(--primary); font-size:1.15rem; margin-bottom:1rem; padding-bottom:0.5rem; border-bottom:2px solid var(--accent); }

/* Actions card */
.actions-card { background:var(--surface); border-radius:12px; padding:1.5rem 1.75rem; margin-bottom:2rem; box-shadow:0 2px 8px rgba(0,0,0,0.06); border-left:4px solid var(--accent); }
.action-row { display:flex; gap:0.75rem; padding:0.75rem 0; border-bottom:1px solid rgba(0,0,0,0.05); }
.action-row:last-child { border-bottom:none; }
.action-num { flex-shrink:0; width:30px; height:30px; border-radius:50%; background:var(--accent); color:white; display:flex; align-items:center; justify-content:center; font-size:0.8rem; font-weight:700; }
.action-body { flex:1; min-width:0; }
.action-text { font-weight:600; font-size:0.9rem; line-height:1.4; }
.action-meta { font-size:0.75rem; color:var(--text-light); margin-top:0.15rem; }

/* Check cards — 2-col on desktop */
.checks-grid { display:grid; grid-template-columns:1fr 1fr; gap:1rem; }
.check-card { background:var(--surface); border-radius:12px; padding:1.25rem 1.5rem; box-shadow:0 1px 4px rgba(0,0,0,0.06); border:1px solid rgba(0,0,0,0.06); }
.check-header { display:flex; align-items:center; gap:0.5rem; margin-bottom:0.75rem; }
.check-header h3 { flex:1; font-size:0.95rem; font-family:var(--heading); color:var(--primary); margin:0; }
.check-icon { font-size:0.6rem; font-weight:700; letter-spacing:0.05em; background:var(--primary); color:rgba(255,255,255,0.7); padding:0.2rem 0.4rem; border-radius:4px; }
.check-grade { font-size:1.3rem; font-weight:800; }
.progress-track { height:6px; background:rgba(0,0,0,0.06); border-radius:3px; margin-bottom:0.6rem; overflow:hidden; }
.progress-fill { height:100%; border-radius:3px; transition:width 0.4s ease; }
.check-meta { font-size:0.75rem; color:var(--text-light); margin-bottom:0.5rem; }
.review-issue { padding:0.25rem 0; font-size:0.8rem; color:#c62828; line-height:1.4; }
.review-action { padding:0.25rem 0; font-size:0.8rem; color:var(--success); line-height:1.4; }

/* Responsive */
@media(max-width:960px) {
  .review-layout { grid-template-columns:1fr; }
  .score-sidebar { position:static; display:grid; grid-template-columns:1fr 1fr; gap:1.5rem; }
  .stat-grid { grid-template-columns:repeat(4,1fr); }
}
@media(max-width:600px) {
  .review-container { padding:1.25rem; }
  .score-sidebar { grid-template-columns:1fr; }
  .checks-grid { grid-template-columns:1fr; }
  .stat-grid { grid-template-columns:1fr 1fr; }
}
'''

    html = f'''<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{name} — Platform Review</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin><link href="https://fonts.googleapis.com/css2?family=DM+Sans:ital,opsz,wght@0,9..40,300;0,9..40,400;0,9..40,500;0,9..40,600;0,9..40,700;1,9..40,400&family=Lora:ital,wght@0,400;0,500;0,600;0,700;1,400&display=swap" rel="stylesheet">
<style>
{base_styles}
{review_css}
</style>
</head><body>
{nav}

<div class="review-container">
  <div class="review-layout">

    <!-- LEFT: Score sidebar -->
    <aside class="score-sidebar">
      <div class="score-ring-wrap">
        <div class="score-ring" style="border-color:{gc};">
          <div>
            <div class="score-num" style="color:{gc};">{score}</div>
            <div class="score-label">out of 100</div>
          </div>
        </div>
        <div class="score-grade" style="color:{gc};">Grade: {grade}</div>
        <div class="score-version">
          {review_ver} &middot; {review_ts}
        </div>
      </div>

      <div class="stat-grid">
        <div class="stat-box">
          <div class="stat-num" style="color:#2D6A4F;">{n_pass}</div>
          <div class="stat-label">Passing</div>
        </div>
        <div class="stat-box">
          <div class="stat-num" style="color:#ff9800;">{n_warn}</div>
          <div class="stat-label">Warning</div>
        </div>
        <div class="stat-box">
          <div class="stat-num" style="color:#f44336;">{n_fail}</div>
          <div class="stat-label">Failing</div>
        </div>
        <div class="stat-box">
          <div class="stat-num" style="color:var(--accent);">{n_actions}</div>
          <div class="stat-label">Actions</div>
        </div>
      </div>
    </aside>

    <!-- RIGHT: Content -->
    <div class="review-main">
      <h1 class="section-title">Platform Review</h1>
      <p style="margin-bottom:2rem;color:var(--text-light);font-size:0.92rem;line-height:1.6;">{review["summary"]}</p>

      <div class="actions-card">
        <h2 style="font-family:var(--heading);color:var(--primary);font-size:1.05rem;margin-bottom:0.75rem;">Priority Actions</h2>
        {action_html if action_html else '<div style="color:var(--text-light);font-style:italic;">Looking good! No urgent actions needed.</div>'}
      </div>

      <h2 class="section-title">Detailed Scores</h2>
      <div class="checks-grid">
        {check_html}
      </div>
    </div>

  </div>
</div>

{footer}
</body></html>'''

    return html


# ============================================================
# CLI + INTEGRATION
# ============================================================

def review_all():
    """Review all tenants and return results."""
    results = {}
    for tf in sorted(glob.glob(os.path.join(TENANT_DIR, "*.yaml"))):
        tenant = yaml.safe_load(open(tf))
        if not tenant or tenant.get("type") == "platform":
            continue
        slug = tenant.get("slug", "")
        results[slug] = review_tenant(slug)
    return results


def main():
    print("\n  TENANT REVIEW ENGINE")
    print("  ====================\n")

    target = sys.argv[1] if len(sys.argv) > 1 else None

    if target:
        review = review_tenant(target)
        if review.get("error"):
            print(f"  Error: {review['error']}")
            return
        _print_review(review)

        # Generate HTML
        tenant = yaml.safe_load(open(os.path.join(TENANT_DIR, f"{target}.yaml")))
        html = render_review_page(review, tenant.get("colors"))
        out_dir = os.path.join(OUTPUT_BASE, target)
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, "review.html"), "w") as f:
            f.write(html)
        print(f"\n  Report: output/tenants/{target}/review.html")
    else:
        results = review_all()
        for slug, review in sorted(results.items(), key=lambda x: x[1].get("overall_score", 0), reverse=True):
            _print_review(review)

            # Generate HTML for each
            tenant = yaml.safe_load(open(os.path.join(TENANT_DIR, f"{slug}.yaml")))
            html = render_review_page(review, tenant.get("colors"))
            out_dir = os.path.join(OUTPUT_BASE, slug)
            os.makedirs(out_dir, exist_ok=True)
            with open(os.path.join(out_dir, "review.html"), "w") as f:
                f.write(html)

        print(f"\n  Reports generated for {len(results)} tenants.")


def _print_review(review):
    """Print a review to console."""
    name = review["tenant_name"]
    score = review["overall_score"]
    grade = review["overall_grade"]
    print(f"  {name:25s}  {score:3d}/100  Grade: {grade}")
    for check_name, data in review["checks"].items():
        print(f"    {check_name:20s}  {data['score']:2d}/10 ({data['grade']})")
    if review["priority_actions"]:
        print(f"    Top action: {review['priority_actions'][0]['action'][:80]}")
    print()


if __name__ == "__main__":
    main()
