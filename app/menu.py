#!/usr/bin/env python3
"""
MENU_ENGINE.PY — Menu engineering + layout optimization.

Takes recipe cost data from onboarding and produces:
  1. Profitability classification per dish (Star / Plowhorse / Puzzle / Dog)
  2. Strategic placement recommendations
  3. HTML menu wireframe with items positioned for max revenue

This is the "menu consultant in a box" piece of HTCAH.

Menu engineering 101:
  - Food cost % = ingredient cost / menu price
  - Contribution margin = menu price - ingredient cost (the actual dollars you keep)
  - Classification uses a 2x2: margin (high/low) × popularity (high/low)
    • Star:      high margin, high popularity → FEATURE (box it, top-right)
    • Plowhorse: low margin,  high popularity → REPRICE (raise price or cut cost)
    • Puzzle:    high margin, low popularity  → PROMOTE (draw attention)
    • Dog:       low margin,  low popularity  → RETHINK (remove or reimagine)

Without real sales data, we estimate popularity from:
  - Flavor score (higher = more appealing)
  - Ingredient count (simpler dishes often sell more)
  - Cuisine familiarity

The restaurant can override with their actual sales numbers later.
"""
import os
import sys
import yaml

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
RECIPE_DIR = os.path.join(BASE_DIR, "content", "recipes")
TENANT_DIR = os.path.join(BASE_DIR, "content", "tenants")


# ============================================================
# MENU ZONES — where items go on a physical/digital menu
# Based on eye-tracking research:
#   Zone A (top-right):  First place eyes land → Stars
#   Zone B (top-center): Second scan → Puzzles (need visibility)
#   Zone C (center):     Comfortable default → Plowhorses
#   Zone D (bottom):     Last glance → Dogs (or remove entirely)
# ============================================================
ZONE_MAP = {
    "star":      {"zone": "A", "position": "top-right",    "style": "boxed",     "label": "Featured"},
    "puzzle":    {"zone": "B", "position": "top-center",   "style": "highlight", "label": "Chef's Pick"},
    "plowhorse": {"zone": "C", "position": "center",       "style": "normal",    "label": None},
    "dog":       {"zone": "D", "position": "bottom",       "style": "minimal",   "label": None},
}


def classify_menu_item(ingredient_cost, menu_price, popularity_score,
                       margin_threshold=0.65, popularity_threshold=50):
    """Classify a menu item into Star/Plowhorse/Puzzle/Dog.

    Args:
        ingredient_cost: Total cost of ingredients (from vendor data)
        menu_price: What the restaurant charges (they provide this, or we estimate)
        popularity_score: 0-100 estimated or actual popularity
        margin_threshold: Contribution margin % cutoff for "high" (default 65%)
        popularity_threshold: Score cutoff for "high popularity" (default 50)

    Returns:
        dict with classification, margin data, and placement recommendation.
    """
    if menu_price <= 0:
        return {"classification": "unknown", "reason": "no menu price"}

    food_cost_pct = ingredient_cost / menu_price
    contribution_margin = menu_price - ingredient_cost
    margin_pct = contribution_margin / menu_price

    high_margin = margin_pct >= margin_threshold
    high_popularity = popularity_score >= popularity_threshold

    if high_margin and high_popularity:
        classification = "star"
    elif not high_margin and high_popularity:
        classification = "plowhorse"
    elif high_margin and not high_popularity:
        classification = "puzzle"
    else:
        classification = "dog"

    zone = ZONE_MAP[classification]

    return {
        "classification": classification,
        "food_cost_pct": round(food_cost_pct * 100, 1),
        "contribution_margin": round(contribution_margin, 2),
        "margin_pct": round(margin_pct * 100, 1),
        "popularity_score": popularity_score,
        "placement": zone,
        "action": {
            "star": "Feature prominently. Box it. Top-right position. This is your money maker.",
            "plowhorse": "Popular but thin margin. Raise price $1-2 or reduce portion/ingredient cost.",
            "puzzle": "Good margin but undersold. Move it up, add a 'Chef's Pick' tag, describe it better.",
            "dog": "Low margin, low demand. Remove, reimagine, or replace with a Star candidate.",
        }[classification],
    }


def estimate_popularity(recipe, spice_analysis=None):
    """Estimate a dish's popularity score (0-100) without sales data.

    Uses signals we DO have:
      - Flavor score: higher intensity → more memorable → likely more popular
      - Ingredient simplicity: 4-8 ingredients is the sweet spot
      - Spice variety: more flavor dimensions → broader appeal
    """
    score = 50  # baseline

    ingredients = recipe.get("ingredients", [])
    ing_count = len(ingredients)

    # Simplicity bonus: 4-8 ingredients is the sweet spot
    if 4 <= ing_count <= 8:
        score += 10
    elif ing_count <= 3:
        score += 5  # too simple might be boring
    elif ing_count > 12:
        score -= 10  # complex = niche

    # Flavor profile bonus
    if spice_analysis and spice_analysis.get("spices"):
        spice_count = len(spice_analysis["spices"])
        if spice_count >= 3:
            score += 15  # well-seasoned
        elif spice_count >= 1:
            score += 5

        # Flavor dimension diversity
        agg = spice_analysis.get("flavor_profile_aggregate", {})
        if len(agg) >= 4:
            score += 10  # hits multiple taste receptors
        elif len(agg) >= 2:
            score += 5

        # High intensity spices = memorable
        max_intensity = max((s.get("flavor_intensity", 0) for s in spice_analysis["spices"]), default=0)
        if max_intensity >= 7:
            score += 5

    return min(max(score, 0), 100)


def estimate_menu_price(ingredient_cost, food_cost_target=0.30):
    """Estimate a menu price from ingredient cost.

    Industry standard: food cost should be 28-35% of menu price.
    Default target: 30% (so menu price = ingredient cost / 0.30).

    Restaurant can override with their actual prices.
    """
    if ingredient_cost <= 0:
        return 0
    return round(ingredient_cost / food_cost_target, 2)


def analyze_menu(tenant_slug, menu_prices=None):
    """Full menu engineering analysis for a tenant.

    Args:
        tenant_slug: The tenant to analyze
        menu_prices: Optional dict of {recipe_slug: actual_menu_price}
                     If not provided, we estimate from ingredient costs.

    Returns:
        Complete menu analysis with classifications and layout recommendations.
    """
    menu_prices = menu_prices or {}

    # Load tenant
    tenant_path = os.path.join(TENANT_DIR, f"{tenant_slug}.yaml")
    if not os.path.exists(tenant_path):
        return {"error": f"Tenant {tenant_slug} not found"}

    tenant = yaml.safe_load(open(tenant_path))
    if not tenant:
        return {"error": "Empty tenant config"}

    # Import spice analysis
    from app.onboard import analyze_spice_value, SPICE_CATEGORIES

    items = []
    for recipe_slug in (tenant.get("dishes") or tenant.get("recipes") or []):
        recipe_path = os.path.join(RECIPE_DIR, f"{recipe_slug}.yaml")
        if not os.path.exists(recipe_path):
            continue

        recipe = yaml.safe_load(open(recipe_path))
        if not recipe:
            continue

        # Skip inventory files (they're not dishes)
        if recipe.get("type") == "inventory":
            continue

        ingredients = recipe.get("ingredients", [])

        # Get ingredient / food cost from the RIGHT source
        # New dishes have menu_pricing.food_cost (calculated per-dish)
        # Old recipes have raw vendor_price on each ingredient (the whole invoice)
        menu_pricing = recipe.get("menu_pricing", {})

        if menu_pricing.get("food_cost") is not None:
            # New format: dish with per-dish cost
            ingredient_cost = menu_pricing["food_cost"]
        else:
            # Old format: sum up item_cost or vendor_price per ingredient
            ingredient_cost = sum(
                ing.get("item_cost", 0) or ing.get("vendor_price", 0) or 0
                for ing in ingredients
            )

        # Get or estimate menu price
        if recipe_slug in menu_prices:
            menu_price = menu_prices[recipe_slug]
            price_source = "provided"
        elif menu_pricing.get("menu_price"):
            menu_price = menu_pricing["menu_price"]
            price_source = "auto_estimated"
        elif ingredient_cost > 0:
            menu_price = estimate_menu_price(ingredient_cost)
            price_source = "estimated_30pct"
        else:
            menu_price = 0
            price_source = "none"

        # Get spice analysis for popularity estimation
        spice_data = analyze_spice_value(ingredients)
        popularity = estimate_popularity(recipe, spice_data)

        # Classify
        if menu_price > 0 and ingredient_cost > 0:
            classification = classify_menu_item(ingredient_cost, menu_price, popularity)
        else:
            classification = {
                "classification": "unpriced",
                "food_cost_pct": None,
                "contribution_margin": None,
                "margin_pct": None,
                "popularity_score": popularity,
                "placement": ZONE_MAP.get("plowhorse"),  # default center
                "action": "Add vendor pricing to unlock profitability analysis.",
            }

        items.append({
            "recipe_slug": recipe_slug,
            "title": recipe.get("title", recipe_slug),
            "ingredient_cost": round(ingredient_cost, 2),
            "menu_price": round(menu_price, 2),
            "price_source": price_source,
            "ingredient_count": len(ingredients),
            **classification,
        })

    # Sort: Stars first, then Puzzles, Plowhorses, Dogs
    priority = {"star": 0, "puzzle": 1, "plowhorse": 2, "dog": 3, "unpriced": 4, "unknown": 5}
    items.sort(key=lambda x: (priority.get(x["classification"], 5), -(x.get("contribution_margin") or 0)))

    # Summary stats
    stars = [i for i in items if i["classification"] == "star"]
    plowhorses = [i for i in items if i["classification"] == "plowhorse"]
    puzzles = [i for i in items if i["classification"] == "puzzle"]
    dogs = [i for i in items if i["classification"] == "dog"]

    return {
        "tenant": tenant_slug,
        "tenant_name": tenant.get("name", tenant_slug),
        "item_count": len(items),
        "items": items,
        "summary": {
            "stars": len(stars),
            "plowhorses": len(plowhorses),
            "puzzles": len(puzzles),
            "dogs": len(dogs),
            "unpriced": len([i for i in items if i["classification"] == "unpriced"]),
        },
        "recommendations": _generate_recommendations(items, stars, plowhorses, puzzles, dogs),
    }


def _generate_recommendations(items, stars, plowhorses, puzzles, dogs):
    """Generate actionable menu recommendations."""
    recs = []

    if not stars and items:
        recs.append({
            "priority": "high",
            "type": "no_stars",
            "message": "No Star items detected. Consider adjusting pricing or reducing ingredient costs on your most popular dishes.",
        })

    if stars:
        names = ", ".join(i["title"] for i in stars[:3])
        recs.append({
            "priority": "action",
            "type": "feature_stars",
            "message": f"Feature these prominently (box, bold, top-right): {names}",
        })

    if plowhorses:
        for ph in plowhorses[:2]:
            gap = round((0.65 - (ph.get("margin_pct", 0) / 100)) * ph["menu_price"], 2)
            if gap > 0:
                recs.append({
                    "priority": "medium",
                    "type": "reprice_plowhorse",
                    "message": f"'{ph['title']}' is popular but thin margin ({ph.get('margin_pct', '?')}%). Raise price ~${gap:.2f} or reduce ingredient cost to hit 65% margin.",
                    "item": ph["recipe_slug"],
                })

    if puzzles:
        names = ", ".join(i["title"] for i in puzzles[:3])
        recs.append({
            "priority": "medium",
            "type": "promote_puzzles",
            "message": f"Good margin but undersold — add 'Chef's Pick' tags and better descriptions: {names}",
        })

    if dogs:
        names = ", ".join(i["title"] for i in dogs[:3])
        recs.append({
            "priority": "low",
            "type": "rethink_dogs",
            "message": f"Consider removing or reimagining: {names}",
        })

    return recs


def render_menu_wireframe(analysis, tenant_colors=None):
    """Generate an HTML menu wireframe with strategic item placement.

    This is the visual output — shows the restaurant exactly where
    each item should go on their menu, with styling recommendations.
    """
    colors = tenant_colors or {
        "primary": "#1A1A2E",
        "accent": "#C4975A",
        "background": "#F5F0E8",
        "surface": "#FFFFFF",
        "text": "#333333",
        "text_light": "#666666",
        "success": "#2D6A4F",
    }

    items = analysis.get("items", [])
    tenant_name = analysis.get("tenant_name", "Restaurant")
    recs = analysis.get("recommendations", [])

    # Group by classification
    by_class = {}
    for item in items:
        c = item["classification"]
        by_class.setdefault(c, []).append(item)

    def item_card(item):
        c = item["classification"]
        placement = item.get("placement", {})
        style_type = placement.get("style", "normal")
        label = placement.get("label")

        border = {
            "boxed": f"3px solid {colors['accent']}",
            "highlight": f"2px dashed {colors['success']}",
            "normal": f"1px solid #ddd",
            "minimal": f"1px solid #eee",
        }.get(style_type, "1px solid #ddd")

        bg = {
            "boxed": f"{colors['accent']}11",
            "highlight": f"{colors['success']}11",
            "normal": colors["surface"],
            "minimal": "#fafafa",
        }.get(style_type, colors["surface"])

        badge_colors = {
            "star": (colors["accent"], "#fff"),
            "puzzle": (colors["success"], "#fff"),
            "plowhorse": ("#888", "#fff"),
            "dog": ("#ccc", "#666"),
        }
        badge_bg, badge_fg = badge_colors.get(c, ("#eee", "#333"))

        margin_display = ""
        if item.get("margin_pct") is not None:
            margin_display = f'<span style="color:{colors["text_light"]};font-size:0.8em;">{item["margin_pct"]}% margin</span>'

        price_display = ""
        if item.get("menu_price") and item["menu_price"] > 0:
            est = " (est)" if item.get("price_source") == "estimated_30pct" else ""
            price_display = f'<span style="font-weight:bold;color:{colors["primary"]}">${item["menu_price"]:.2f}{est}</span>'

        cost_display = ""
        if item.get("ingredient_cost") and item["ingredient_cost"] > 0:
            cost_display = f'<span style="color:{colors["text_light"]};font-size:0.8em;">Cost: ${item["ingredient_cost"]:.2f}</span>'

        label_html = ""
        if label:
            label_html = f'<span style="background:{colors["accent"]};color:#fff;padding:2px 8px;border-radius:3px;font-size:0.75em;font-weight:bold;text-transform:uppercase;">{label}</span>'

        return f'''<div style="border:{border};border-radius:8px;padding:16px;background:{bg};position:relative;">
            <div style="display:flex;justify-content:space-between;align-items:start;margin-bottom:8px;">
                <div>
                    {label_html}
                    <h3 style="margin:4px 0 2px 0;color:{colors['primary']};font-size:1.1em;">{item["title"]}</h3>
                </div>
                {price_display}
            </div>
            <div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap;">
                <span style="background:{badge_bg};color:{badge_fg};padding:2px 8px;border-radius:12px;font-size:0.75em;font-weight:bold;text-transform:uppercase;">{c}</span>
                {cost_display}
                {margin_display}
                <span style="color:{colors['text_light']};font-size:0.8em;">Pop: {item.get('popularity_score', '?')}/100</span>
            </div>
        </div>'''

    # Build zones
    zone_a = "".join(item_card(i) for i in by_class.get("star", []))
    zone_b = "".join(item_card(i) for i in by_class.get("puzzle", []))
    zone_c = "".join(item_card(i) for i in by_class.get("plowhorse", []))
    zone_d = "".join(item_card(i) for i in by_class.get("dog", []))
    zone_u = "".join(item_card(i) for i in by_class.get("unpriced", []))

    # Recommendations HTML
    rec_html = ""
    if recs:
        rec_items = ""
        for r in recs:
            icon = {"high": "!!", "action": "->", "medium": "*", "low": "-"}.get(r["priority"], "*")
            rec_items += f'<li style="margin-bottom:8px;"><strong>{icon}</strong> {r["message"]}</li>'
        rec_html = f'''<div style="background:#fff3cd;border:1px solid #ffc107;border-radius:8px;padding:16px;margin-bottom:24px;">
            <h3 style="margin:0 0 8px 0;color:#856404;">Menu Optimization Recommendations</h3>
            <ul style="margin:0;padding-left:20px;color:#856404;">{rec_items}</ul>
        </div>'''

    summary = analysis.get("summary", {})

    html = f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{tenant_name} — Menu Engineering Report</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin><link href="https://fonts.googleapis.com/css2?family=DM+Sans:ital,opsz,wght@0,9..40,300;0,9..40,400;0,9..40,500;0,9..40,600;0,9..40,700;1,9..40,400&family=Lora:ital,wght@0,400;0,500;0,600;0,700;1,400&display=swap" rel="stylesheet">
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: 'DM Sans', -apple-system, BlinkMacSystemFont, sans-serif; background: {colors["background"]}; color: {colors["text"]}; padding: 24px; max-width: 960px; margin: 0 auto; line-height: 1.7; font-size: 15px; -webkit-font-smoothing: antialiased; }}
  .header {{ text-align: center; margin-bottom: 32px; padding-bottom: 16px; border-bottom: 2px solid {colors["accent"]}; }}
  .header h1 {{ color: {colors["primary"]}; font-family: 'Lora', Georgia, serif; margin-bottom: 4px; letter-spacing: -0.02em; }}
  .header p {{ color: {colors["text_light"]}; }}
  .stats {{ display: flex; gap: 16px; justify-content: center; margin: 16px 0; flex-wrap: wrap; }}
  .stat {{ background: {colors["surface"]}; padding: 12px 20px; border-radius: 16px; text-align: center; min-width: 100px; box-shadow: 0 1px 2px rgba(0,0,0,0.04), 0 1px 4px rgba(0,0,0,0.03); }}
  .stat .num {{ font-size: 1.5em; font-weight: bold; color: {colors["accent"]}; }}
  .stat .lbl {{ font-size: 0.8em; color: {colors["text_light"]}; }}
  .zone {{ margin-bottom: 24px; }}
  .zone h2 {{ font-size: 1em; color: {colors["text_light"]}; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 12px; padding-bottom: 4px; border-bottom: 1px solid #ddd; }}
  .zone-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 12px; }}
  .wireframe {{ background: {colors["surface"]}; border: 2px solid {colors["primary"]}; border-radius: 12px; padding: 24px; margin-top: 32px; }}
  .wireframe h2 {{ text-align: center; color: {colors["primary"]}; margin-bottom: 16px; font-family: Georgia, serif; }}
  .wireframe-label {{ font-size: 0.7em; color: {colors["text_light"]}; text-transform: uppercase; letter-spacing: 1px; }}
  .powered {{ text-align: center; margin-top: 32px; padding-top: 16px; border-top: 1px solid #ddd; color: {colors["text_light"]}; font-size: 0.8em; }}
</style>
</head>
<body>
<div class="header">
    <h1>{tenant_name}</h1>
    <p>Menu Engineering Report — Powered by HowToCookAtHome</p>
    <div class="stats">
        <div class="stat"><div class="num">{summary.get("stars", 0)}</div><div class="lbl">Stars</div></div>
        <div class="stat"><div class="num">{summary.get("puzzles", 0)}</div><div class="lbl">Puzzles</div></div>
        <div class="stat"><div class="num">{summary.get("plowhorses", 0)}</div><div class="lbl">Plowhorses</div></div>
        <div class="stat"><div class="num">{summary.get("dogs", 0)}</div><div class="lbl">Dogs</div></div>
    </div>
</div>

{rec_html}

<div class="wireframe">
    <h2>Recommended Menu Layout</h2>
    <p style="text-align:center;color:{colors['text_light']};margin-bottom:20px;font-size:0.9em;">
        Items positioned by profitability research. Zone A (top-right) is where eyes land first.
    </p>

    {"" if not zone_a else f'<div class="zone"><h2>Zone A — Feature These (Top-Right)</h2><div class="zone-grid">{zone_a}</div></div>'}
    {"" if not zone_b else f'<div class="zone"><h2>Zone B — Promote These (Top-Center)</h2><div class="zone-grid">{zone_b}</div></div>'}
    {"" if not zone_c else f'<div class="zone"><h2>Zone C — Steady Sellers (Center)</h2><div class="zone-grid">{zone_c}</div></div>'}
    {"" if not zone_d else f'<div class="zone"><h2>Zone D — Rethink These (Bottom)</h2><div class="zone-grid">{zone_d}</div></div>'}
    {"" if not zone_u else f'<div class="zone"><h2>Needs Pricing Data</h2><div class="zone-grid">{zone_u}</div></div>'}
</div>

<div class="powered">
    <p>Menu analysis by HowToCookAtHome &middot; USDA FoodData Central &middot; Privacy-first, no cookies</p>
    <p style="margin-top:4px;">Add vendor invoices with pricing to unlock full profitability analysis.</p>
</div>
</body>
</html>'''

    return html


# ============================================================
# MENU ADVISOR — "What should I add / change / drop?"
# ============================================================

# Ideal menu structure by restaurant type
MENU_BLUEPRINTS = {
    "burger": {
        "categories": {
            "Signature Burgers": {"min": 3, "max": 6, "role": "Stars — your identity. 3-5 is the sweet spot."},
            "Sides": {"min": 2, "max": 4, "role": "High margin, low effort. Fries, onion rings, slaw."},
            "Drinks / Desserts": {"min": 1, "max": 3, "role": "Impulse add-ons. Shakes, custard, lemonade."},
        },
        "ideal_count": "6-10 items total",
        "pricing_advice": "Anchor with a premium double ($14-18), cluster core burgers $9-12, sides $4-6.",
        "missing_signals": {
            "no_premium": ("No premium/double burger", "Add a loaded double or specialty burger at $14-18 to anchor pricing. Everything else looks cheaper by comparison."),
            "no_sides": ("No side items", "Add fries or a simple side. Sides are 80%+ margin and increase average ticket by $3-5."),
            "no_dessert": ("No dessert or drink", "A shake, custard, or simple dessert is pure margin and increases dwell time."),
            "too_few": ("Only {n} items — menu feels thin", "Customers need 3-5 burger options to feel they have a real choice without feeling overwhelmed."),
            "too_many": ("{n} items — menu is too big", "Trim to 8-10 items max. Large menus increase food waste, slow prep, and confuse customers."),
        },
    },
    "bbq": {
        "categories": {
            "Smoked Meats": {"min": 2, "max": 5, "role": "Core menu. Brisket and pulled pork are non-negotiable."},
            "Combo Plates": {"min": 1, "max": 2, "role": "Highest ticket item. Let people try multiple meats."},
            "Sides": {"min": 3, "max": 5, "role": "Coleslaw, beans, mac & cheese, cornbread. Cheap to make, high margin."},
            "Sandwiches": {"min": 1, "max": 2, "role": "Lower price entry point. Pulls in lunch crowd."},
        },
        "ideal_count": "8-14 items total",
        "pricing_advice": "Combo plates anchor at $18-24. Individual meats $12-18. Sandwiches $8-12. Sides $3-5.",
        "missing_signals": {
            "no_brisket": ("No brisket on menu", "Brisket is the flagship of any BBQ joint. Even if it's expensive, it sets the tone."),
            "no_sides": ("No dedicated sides", "BBQ without sides feels incomplete. Coleslaw, beans, and cornbread are dirt cheap to make."),
            "no_combo": ("No combo/sampler plate", "A two or three-meat plate is usually the highest-ticket item and the most ordered."),
            "no_sandwich": ("No sandwich option", "A pulled pork sandwich at $8-10 is an easy lunch entry point that brings in weekday traffic."),
        },
    },
    "pizza": {
        "categories": {
            "Pizzas": {"min": 3, "max": 8, "role": "Core menu. Start with Margherita (your benchmark) plus 3-5 specialties."},
            "Appetizers": {"min": 2, "max": 4, "role": "Bruschetta, garlic bread, salad. Fills the wait and boosts ticket."},
            "Pasta": {"min": 1, "max": 3, "role": "Uses same ingredients, different format. Great for non-pizza eaters in the group."},
            "Desserts": {"min": 0, "max": 2, "role": "Tiramisu or cannoli if you can source them. Otherwise skip — don't force it."},
        },
        "ideal_count": "8-15 items total",
        "pricing_advice": "Margherita anchors at $12-14. Specialty pizzas $15-20. Pasta $11-15. Apps $7-10.",
        "missing_signals": {
            "no_margherita": ("No Margherita pizza", "The Margherita is the benchmark — it tells customers the quality of your dough, sauce, and cheese."),
            "no_appetizer": ("No appetizers or starters", "Bruschetta, garlic bread, or a salad fills the wait and adds $7-10 to the check."),
            "no_specialty": ("Only basic pizzas", "Add 1-2 specialty/signature pizzas at a premium price to show creativity and anchor pricing."),
        },
    },
    "bakery": {
        "categories": {
            "Breads": {"min": 1, "max": 3, "role": "Daily loaves show you're a real bakery. Sourdough is king right now."},
            "Pastries": {"min": 2, "max": 5, "role": "Croissants, scones, muffins. High margin, bake in batches."},
            "Cookies & Bars": {"min": 1, "max": 3, "role": "Impulse buys at the counter. $2-4 each, costs pennies."},
            "Cakes & Special": {"min": 1, "max": 2, "role": "Showcase item. Doesn't need to sell daily — it sells everything else."},
        },
        "ideal_count": "8-12 items total",
        "pricing_advice": "Bread $5-8 per loaf. Pastries $3-6 each. Cookies $2-4. Cakes $5-8 per slice, $35-60 whole.",
        "missing_signals": {
            "no_bread": ("No bread/loaf item", "A bakery without bread is just a dessert shop. Even one daily loaf anchors your identity."),
            "no_pastry": ("Fewer than 2 pastries", "Pastries are the highest-margin baked goods. Scones, muffins, and croissants should be your core."),
            "too_few": ("Only {n} items", "Bakery display cases need to look full. 8-12 items minimum to feel abundant."),
        },
    },
}


def analyze_menu_gaps(tenant_slug):
    """Analyze what's missing from a restaurant's menu and suggest additions.

    Returns actionable suggestions based on restaurant type and current items.
    """
    tenant_path = os.path.join(TENANT_DIR, f"{tenant_slug}.yaml")
    if not os.path.exists(tenant_path):
        return {"error": f"Tenant {tenant_slug} not found"}

    tenant = yaml.safe_load(open(tenant_path))
    recipe_slugs = tenant.get("dishes") or tenant.get("recipes") or []

    # Load current dishes
    dishes = []
    for rs in recipe_slugs:
        rpath = os.path.join(RECIPE_DIR, f"{rs}.yaml")
        if os.path.exists(rpath):
            r = yaml.safe_load(open(rpath))
            if r:
                dishes.append(r)

    dish_names = [d.get("title", "").lower() for d in dishes]
    all_text = " ".join(dish_names)
    n = len(dishes)

    # Detect restaurant type from dishes
    rtype = "general"
    if any(w in all_text for w in ["burger", "smash", "fries"]):
        rtype = "burger"
    elif any(w in all_text for w in ["brisket", "pulled pork", "ribs", "smoked"]):
        rtype = "bbq"
    elif any(w in all_text for w in ["pizza", "margherita", "pasta", "bruschetta"]):
        rtype = "pizza"
    elif any(w in all_text for w in ["biscuit", "scone", "bread loaf", "cookie", "cake", "roll"]):
        rtype = "bakery"

    blueprint = MENU_BLUEPRINTS.get(rtype)
    if not blueprint:
        return {
            "tenant": tenant_slug,
            "restaurant_type": rtype,
            "dish_count": n,
            "suggestions": [{"type": "info", "message": "Upload your menu or vendor invoice to get personalized suggestions."}],
        }

    # Run gap checks
    suggestions = []

    # Type-specific missing item checks
    signals = blueprint.get("missing_signals", {})

    if rtype == "burger":
        has_premium = any(w in all_text for w in ["double", "deluxe", "loaded", "premium"])
        has_sides = any(w in all_text for w in ["fries", "fry", "rings", "tots", "slaw", "side"])
        has_dessert = any(w in all_text for w in ["custard", "shake", "ice cream", "dessert", "cookie"])
        if not has_premium and "no_premium" in signals:
            suggestions.append({"type": "add_item", "priority": "high", **dict(zip(["title", "detail"], signals["no_premium"]))})
        if not has_sides and "no_sides" in signals:
            suggestions.append({"type": "add_item", "priority": "high", **dict(zip(["title", "detail"], signals["no_sides"]))})
        if not has_dessert and "no_dessert" in signals:
            suggestions.append({"type": "add_item", "priority": "medium", **dict(zip(["title", "detail"], signals["no_dessert"]))})
        if n < 4:
            suggestions.append({"type": "menu_size", "priority": "high",
                                "title": signals["too_few"][0].format(n=n), "detail": signals["too_few"][1]})
        elif n > 12:
            suggestions.append({"type": "menu_size", "priority": "medium",
                                "title": signals["too_many"][0].format(n=n), "detail": signals["too_many"][1]})

    elif rtype == "bbq":
        has_brisket = "brisket" in all_text
        has_sides = any(w in all_text for w in ["slaw", "beans", "cornbread", "mac", "fries", "side"])
        has_combo = any(w in all_text for w in ["combo", "sampler", "two-meat", "three-meat"])
        has_sandwich = "sandwich" in all_text
        if not has_brisket and "no_brisket" in signals:
            suggestions.append({"type": "add_item", "priority": "high", **dict(zip(["title", "detail"], signals["no_brisket"]))})
        if not has_sides and "no_sides" in signals:
            suggestions.append({"type": "add_item", "priority": "high", **dict(zip(["title", "detail"], signals["no_sides"]))})
        if not has_combo and "no_combo" in signals:
            suggestions.append({"type": "add_item", "priority": "medium", **dict(zip(["title", "detail"], signals["no_combo"]))})
        if not has_sandwich and "no_sandwich" in signals:
            suggestions.append({"type": "add_item", "priority": "medium", **dict(zip(["title", "detail"], signals["no_sandwich"]))})

    elif rtype == "pizza":
        has_margherita = "margherita" in all_text
        has_appetizer = any(w in all_text for w in ["bruschetta", "bread", "salad", "antipast", "garlic"])
        has_specialty = n > 3  # more than basics
        if not has_margherita and "no_margherita" in signals:
            suggestions.append({"type": "add_item", "priority": "high", **dict(zip(["title", "detail"], signals["no_margherita"]))})
        if not has_appetizer and "no_appetizer" in signals:
            suggestions.append({"type": "add_item", "priority": "medium", **dict(zip(["title", "detail"], signals["no_appetizer"]))})

    elif rtype == "bakery":
        has_bread = any(w in all_text for w in ["bread", "loaf", "sourdough"])
        has_pastry = sum(1 for w in ["scone", "muffin", "croissant", "biscuit", "danish", "roll"] if w in all_text)
        if not has_bread and "no_bread" in signals:
            suggestions.append({"type": "add_item", "priority": "high", **dict(zip(["title", "detail"], signals["no_bread"]))})
        if has_pastry < 2 and "no_pastry" in signals:
            suggestions.append({"type": "add_item", "priority": "medium", **dict(zip(["title", "detail"], signals["no_pastry"]))})
        if n < 6:
            suggestions.append({"type": "menu_size", "priority": "high",
                                "title": signals["too_few"][0].format(n=n), "detail": signals["too_few"][1]})

    # Pricing analysis
    prices = [d.get("menu_pricing", {}).get("menu_price", 0) for d in dishes if d.get("menu_pricing", {}).get("menu_price", 0) > 0]
    if prices:
        avg_price = sum(prices) / len(prices)
        max_price = max(prices)
        min_price = min(prices)
        spread = max_price - min_price

        if spread < 3 and n > 3:
            suggestions.append({
                "type": "pricing",
                "priority": "medium",
                "title": f"Price spread is too tight (${min_price:.0f}-${max_price:.0f})",
                "detail": f"Widen your price range. A bigger spread lets customers self-select and makes mid-range items feel like good value. Try anchoring one item at ${max_price + 5:.0f}+.",
            })

        # Check if any item is priced below food cost
        for d in dishes:
            mp = d.get("menu_pricing", {})
            if mp.get("food_cost") and mp.get("menu_price"):
                fc_pct = mp["food_cost"] / mp["menu_price"] * 100
                if fc_pct > 40:
                    suggestions.append({
                        "type": "pricing",
                        "priority": "high",
                        "title": f"\"{d['title']}\" has {fc_pct:.0f}% food cost",
                        "detail": f"Target 28-33%. Either raise the price or find cheaper ingredient alternatives.",
                    })

    # Overall menu health
    if not suggestions:
        suggestions.append({
            "type": "healthy",
            "priority": "info",
            "title": "Menu looks solid",
            "detail": f"{n} items across the right categories for a {rtype} restaurant. {blueprint['pricing_advice']}",
        })

    return {
        "tenant": tenant_slug,
        "tenant_name": tenant.get("name", tenant_slug),
        "restaurant_type": rtype,
        "dish_count": n,
        "blueprint": blueprint,
        "current_dishes": [d.get("title", "?") for d in dishes],
        "suggestions": suggestions,
    }


def render_advisor_page(tenant, analysis, gap_analysis):
    """Render the Menu Advisor HTML page — combines engineering analysis with gap suggestions."""
    c = tenant.get("colors", {})
    colors = {
        "primary": c.get("primary", "#1C1C1C"),
        "accent": c.get("accent", "#E63946"),
        "background": c.get("background", "#FFF8F0"),
        "surface": c.get("surface", "#FFFFFF"),
        "text": c.get("text", "#333333"),
        "text_light": c.get("text_light", "#666666"),
        "success": c.get("success", "#2D6A4F"),
    }
    name = tenant.get("name", "Restaurant")
    rtype = gap_analysis.get("restaurant_type", "general")
    blueprint = gap_analysis.get("blueprint", {})
    suggestions = gap_analysis.get("suggestions", [])
    items = analysis.get("items", [])
    summary = analysis.get("summary", {})
    recs = analysis.get("recommendations", [])

    # Build the current menu table
    menu_rows = ""
    for item in items:
        cls = item["classification"]
        cls_colors = {"star": colors["accent"], "puzzle": colors["success"], "plowhorse": "#888", "dog": "#cc4444", "unpriced": "#aaa"}
        cls_bg = {"star": f"{colors['accent']}15", "puzzle": f"{colors['success']}15", "plowhorse": "#f5f5f5", "dog": "#fff0f0", "unpriced": "#f9f9f9"}
        cls_color = cls_colors.get(cls, "#999")
        badge = f'<span style="background:{cls_color};color:#fff;padding:2px 8px;border-radius:10px;font-size:0.7rem;font-weight:600;text-transform:uppercase;">{cls}</span>'
        margin = f'{item.get("margin_pct", "?")}%' if item.get("margin_pct") is not None else "—"
        menu_rows += f'''<tr style="background:{cls_bg.get(cls, '#fff')};">
            <td style="padding:0.6rem;font-weight:500;">{item["title"]}</td>
            <td style="padding:0.6rem;text-align:center;">${item["ingredient_cost"]:.2f}</td>
            <td style="padding:0.6rem;text-align:center;font-weight:600;">${item["menu_price"]:.2f}</td>
            <td style="padding:0.6rem;text-align:center;">{margin}</td>
            <td style="padding:0.6rem;text-align:center;">{badge}</td>
        </tr>'''

    # Build suggestions cards
    suggestion_cards = ""
    priority_icons = {"high": "!!!", "medium": "!!", "low": "!", "info": "i"}
    priority_colors = {"high": "#dc3545", "medium": "#fd7e14", "low": "#6c757d", "info": colors["success"]}
    for s in suggestions:
        p = s.get("priority", "info")
        icon = priority_icons.get(p, "*")
        pcolor = priority_colors.get(p, "#666")
        suggestion_cards += f'''<div style="background:{colors['surface']};border-left:4px solid {pcolor};border-radius:8px;padding:1rem 1.25rem;margin-bottom:0.75rem;box-shadow:0 1px 3px rgba(0,0,0,0.06);">
            <div style="display:flex;align-items:center;gap:0.5rem;margin-bottom:0.3rem;">
                <span style="background:{pcolor};color:#fff;width:24px;height:24px;border-radius:50%;display:inline-flex;align-items:center;justify-content:center;font-size:0.65rem;font-weight:700;">{icon}</span>
                <strong style="font-size:0.95rem;">{s.get("title", "")}</strong>
            </div>
            <p style="margin:0;font-size:0.85rem;color:{colors['text_light']};line-height:1.5;">{s.get("detail", "")}</p>
        </div>'''

    # Build action items from engineering recommendations
    action_cards = ""
    for r in recs:
        action_cards += f'''<div style="background:#fff3cd;border-radius:8px;padding:0.75rem 1rem;margin-bottom:0.5rem;font-size:0.85rem;color:#856404;">
            <strong>{r.get("priority", "").upper()}</strong>: {r.get("message", "")}
        </div>'''

    # Blueprint info
    blueprint_html = ""
    if blueprint:
        cats = ""
        for cat_name, cat_info in blueprint.get("categories", {}).items():
            cats += f'<div style="padding:0.5rem 0;border-bottom:1px solid rgba(0,0,0,0.05);"><strong>{cat_name}</strong> ({cat_info["min"]}-{cat_info["max"]} items)<br><span style="font-size:0.8rem;color:{colors["text_light"]};">{cat_info["role"]}</span></div>'
        blueprint_html = f'''<div style="background:{colors['surface']};border-radius:12px;padding:1.25rem;box-shadow:0 1px 3px rgba(0,0,0,0.06);margin-bottom:1.5rem;">
            <h3 style="margin:0 0 0.5rem 0;font-size:1rem;">Ideal {rtype.title()} Menu Structure</h3>
            <p style="font-size:0.85rem;color:{colors['text_light']};margin-bottom:0.75rem;">Target: {blueprint.get("ideal_count", "8-12 items")} &middot; {blueprint.get("pricing_advice", "")}</p>
            {cats}
        </div>'''

    html = f'''<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Menu Advisor | {name}</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@300;400;500;600;700&family=Lora:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
*{{margin:0;padding:0;box-sizing:border-box;}}
body{{font-family:'DM Sans',sans-serif;background:{colors['background']};color:{colors['text']};line-height:1.7;font-size:15px;-webkit-font-smoothing:antialiased;}}
.container{{max-width:900px;margin:0 auto;padding:1.5rem;}}
h1{{font-family:'Lora',Georgia,serif;color:{colors['primary']};letter-spacing:-0.02em;}}
h2{{font-family:'Lora',Georgia,serif;color:{colors['primary']};font-size:1.2rem;margin-bottom:0.75rem;}}
.hero{{text-align:center;padding:2rem 0 1.5rem;border-bottom:2px solid {colors['accent']};margin-bottom:2rem;}}
.hero p{{color:{colors['text_light']};margin-top:0.25rem;}}
.stats{{display:flex;gap:0.75rem;justify-content:center;margin:1rem 0;flex-wrap:wrap;}}
.stat{{background:{colors['surface']};padding:0.75rem 1.25rem;border-radius:12px;text-align:center;min-width:80px;box-shadow:0 1px 3px rgba(0,0,0,0.06);}}
.stat .n{{font-size:1.5rem;font-weight:700;color:{colors['accent']};}}
.stat .l{{font-size:0.7rem;text-transform:uppercase;letter-spacing:0.05em;color:{colors['text_light']};}}
.section{{margin-bottom:2rem;}}
table{{width:100%;border-collapse:collapse;font-size:0.85rem;}}
th{{padding:0.6rem;text-align:left;border-bottom:2px solid {colors['accent']};font-size:0.75rem;text-transform:uppercase;letter-spacing:0.05em;color:{colors['text_light']};}}
td{{border-bottom:1px solid rgba(0,0,0,0.05);}}
.nav{{background:{colors['primary']};padding:0.5rem 1.5rem;}}
.nav a{{color:#fff;text-decoration:none;font-size:0.85rem;margin-right:1rem;opacity:0.8;}}
.nav a:hover{{opacity:1;}}
.footer{{text-align:center;padding:1.5rem 0;border-top:1px solid #ddd;margin-top:2rem;font-size:0.8rem;color:{colors['text_light']};}}
</style></head><body>
<nav class="nav"><a href="index.html">&larr; Home</a><a href="menu.html">Menu Layout</a><a href="advisor.html" style="opacity:1;font-weight:600;">Menu Advisor</a></nav>
<div class="container">
<div class="hero">
    <h1>Menu Advisor</h1>
    <p>{name} &middot; {rtype.title()} Restaurant &middot; {gap_analysis.get("dish_count", 0)} items on menu</p>
    <div class="stats">
        <div class="stat"><div class="n">{summary.get("stars", 0)}</div><div class="l">Stars</div></div>
        <div class="stat"><div class="n">{summary.get("puzzles", 0)}</div><div class="l">Puzzles</div></div>
        <div class="stat"><div class="n">{summary.get("plowhorses", 0)}</div><div class="l">Plowhorses</div></div>
        <div class="stat"><div class="n">{summary.get("dogs", 0)}</div><div class="l">Dogs</div></div>
    </div>
</div>

<div class="section">
    <h2>Suggestions</h2>
    <p style="font-size:0.85rem;color:{colors['text_light']};margin-bottom:1rem;">Based on your inventory, menu structure, and pricing. These are starting points — you know your customers best.</p>
    {suggestion_cards}
</div>

{f'<div class="section"><h2>Menu Engineering Actions</h2>{action_cards}</div>' if action_cards else ''}

{blueprint_html}

<div class="section">
    <h2>Your Menu Breakdown</h2>
    <div style="overflow-x:auto;background:{colors['surface']};border-radius:12px;box-shadow:0 1px 3px rgba(0,0,0,0.06);">
    <table>
        <tr><th>Dish</th><th style="text-align:center;">Food Cost</th><th style="text-align:center;">Price</th><th style="text-align:center;">Margin</th><th style="text-align:center;">Class</th></tr>
        {menu_rows}
    </table>
    </div>
</div>

<div class="footer">
    <p>Menu Advisor by HowToCookAtHome &middot; Data from your vendor inventory + USDA FoodData Central</p>
    <p style="margin-top:0.3rem;">Override any suggestion with your own sales data. Real numbers always beat estimates.</p>
</div>
</div></body></html>'''

    return html
