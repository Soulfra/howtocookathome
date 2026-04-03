#!/usr/bin/env python3
"""
MIGRATE_YAML_TO_DB.PY — One-time migration from YAML files to the database.

Reads all tenant, inventory, recipe, and review data from the YAML source
files and inserts it into the SQLite database via db.py.

Run once:  python3 -m app.migrate
"""
import yaml
import os
import glob
import json
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import sys; sys.path.insert(0, BASE_DIR)
from app.db import DB

CONTENT_DIR = os.path.join(BASE_DIR, "content")
TENANT_DIR = os.path.join(CONTENT_DIR, "tenants")
INV_DIR = os.path.join(CONTENT_DIR, "inventories")
RECIPE_DIR = os.path.join(CONTENT_DIR, "recipes")


def migrate():
    db = DB()
    stats = {"tenants": 0, "inventory_items": 0, "dishes": 0, "dish_ingredients": 0, "flavor_scores": 0}

    # ---- 1. Tenants ----
    print("\n  Migrating tenants...")
    for tf in sorted(glob.glob(os.path.join(TENANT_DIR, "*.yaml"))):
        t = yaml.safe_load(open(tf))
        if not t or not t.get("slug"):
            continue
        db.upsert_tenant({
            "slug": t["slug"],
            "name": t.get("name", t["slug"]),
            "type": t.get("type", "partner"),
            "email_hash": t.get("email_hash", ""),
            "token_hash": t.get("token_hash", ""),
            "tagline": t.get("tagline", ""),
            "colors": t.get("colors", {}),
            "fonts": t.get("fonts", {}),
            "nav": t.get("nav", []),
            "footer": t.get("footer", {}),
            "logo_text": t.get("logo_text", t.get("name", "")),
        })
        stats["tenants"] += 1
        print(f"    {t['slug']}")

    # ---- 2. Inventory items ----
    print("\n  Migrating inventory...")
    for inv_file in sorted(glob.glob(os.path.join(INV_DIR, "*.yaml"))):
        inv = yaml.safe_load(open(inv_file))
        if not inv or not inv.get("items"):
            continue

        # Derive tenant slug from filename: big-reds-bbq-inventory.yaml -> big-reds-bbq
        fname = os.path.basename(inv_file).replace("-inventory.yaml", "")
        tenant_slug = fname

        for item in inv["items"]:
            db.add_inventory_item(tenant_slug, {
                "item_name": item.get("item", item.get("display_name", "")),
                "display_name": item.get("display_name", ""),
                "original_line": item.get("original", ""),
                "vendor_price": item.get("vendor_price"),
                "package_size": float(item["package_size"]) if item.get("package_size") else None,
                "package_unit": item.get("package_unit", ""),
                "unit_price": item.get("unit_price"),
                "unit_price_label": item.get("unit_price_label", ""),
                "usda_fdc_id": item.get("usda_fdc_id"),
                "usda_description": item.get("usda_description"),
                "match_source": item.get("_match_source", ""),
                "category": _categorize(item.get("item", "")),
            })
            stats["inventory_items"] += 1

        print(f"    {tenant_slug}: {len(inv['items'])} items")

    # ---- 3. Dishes + Ingredients + Flavors ----
    print("\n  Migrating dishes...")
    skip_types = {"inventory", "deprecated_invoice"}
    for rf in sorted(glob.glob(os.path.join(RECIPE_DIR, "*.yaml"))):
        r = yaml.safe_load(open(rf))
        if not r or r.get("type") in skip_types:
            continue

        slug = r.get("slug", os.path.basename(rf).replace(".yaml", ""))
        tenant_slug = r.get("tenant", "")

        # Try to derive tenant from slug prefix
        if not tenant_slug:
            tenant_slug = _guess_tenant(slug)

        if not tenant_slug:
            continue  # skip platform-level recipes for now

        # Get pricing info
        food_cost = 0
        menu_price = 0
        margin_pct = 0
        pricing = r.get("menu_pricing", {})
        if pricing:
            food_cost = pricing.get("food_cost", 0) or 0
            menu_price = pricing.get("menu_price", pricing.get("suggested_menu_price", 0)) or 0
            margin_pct = pricing.get("margin_pct", 0) or 0
        else:
            # Old format: sum ingredient costs
            for ing in r.get("ingredients", []):
                cost = ing.get("item_cost") or ing.get("vendor_price", 0) or 0
                food_cost += cost
            if food_cost > 0:
                menu_price = round(food_cost / 0.30, 2)
                margin_pct = 70.0

        # Classification
        classification = ""
        eng = r.get("menu_engineering", {})
        if eng:
            classification = eng.get("classification", "")

        dish_data = {
            "slug": slug,
            "tenant_slug": tenant_slug,
            "name": r.get("name", slug.replace("-", " ").title()),
            "cuisine": r.get("cuisine", ""),
            "type": r.get("type", "auto_generated"),
            "food_cost": food_cost,
            "menu_price": menu_price,
            "margin_pct": margin_pct,
            "popularity": eng.get("popularity", 0) if eng else 0,
            "classification": classification,
            "steps": r.get("steps", []),
            "auto_generated": r.get("auto_generated", True),
            "version": r.get("history", {}).get("version", "1.0") if r.get("history") else "1.0",
        }

        # Ingredients
        ingredients = []
        for ing in r.get("ingredients", []):
            ingredients.append({
                "item_name": ing.get("item", ""),
                "display_name": ing.get("display_name", ""),
                "usage_amount": ing.get("usage_amount", ing.get("amount")),
                "usage_unit": ing.get("usage_unit", ing.get("unit", "")),
                "item_cost": ing.get("item_cost", 0),
                "usda_fdc_id": ing.get("usda_fdc_id"),
                "match_source": ing.get("_match_source", ""),
            })

        db.add_dish(dish_data, ingredients)
        stats["dishes"] += 1
        stats["dish_ingredients"] += len(ingredients)

        # Flavor scores
        fp = r.get("flavor_profile", {})
        scores = fp.get("scores", {})
        if scores:
            db.save_flavor_scores(slug, scores, source="merged")
            stats["flavor_scores"] += len(scores)

        print(f"    {slug}: {len(ingredients)} ingredients, {len(scores)} flavors")

    # ---- Summary ----
    print(f"\n  Migration complete:")
    for k, v in stats.items():
        print(f"    {k:20s}: {v}")

    # Verify with platform stats
    print()
    ps = db.platform_stats()
    for k, v in ps.items():
        print(f"    {k:20s}: {v}")

    db.close()


def _categorize(item_name):
    """Simple ingredient categorization."""
    name = item_name.lower()
    proteins = ["beef", "pork", "chicken", "ribs", "brisket", "thigh", "breast", "fish", "shrimp", "steak"]
    spices = ["paprika", "garlic", "onion", "cumin", "oregano", "chili", "pepper", "salt", "cayenne", "cinnamon"]
    dairy = ["butter", "cream", "cheese", "milk", "yogurt"]
    produce = ["onion", "tomato", "lettuce", "lime", "lemon", "avocado", "cilantro", "jalape"]
    for p in proteins:
        if p in name:
            return "protein"
    for s in spices:
        if s in name:
            return "spice"
    for d in dairy:
        if d in name:
            return "dairy"
    for pr in produce:
        if pr in name:
            return "produce"
    return "other"


def _guess_tenant(slug):
    """Try to figure out which tenant a recipe belongs to from its slug prefix."""
    # Load all tenant slugs
    tenants = []
    for tf in glob.glob(os.path.join(TENANT_DIR, "*.yaml")):
        t = yaml.safe_load(open(tf))
        if t and t.get("slug"):
            tenants.append(t["slug"])

    # Check if the recipe slug starts with a tenant slug
    for ts in sorted(tenants, key=len, reverse=True):  # longest first
        if slug.startswith(ts + "-"):
            return ts
    return ""


if __name__ == "__main__":
    print("\n  YAML → DATABASE MIGRATION")
    print("  =========================")
    migrate()
