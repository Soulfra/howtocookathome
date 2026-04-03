#!/usr/bin/env python3
"""
EVAL_ENGINE.PY — Platform-level test harness for HTCAH.

This is NOT the review engine (which audits individual tenants).
This audits THE PLATFORM ITSELF — every engine, every data pipeline,
every assumption. It answers: "Is the system working correctly?"

Eval categories:
  1. DATA INTEGRITY    — DB consistency, FK violations, orphans
  2. USDA PIPELINE     — Match rates, nutrition lookups, flavor mapping
  3. ONBOARD PIPELINE  — End-to-end synthetic onboard test
  4. MENU ENGINE       — Classification accuracy, price estimation
  5. REVIEW ENGINE     — Score consistency, grading logic
  6. SITE GENERATION   — HTML output completeness, nav links, CSS
  7. DB vs YAML SYNC   — Are the two sources in agreement?
  8. CODE HEALTH       — Dead files, giant functions, missing imports

Each check returns:
  status: PASS / WARN / FAIL
  detail: what happened
  fix:    what to do about it (if not PASS)

Run:
  python3 -m app.eval              → full eval + HTML report
  python3 -m app.eval --quick      → data integrity only
"""
import os
import sys
import json
import glob
import yaml
import sqlite3
import traceback
from datetime import datetime
from collections import Counter

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import sys; sys.path.insert(0, BASE_DIR)
CONTENT_DIR = os.path.join(BASE_DIR, "content")
OUTPUT_DIR = os.path.join(BASE_DIR, "output")


# ============================================================
# CHECK FRAMEWORK
# ============================================================

class Check:
    """One eval check result."""
    def __init__(self, name, status, detail, fix="", category=""):
        self.name = name
        self.status = status  # PASS, WARN, FAIL
        self.detail = detail
        self.fix = fix
        self.category = category

    def to_dict(self):
        return {
            "name": self.name, "status": self.status,
            "detail": self.detail, "fix": self.fix,
            "category": self.category,
        }


def run_check(name, category, fn):
    """Run a check function safely, catch crashes."""
    try:
        return fn()
    except Exception as e:
        return Check(name, "FAIL", f"Crashed: {type(e).__name__}: {str(e)[:100]}",
                     f"Fix the exception in {name}", category)


# ============================================================
# 1. DATA INTEGRITY
# ============================================================

def eval_data_integrity():
    """Check database consistency."""
    from app.db import DB
    db = DB()
    checks = []

    # FK: dishes → tenants
    orphan_dishes = db.conn.execute(
        "SELECT count(*) FROM dishes WHERE tenant_slug NOT IN (SELECT slug FROM tenants)"
    ).fetchone()[0]
    checks.append(Check(
        "Dish→Tenant FK", "PASS" if orphan_dishes == 0 else "FAIL",
        f"{orphan_dishes} orphaned dishes" if orphan_dishes else "All dishes reference valid tenants",
        "Delete orphaned dishes or add missing tenants" if orphan_dishes else "",
        "data_integrity"
    ))

    # FK: dish_ingredients → dishes
    orphan_ings = db.conn.execute(
        "SELECT count(*) FROM dish_ingredients WHERE dish_slug NOT IN (SELECT slug FROM dishes)"
    ).fetchone()[0]
    checks.append(Check(
        "Ingredient→Dish FK", "PASS" if orphan_ings == 0 else "FAIL",
        f"{orphan_ings} orphaned ingredients" if orphan_ings else "All ingredients reference valid dishes",
        "", "data_integrity"
    ))

    # FK: flavor_scores → dishes
    orphan_fs = db.conn.execute(
        "SELECT count(*) FROM flavor_scores WHERE dish_slug NOT IN (SELECT slug FROM dishes)"
    ).fetchone()[0]
    checks.append(Check(
        "Flavor→Dish FK", "PASS" if orphan_fs == 0 else "FAIL",
        f"{orphan_fs} orphaned flavor scores" if orphan_fs else "All flavor scores reference valid dishes",
        "", "data_integrity"
    ))

    # FDC ID validity
    bad_fdc = db.conn.execute(
        "SELECT count(*) FROM inventory_items WHERE usda_fdc_id IS NOT NULL AND usda_fdc_id NOT IN (SELECT fdc_id FROM food)"
    ).fetchone()[0]
    checks.append(Check(
        "FDC ID Validity", "PASS" if bad_fdc == 0 else "FAIL",
        f"{bad_fdc} invalid FDC IDs" if bad_fdc else "All FDC IDs reference real USDA foods",
        "Re-run USDA matching for bad IDs" if bad_fdc else "", "data_integrity"
    ))

    # Flavor score bounds (0-10)
    over = db.conn.execute("SELECT count(*) FROM flavor_scores WHERE score > 10.01").fetchone()[0]
    under = db.conn.execute("SELECT count(*) FROM flavor_scores WHERE score < -0.01").fetchone()[0]
    checks.append(Check(
        "Flavor Score Bounds", "PASS" if (over + under) == 0 else "FAIL",
        f"{over} over 10, {under} under 0" if (over + under) else "All scores in [0, 10]",
        "Re-run normalize_flavor_profile()" if (over + under) else "", "data_integrity"
    ))

    # Duplicate dish slugs
    dupes = db.conn.execute(
        "SELECT slug, count(*) c FROM dishes GROUP BY slug HAVING c > 1"
    ).fetchall()
    checks.append(Check(
        "Unique Dish Slugs", "PASS" if not dupes else "FAIL",
        f"{len(dupes)} duplicate slugs" if dupes else "All dish slugs unique",
        "", "data_integrity"
    ))

    db.close()
    return checks


# ============================================================
# 2. USDA PIPELINE
# ============================================================

def eval_usda_pipeline():
    """Test USDA matching, nutrition, and flavor mapping."""
    from app.db import DB
    db = DB()
    checks = []

    # Overall match rate (exclude noise items like invoice metadata)
    total = db.conn.execute("SELECT count(*) FROM inventory_items WHERE match_source != 'noise_skip'").fetchone()[0]
    matched = db.conn.execute("SELECT count(*) FROM inventory_items WHERE usda_fdc_id IS NOT NULL AND match_source != 'noise_skip'").fetchone()[0]
    rate = (matched / total * 100) if total else 0
    status = "PASS" if rate >= 70 else "WARN" if rate >= 50 else "FAIL"
    checks.append(Check(
        "USDA Match Rate (Inventory)", status,
        f"{matched}/{total} ({rate:.0f}%)",
        "Expand normalization.yaml synonyms for common gaps" if rate < 70 else "",
        "usda_pipeline"
    ))

    # Top unmatched items (exclude noise-tagged items)
    unmatched = [i for i in db.unmatched_ingredients() if i.get("match_source") != "noise_skip"]
    gaps = Counter(i["item_name"] for i in unmatched)
    top_gaps = gaps.most_common(5)
    checks.append(Check(
        "Top USDA Gaps", "WARN" if top_gaps else "PASS",
        ", ".join(f"{n} ({c}x)" for n, c in top_gaps) if top_gaps else "No gaps",
        "Add these to normalization.yaml" if top_gaps else "",
        "usda_pipeline"
    ))

    # Nutrition lookup test
    from app.onboard import get_nutrition
    test_ids = [171325, 171327, 171328]  # garlic powder, onion powder, paprika
    working = 0
    for fdc_id in test_ids:
        n = get_nutrition(fdc_id)
        if n and "calories" in n:
            working += 1
    checks.append(Check(
        "Nutrition Lookup", "PASS" if working == len(test_ids) else "FAIL",
        f"{working}/{len(test_ids)} test lookups returned data",
        "Check foundation.db food_nutrient table" if working < len(test_ids) else "",
        "usda_pipeline"
    ))

    # Flavor mapping test
    from app.onboard import build_flavor_from_usda, normalize_flavor_profile
    test_ings = [{"item": "garlic powder", "usda_fdc_id": 171325}]
    raw = build_flavor_from_usda(test_ings)
    if raw and any(v > 0 for v in raw.values()):
        normalized = normalize_flavor_profile(raw)
        max_val = max(normalized.values()) if normalized else 0
        checks.append(Check(
            "Flavor Mapping", "PASS" if 9.5 <= max_val <= 10.0 else "WARN",
            f"Garlic powder: {len(raw)} dimensions, max normalized={max_val:.1f}",
            "Check NUTRIENT_MAP thresholds" if max_val > 10 else "",
            "usda_pipeline"
        ))
    else:
        checks.append(Check("Flavor Mapping", "FAIL", "build_flavor_from_usda returned empty",
                            "Check NUTRIENT_MAP in app/onboard.py", "usda_pipeline"))

    db.close()
    return checks


# ============================================================
# 3. ONBOARD PIPELINE (synthetic test)
# ============================================================

def eval_onboard_pipeline():
    """Run a synthetic onboard and verify every step."""
    checks = []

    # Test with a fake pricelist submission
    from app.onboard import (
        load_normalization, parse_ingredients, match_usda, get_nutrition,
        normalize_flavor_profile, build_flavor_from_usda, build_dish_flavor_profile,
    )

    test_input = "Ground Beef 10lb $45.00\nGarlic Powder 2lb $8.00\nKosher Salt 5lb $6.00"

    # Step 1: Parse
    parsed = parse_ingredients(test_input)
    checks.append(Check(
        "Parse Ingredients", "PASS" if len(parsed) == 3 else "FAIL",
        f"Parsed {len(parsed)}/3 expected items",
        "Check parse_ingredients regex" if len(parsed) != 3 else "",
        "onboard_pipeline"
    ))

    # Step 2: USDA match
    syn_map = load_normalization()
    matched = 0
    for ing in parsed:
        result = match_usda(ing["item"], syn_map)
        if result.get("usda_fdc_id"):
            matched += 1
    checks.append(Check(
        "USDA Match (Synthetic)", "PASS" if matched >= 2 else "WARN" if matched >= 1 else "FAIL",
        f"{matched}/3 matched",
        "Add 'ground beef', 'kosher salt' to normalization.yaml" if matched < 3 else "",
        "onboard_pipeline"
    ))

    # Step 3: Pricing extraction
    priced = sum(1 for p in parsed if p.get("vendor_price") is not None)
    checks.append(Check(
        "Price Extraction", "PASS" if priced == 3 else "WARN",
        f"{priced}/3 items got vendor prices",
        "Check price regex in parse_ingredients" if priced < 3 else "",
        "onboard_pipeline"
    ))

    # Step 4: Flavor profile build
    test_ings_for_flavor = [
        {"item": "garlic powder", "usda_fdc_id": 171325},
        {"item": "ground beef", "usda_fdc_id": None},
    ]
    profile = build_dish_flavor_profile(test_ings_for_flavor)
    if profile and isinstance(profile, dict) and profile.get("scores"):
        scores = profile["scores"]
        max_s = max(scores.values()) if scores else 0
        checks.append(Check(
            "Flavor Profile Build", "PASS" if max_s <= 10.01 else "FAIL",
            f"{len(scores)} dimensions, max={max_s:.1f}",
            "normalize_flavor_profile not capping at 10" if max_s > 10 else "",
            "onboard_pipeline"
        ))
    else:
        checks.append(Check(
            "Flavor Profile Build", "WARN",
            "build_dish_flavor_profile returned incomplete data",
            "Check SPICE_CATEGORIES fallback", "onboard_pipeline"
        ))

    return checks


# ============================================================
# 4. MENU ENGINE
# ============================================================

def eval_menu_engine():
    """Validate menu engineering logic."""
    from app.menu import classify_menu_item, estimate_popularity, estimate_menu_price, analyze_menu
    checks = []

    # Classification logic: (food_cost, menu_price, popularity) → expected class
    # High margin = low cost relative to price. High pop = above threshold.
    test_cases = [
        (2.0, 20.0, 80, "star"),       # high margin (90%), high pop
        (2.0, 20.0, 20, "puzzle"),      # high margin (90%), low pop
        (15.0, 20.0, 80, "plowhorse"), # low margin (25%), high pop
        (15.0, 20.0, 20, "dog"),       # low margin (25%), low pop
    ]
    correct = 0
    for cost, price, pop, expected in test_cases:
        result = classify_menu_item(cost, price, pop)
        if isinstance(result, dict) and result.get("classification") == expected:
            correct += 1
    checks.append(Check(
        "Classification Logic", "PASS" if correct == 4 else "FAIL",
        f"{correct}/4 classification tests passed",
        "Check classify_menu_item thresholds" if correct < 4 else "",
        "menu_engine"
    ))

    # Price estimation sanity: estimate_menu_price(food_cost_dollars)
    price = estimate_menu_price(3.50)
    expected_price = 3.50 / 0.30  # ~11.67
    checks.append(Check(
        "Price Estimation", "PASS" if 10 < price < 15 else "WARN",
        f"$3.50 food cost → ${price:.2f} menu price (expected ~$11.67)",
        "Check 30% food cost target" if not (10 < price < 15) else "",
        "menu_engine"
    ))

    # Run against a real tenant
    analysis = analyze_menu("big-reds-bbq")
    if analysis and not analysis.get("error") and analysis.get("items"):
        n_items = len(analysis["items"])
        classifications = set(i.get("classification") for i in analysis["items"])
        checks.append(Check(
            "Live Menu Analysis", "PASS" if n_items >= 2 else "WARN",
            f"{n_items} items analyzed, types: {classifications}",
            "", "menu_engine"
        ))
    else:
        checks.append(Check(
            "Live Menu Analysis", "FAIL",
            f"analyze_menu returned: {analysis.get('error', 'empty')}",
            "Check big-reds-bbq dish data", "menu_engine"
        ))

    return checks


# ============================================================
# 5. REVIEW ENGINE
# ============================================================

def eval_review_engine():
    """Validate review scoring and grading."""
    from app.review import review_tenant, _grade_pct
    checks = []

    # Grade boundaries
    grade_tests = [(95, "A"), (85, "B"), (75, "B"), (65, "C"), (50, "D"), (30, "F")]
    correct = 0
    for score, expected in grade_tests:
        actual = _grade_pct(score)
        if actual == expected:
            correct += 1
    checks.append(Check(
        "Grade Boundaries", "PASS" if correct == len(grade_tests) else "WARN",
        f"{correct}/{len(grade_tests)} grade boundary tests passed",
        "Check _grade_pct thresholds", "review_engine"
    ))

    # Run against real tenant
    review = review_tenant("big-reds-bbq")
    if review and not review.get("error"):
        score = review["overall_score"]
        grade = review["overall_grade"]
        n_checks = len(review.get("checks", {}))
        n_actions = len(review.get("priority_actions", []))
        checks.append(Check(
            "Live Review (big-reds-bbq)", "PASS" if n_checks == 6 else "WARN",
            f"Score: {score}/100 ({grade}), {n_checks}/6 checks ran, {n_actions} actions",
            "Missing check categories" if n_checks < 6 else "",
            "review_engine"
        ))
        # Score should be reasonable (not 0, not 100 for a partial tenant)
        checks.append(Check(
            "Score Reasonableness", "PASS" if 20 < score < 95 else "WARN",
            f"Score {score} for a partial tenant (expected 20-95 range)",
            "Review scoring weights" if not (20 < score < 95) else "",
            "review_engine"
        ))
    else:
        checks.append(Check("Live Review", "FAIL", str(review), "", "review_engine"))

    return checks


# ============================================================
# 6. SITE GENERATION
# ============================================================

def eval_site_generation():
    """Check HTML output quality."""
    checks = []
    tenant_dirs = glob.glob(os.path.join(OUTPUT_DIR, "tenants", "*"))

    missing_pages = []
    broken_nav = []
    missing_css_vars = []

    for td in tenant_dirs:
        slug = os.path.basename(td)
        if slug == "htcah":
            continue

        # Required pages
        for page in ["index.html", "review.html"]:
            path = os.path.join(td, page)
            if not os.path.exists(path):
                missing_pages.append(f"{slug}/{page}")
                continue

            content = open(path).read()

            # Check nav has all links
            for link in ["index.html", "menu.html", "review.html"]:
                if link not in content:
                    broken_nav.append(f"{slug}/{page} missing nav link to {link}")

            # Check CSS variables are used (not hardcoded colors)
            if "var(--primary)" not in content and "var(--accent)" not in content:
                if page == "review.html":  # review might use shared CSS
                    if "--primary" not in content:
                        missing_css_vars.append(f"{slug}/{page}")

    checks.append(Check(
        "Required Pages", "PASS" if not missing_pages else "FAIL",
        f"{len(missing_pages)} missing" if missing_pages else f"All {len(tenant_dirs)-1} tenants have required pages",
        ", ".join(missing_pages[:5]) if missing_pages else "",
        "site_generation"
    ))

    checks.append(Check(
        "Navigation Links", "PASS" if not broken_nav else "WARN",
        f"{len(broken_nav)} broken nav links" if broken_nav else "All pages have complete navigation",
        "; ".join(broken_nav[:3]) if broken_nav else "",
        "site_generation"
    ))

    # API health.json existence
    missing_api = []
    for td in tenant_dirs:
        slug = os.path.basename(td)
        if slug == "htcah":
            continue
        api_path = os.path.join(td, "api", "health.json")
        if not os.path.exists(api_path):
            missing_api.append(slug)
        else:
            try:
                data = json.load(open(api_path))
                if "tenant" not in data or "dishes" not in data:
                    missing_api.append(f"{slug} (malformed)")
            except:
                missing_api.append(f"{slug} (invalid JSON)")

    checks.append(Check(
        "API Health Endpoints", "PASS" if not missing_api else "WARN",
        f"{len(missing_api)} missing/broken" if missing_api else "All tenants have valid health.json",
        ", ".join(missing_api) if missing_api else "",
        "site_generation"
    ))

    return checks


# ============================================================
# 7. DB vs YAML SYNC
# ============================================================

def eval_db_yaml_sync():
    """Check that DB and YAML files agree."""
    from app.db import DB
    checks = []
    db = DB()

    # Tenant count (exclude auto-created pseudo-tenants that have no YAML)
    yaml_count = len(glob.glob(os.path.join(CONTENT_DIR, "tenants", "*.yaml")))
    db_count = db.conn.execute("SELECT count(*) FROM tenants WHERE slug != 'platform'").fetchone()[0]
    checks.append(Check(
        "Tenant Count Sync", "PASS" if yaml_count == db_count else "WARN",
        f"YAML: {yaml_count}, DB: {db_count}",
        "Run python3 -m app.migrate" if yaml_count != db_count else "",
        "db_yaml_sync"
    ))

    # Inventory count
    yaml_inv = 0
    for f in glob.glob(os.path.join(CONTENT_DIR, "inventories", "*.yaml")):
        inv = yaml.safe_load(open(f))
        if inv and inv.get("items"):
            yaml_inv += len(inv["items"])
    db_inv = db.conn.execute("SELECT count(*) FROM inventory_items").fetchone()[0]
    checks.append(Check(
        "Inventory Count Sync", "PASS" if abs(yaml_inv - db_inv) <= 2 else "WARN",
        f"YAML: {yaml_inv} items, DB: {db_inv} items",
        "Re-run migration" if abs(yaml_inv - db_inv) > 2 else "",
        "db_yaml_sync"
    ))

    # Recipe/dish count
    skip = {"inventory", "deprecated_invoice"}
    yaml_dishes = 0
    for f in glob.glob(os.path.join(CONTENT_DIR, "recipes", "*.yaml")):
        r = yaml.safe_load(open(f))
        if r and r.get("type") not in skip:
            yaml_dishes += 1
    db_dishes = db.conn.execute("SELECT count(*) FROM dishes").fetchone()[0]
    checks.append(Check(
        "Dish Count Sync", "PASS" if abs(yaml_dishes - db_dishes) <= 2 else "WARN",
        f"YAML: {yaml_dishes} recipes, DB: {db_dishes} dishes",
        "Some recipes may not have matched a tenant" if yaml_dishes != db_dishes else "",
        "db_yaml_sync"
    ))

    db.close()
    return checks


# ============================================================
# 8. CODE HEALTH
# ============================================================

def eval_code_health():
    """Check code quality indicators."""
    checks = []

    # File sizes
    big_files = []
    for f in glob.glob(os.path.join(BASE_DIR, "app", "*.py")):
        lines = sum(1 for _ in open(f))
        name = os.path.basename(f)
        if lines > 500:
            big_files.append((name, lines))

    checks.append(Check(
        "File Sizes", "WARN" if big_files else "PASS",
        ", ".join(f"{n} ({l} lines)" for n, l in big_files) if big_files else "All files under 500 lines",
        "Consider splitting large files into modules" if big_files else "",
        "code_health"
    ))

    # Legacy files (intake.py and publish.py are the old pipeline)
    legacy = []
    for name in ["intake.py", "publish.py"]:
        path = os.path.join(BASE_DIR, name)
        if os.path.exists(path):
            legacy.append(name)
    checks.append(Check(
        "Legacy Files", "WARN" if legacy else "PASS",
        f"Pre-tenant pipeline files still present: {', '.join(legacy)}" if legacy else "No legacy files",
        "Archive or delete legacy files (replaced by app/onboard.py + app/publish.py)" if legacy else "",
        "code_health"
    ))

    # Duplicate function names across files
    import ast
    func_defs = {}
    for f in glob.glob(os.path.join(BASE_DIR, "app", "*.py")):
        name = os.path.basename(f)
        try:
            tree = ast.parse(open(f).read())
        except:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                fn = node.name
                if fn.startswith("_") or fn in ("main", "__init__"):
                    continue
                if fn in func_defs:
                    func_defs[fn].append(name)
                else:
                    func_defs[fn] = [name]

    dupes = {fn: files for fn, files in func_defs.items() if len(files) > 1}
    checks.append(Check(
        "Duplicate Function Names", "WARN" if dupes else "PASS",
        ", ".join(f"{fn}() in {'+'.join(files)}" for fn, files in dupes.items()) if dupes else "No duplicate function names",
        "Consolidate or rename to avoid confusion" if dupes else "",
        "code_health"
    ))

    # YAML files that look stale (deprecated but not cleaned up)
    stale = []
    for f in glob.glob(os.path.join(CONTENT_DIR, "recipes", "*.yaml")):
        r = yaml.safe_load(open(f))
        if r and r.get("type") in ("deprecated_invoice", "inventory"):
            stale.append(os.path.basename(f))
    checks.append(Check(
        "Stale YAML Files", "WARN" if stale else "PASS",
        f"{len(stale)} deprecated/inventory recipe files still in content/recipes/" if stale else "No stale files",
        "Move to archive/ or delete" if stale else "",
        "code_health"
    ))

    return checks


# ============================================================
# REPORT GENERATOR
# ============================================================

def generate_report(all_checks):
    """Generate an HTML eval report."""
    now = datetime.now()
    ts = now.strftime("%B %d, %Y at %I:%M %p")
    ver = now.strftime("v%Y.%m.%d")

    n_pass = sum(1 for c in all_checks if c.status == "PASS")
    n_warn = sum(1 for c in all_checks if c.status == "WARN")
    n_fail = sum(1 for c in all_checks if c.status == "FAIL")
    total = len(all_checks)
    health_pct = round((n_pass / total) * 100) if total else 0

    # Group by category
    categories = {}
    for c in all_checks:
        cat = c.category or "uncategorized"
        if cat not in categories:
            categories[cat] = []
        categories[cat].append(c)

    cat_labels = {
        "data_integrity": "Data Integrity",
        "usda_pipeline": "USDA Pipeline",
        "onboard_pipeline": "Onboard Pipeline",
        "menu_engine": "Menu Engine",
        "review_engine": "Review Engine",
        "site_generation": "Site Generation",
        "db_yaml_sync": "DB/YAML Sync",
        "code_health": "Code Health",
    }

    status_color = {"PASS": "#2D6A4F", "WARN": "#e65100", "FAIL": "#b71c1c"}
    status_bg = {"PASS": "#e8f5e9", "WARN": "#fff3e0", "FAIL": "#ffebee"}

    # Build check rows
    sections_html = ""
    for cat_key, cat_checks in categories.items():
        label = cat_labels.get(cat_key, cat_key)
        cat_pass = sum(1 for c in cat_checks if c.status == "PASS")
        cat_total = len(cat_checks)

        rows = ""
        for c in cat_checks:
            fix_html = f'<div class="fix">{c.fix}</div>' if c.fix else ""
            rows += f'''
            <div class="check-row" style="border-left:4px solid {status_color[c.status]};">
              <div class="check-status" style="background:{status_bg[c.status]};color:{status_color[c.status]};">{c.status}</div>
              <div class="check-body">
                <div class="check-name">{c.name}</div>
                <div class="check-detail">{c.detail}</div>
                {fix_html}
              </div>
            </div>'''

        sections_html += f'''
        <div class="section">
          <div class="section-header">
            <h2>{label}</h2>
            <span class="section-score">{cat_pass}/{cat_total}</span>
          </div>
          {rows}
        </div>'''

    # Fixes summary (only FAIL and WARN)
    fixes = [c for c in all_checks if c.status in ("FAIL", "WARN") and c.fix]
    fixes_html = ""
    for i, c in enumerate(sorted(fixes, key=lambda x: 0 if x.status == "FAIL" else 1), 1):
        fixes_html += f'''
        <div class="fix-row">
          <span class="fix-num" style="background:{status_color[c.status]};">{i}</span>
          <div>
            <div class="fix-text">{c.fix}</div>
            <div class="fix-meta">{cat_labels.get(c.category, c.category)} / {c.name}</div>
          </div>
        </div>'''

    if fixes_html:
        fixes_block = f'<div class="fixes-card"><h2>Fix List ({len(fixes)} items)</h2>{fixes_html}</div>'
    else:
        fixes_block = ""

    html = f'''<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>HTCAH Platform Eval — {ver}</title>
<style>
:root {{
  --primary:#1A1A2E; --accent:#C4975A; --bg:#f8f6f3; --surface:#fff;
  --text:#333; --text-light:#666; --pass:#2D6A4F; --warn:#e65100; --fail:#b71c1c;
}}
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family:'SF Mono', 'Fira Code', 'Consolas', monospace; color:var(--text); background:var(--bg); line-height:1.5; font-size:14px; }}
a {{ color:var(--accent); }}

.header {{ background:var(--primary); color:white; padding:2rem 2.5rem; }}
.header h1 {{ font-size:1.3rem; font-weight:600; letter-spacing:-0.02em; }}
.header .meta {{ font-size:0.75rem; opacity:0.6; margin-top:0.25rem; }}

.stats {{ display:grid; grid-template-columns:repeat(5, 1fr); gap:1px; background:rgba(0,0,0,0.08); margin:0; }}
.stat {{ background:var(--surface); padding:1.25rem; text-align:center; }}
.stat-num {{ font-size:1.8rem; font-weight:700; }}
.stat-label {{ font-size:0.65rem; text-transform:uppercase; letter-spacing:0.08em; color:var(--text-light); margin-top:0.2rem; }}

.container {{ max-width:960px; margin:0 auto; padding:2rem; }}

.section {{ margin-bottom:2rem; }}
.section-header {{ display:flex; justify-content:space-between; align-items:baseline; padding-bottom:0.5rem; border-bottom:2px solid var(--primary); margin-bottom:0.75rem; }}
.section-header h2 {{ font-size:0.9rem; font-weight:700; color:var(--primary); text-transform:uppercase; letter-spacing:0.05em; }}
.section-score {{ font-size:0.8rem; font-weight:600; color:var(--text-light); }}

.check-row {{ display:flex; gap:0.75rem; padding:0.6rem 0.75rem; margin-bottom:0.25rem; background:var(--surface); border-radius:4px; }}
.check-status {{ flex-shrink:0; width:40px; text-align:center; font-size:0.65rem; font-weight:700; padding:0.2rem 0; border-radius:3px; align-self:flex-start; margin-top:0.1rem; }}
.check-body {{ flex:1; min-width:0; }}
.check-name {{ font-weight:600; font-size:0.8rem; }}
.check-detail {{ font-size:0.75rem; color:var(--text-light); margin-top:0.1rem; }}
.fix {{ font-size:0.7rem; color:var(--warn); margin-top:0.2rem; font-style:italic; }}

.fixes-card {{ background:var(--surface); border-radius:6px; padding:1.25rem; margin-bottom:2rem; border-left:4px solid var(--fail); }}
.fixes-card h2 {{ font-size:0.9rem; font-weight:700; color:var(--primary); margin-bottom:0.75rem; text-transform:uppercase; letter-spacing:0.05em; }}
.fix-row {{ display:flex; gap:0.6rem; padding:0.5rem 0; border-bottom:1px solid rgba(0,0,0,0.05); }}
.fix-row:last-child {{ border-bottom:none; }}
.fix-num {{ flex-shrink:0; width:22px; height:22px; border-radius:50%; color:white; display:flex; align-items:center; justify-content:center; font-size:0.65rem; font-weight:700; }}
.fix-text {{ font-size:0.8rem; font-weight:500; }}
.fix-meta {{ font-size:0.65rem; color:var(--text-light); }}

footer {{ background:var(--primary); color:rgba(255,255,255,0.4); padding:1rem; text-align:center; font-size:0.65rem; margin-top:2rem; }}

@media(max-width:768px) {{
  .stats {{ grid-template-columns:repeat(3, 1fr); }}
  .container {{ padding:1rem; }}
}}
</style>
</head><body>

<div class="header">
  <h1>HTCAH Platform Eval</h1>
  <div class="meta">{ver} &middot; {ts} &middot; {total} checks across {len(categories)} categories</div>
</div>

<div class="stats">
  <div class="stat">
    <div class="stat-num" style="color:var(--primary);">{health_pct}%</div>
    <div class="stat-label">Health</div>
  </div>
  <div class="stat">
    <div class="stat-num">{total}</div>
    <div class="stat-label">Total Checks</div>
  </div>
  <div class="stat">
    <div class="stat-num" style="color:var(--pass);">{n_pass}</div>
    <div class="stat-label">Passing</div>
  </div>
  <div class="stat">
    <div class="stat-num" style="color:var(--warn);">{n_warn}</div>
    <div class="stat-label">Warnings</div>
  </div>
  <div class="stat">
    <div class="stat-num" style="color:var(--fail);">{n_fail}</div>
    <div class="stat-label">Failing</div>
  </div>
</div>

<div class="container">

  {fixes_block}

  {sections_html}

</div>

<footer>HTCAH Eval Engine &middot; Tests the platform, not the tenants &middot; Run: python3 eval_engine.py</footer>
</body></html>'''

    return html


# ============================================================
# MAIN
# ============================================================

def run_eval(quick=False):
    """Run all eval checks and generate report."""
    all_checks = []

    eval_fns = [
        ("data_integrity", eval_data_integrity),
        ("usda_pipeline", eval_usda_pipeline),
        ("onboard_pipeline", eval_onboard_pipeline),
        ("menu_engine", eval_menu_engine),
        ("review_engine", eval_review_engine),
        ("site_generation", eval_site_generation),
        ("db_yaml_sync", eval_db_yaml_sync),
        ("code_health", eval_code_health),
    ]

    if quick:
        eval_fns = eval_fns[:1]  # data integrity only

    for cat, fn in eval_fns:
        try:
            results = fn()
            all_checks.extend(results)
        except Exception as e:
            all_checks.append(Check(
                f"{cat} (crashed)", "FAIL",
                f"{type(e).__name__}: {str(e)[:100]}",
                f"Fix {cat} eval function", cat
            ))

    return all_checks


def main():
    quick = "--quick" in sys.argv

    print("\n  HTCAH PLATFORM EVAL")
    print("  ===================\n")

    checks = run_eval(quick=quick)

    # Console output
    current_cat = ""
    for c in checks:
        if c.category != current_cat:
            current_cat = c.category
            print(f"\n  [{current_cat.upper()}]")
        icon = {"PASS": " OK ", "WARN": "WARN", "FAIL": "FAIL"}[c.status]
        print(f"    [{icon}] {c.name:35s} {c.detail}")
        if c.fix:
            print(f"           → {c.fix}")

    # Summary
    n_pass = sum(1 for c in checks if c.status == "PASS")
    n_warn = sum(1 for c in checks if c.status == "WARN")
    n_fail = sum(1 for c in checks if c.status == "FAIL")
    total = len(checks)
    health = round((n_pass / total) * 100) if total else 0

    print(f"\n  RESULT: {health}% healthy ({n_pass} pass, {n_warn} warn, {n_fail} fail)")

    # Generate HTML report
    html = generate_report(checks)
    report_path = os.path.join(OUTPUT_DIR, "eval-report.html")
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(report_path, "w") as f:
        f.write(html)
    print(f"  Report: {report_path}\n")

    # Save JSON for programmatic access
    json_path = os.path.join(OUTPUT_DIR, "eval-report.json")
    with open(json_path, "w") as f:
        json.dump({
            "timestamp": datetime.now().isoformat(),
            "health_pct": health,
            "pass": n_pass, "warn": n_warn, "fail": n_fail,
            "checks": [c.to_dict() for c in checks],
        }, f, indent=2)

    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
