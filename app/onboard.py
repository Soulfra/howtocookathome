#!/usr/bin/env python3
"""
ONBOARD_API.PY — Handles partner sign-up and site generation.

POST /api/onboard → creates tenant, enriches ingredients, builds site.

Privacy-first:
  - No cookies set
  - No IP tracking
  - Token-based identity (like a wallet key)
  - Tenant ID in URL path, not session cookies
"""
import yaml, json, os, re, secrets, sqlite3, hashlib, sys
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
CONTENT_DIR = os.path.join(BASE_DIR, "content")
TENANT_DIR = os.path.join(CONTENT_DIR, "tenants")
RECIPE_DIR = os.path.join(CONTENT_DIR, "recipes")
USDA_DB = os.path.join(BASE_DIR, "data", "usda", "foundation.db")
NORM_PATH = os.path.join(CONTENT_DIR, "normalization.yaml")


def slugify(text):
    """Convert text to a URL-safe slug."""
    s = text.lower().strip()
    s = re.sub(r'[^\w\s-]', '', s)
    s = re.sub(r'[\s_]+', '-', s)
    s = re.sub(r'-+', '-', s).strip('-')
    return s[:50]


def generate_token():
    """Generate a 24-char token for tenant access. No cookies needed."""
    return secrets.token_urlsafe(18)


def load_normalization():
    """Load the synonym map for ingredient matching.

    Reads normalization.yaml ingredients section and builds a flat
    lookup: { synonym_lowercase: { canonical, display_name, usda_fdc_id } }
    """
    if not os.path.exists(NORM_PATH):
        return {}
    data = yaml.safe_load(open(NORM_PATH))
    syn_map = {}

    ingredients = data.get("ingredients", {})
    for key, entry in ingredients.items():
        fdc_id = entry.get("usda_fdc_id")
        display = entry.get("display_name", key.replace("_", " ").title())
        canonical = display.lower()

        record = {
            "canonical": canonical,
            "display_name": display,
            "usda_fdc_id": fdc_id,
        }

        # Map the key itself
        syn_map[key.lower()] = record
        syn_map[canonical] = record

        # Map all synonyms
        for syn in entry.get("synonyms", []):
            syn_map[syn.lower().strip()] = record

    # UK→US regional variants
    pipeline = data.get("pipeline", {})
    uk_to_us = pipeline.get("regional_variants", {}).get("uk_to_us", {})
    for uk_term, us_term in uk_to_us.items():
        us_lower = us_term.replace("_", " ").lower()
        if us_lower in syn_map:
            syn_map[uk_term.replace("_", " ").lower()] = syn_map[us_lower]

    return syn_map


# Words to strip when tokenizing ingredient names — packaging, sizing, brand noise
_NOISE_WORDS = {
    "lb", "lbs", "oz", "qt", "gal", "kg", "g", "ct", "pk", "bag", "box", "can",
    "whole", "fresh", "dried", "ground", "coarse", "fine", "raw", "organic",
    "large", "medium", "small", "extra", "pure", "sliced", "diced", "chopped",
    "boneless", "bone-in", "skin-on", "skinless", "choice", "select", "prime",
    "invoice", "sysco", "usfoods", "order",
}

def _tokenize(name):
    """Split ingredient name into meaningful tokens, strip noise."""
    clean = re.sub(r'[^a-z\s]', '', name.lower().strip())
    return [w for w in clean.split() if w and w not in _NOISE_WORDS and len(w) > 1]


def _token_match_synonyms(tokens, syn_map):
    """Try matching token subsets against the synonym map.

    Handles word-order variations like 'olive oil extra virgin' matching
    'extra virgin olive oil', and modifier stripping like 'oregano dried'
    matching 'oregano'.

    Strategy: try the full cleaned string, then progressively shorter
    token combinations, longest first.
    """
    if not tokens:
        return None

    # Try full token string (order-independent via sorted join)
    full = " ".join(tokens)
    if full in syn_map:
        return syn_map[full]

    # Try reversed token order
    rev = " ".join(reversed(tokens))
    if rev in syn_map:
        return syn_map[rev]

    # Try each individual token (catches 'oregano' from 'oregano dried')
    # But prefer longer matches, so try pairs first
    if len(tokens) >= 2:
        for i in range(len(tokens)):
            for j in range(i + 1, len(tokens)):
                pair = f"{tokens[i]} {tokens[j]}"
                if pair in syn_map:
                    return syn_map[pair]
                pair_rev = f"{tokens[j]} {tokens[i]}"
                if pair_rev in syn_map:
                    return syn_map[pair_rev]

    # Single token match (last resort for synonyms)
    for t in tokens:
        if t in syn_map:
            return syn_map[t]

    return None


def _smart_usda_search(conn, tokens, original_clean):
    """Multi-strategy USDA database search.

    Filters out branded/packaged products by preferring descriptions that
    start with the search term or match USDA canonical naming patterns
    (e.g., 'Spices, cinnamon' not '1.55OZ SUNCHIPS APPLE CINNAMON').

    Priority:
      1. Exact description match
      2. Description starts with search term
      3. Spice prefix pattern ('Spices, X')
      4. USDA canonical pattern ('Food, descriptor') — filters out branded junk
      5. Multi-token AND with brand filtering
      6. Single-token with brand filtering
    """
    search = original_clean

    # Brand/junk filter: skip descriptions that look like packaged products
    # USDA has thousands of entries like "1.55OZ SUNCHIPS APPLE CINNAMON"
    BRAND_FILTER = "AND LOWER(description) NOT GLOB '*[0-9]*oz*' AND description NOT LIKE '%OZ %' AND description NOT LIKE '%Chex%' AND description NOT LIKE '%Lay''s%' AND description NOT LIKE '%Doritos%'"

    # Strategy 1: Exact match
    row = conn.execute(
        "SELECT fdc_id, description FROM food WHERE LOWER(description) = ? LIMIT 1",
        (search,)
    ).fetchone()
    if row:
        return row, "exact"

    # Strategy 2: Description starts with search term (strong signal)
    row = conn.execute(
        f"SELECT fdc_id, description FROM food WHERE LOWER(description) LIKE ? {BRAND_FILTER} LIMIT 1",
        (f"{search}%",)
    ).fetchone()
    if row:
        return row, "prefix"

    # Strategy 3: Spice prefix ('Spices, cinnamon', 'Spices, nutmeg')
    for token in tokens:
        if len(token) >= 3:
            row = conn.execute(
                "SELECT fdc_id, description FROM food WHERE LOWER(description) LIKE ? LIMIT 1",
                (f"spices, {token}%",)
            ).fetchone()
            if row:
                return row, "spice_prefix"

    # Strategy 4: USDA canonical patterns ('Beef, %', 'Pork, %', 'Basil, %')
    # Skip overly generic words that match wrong categories
    _GENERIC_SKIP = {"sauce", "oil", "rice", "milk", "salt", "sugar", "flour", "cream", "water", "juice", "stock", "broth"}
    for token in tokens:
        if len(token) >= 4 and token not in _GENERIC_SKIP:
            row = conn.execute(
                f"SELECT fdc_id, description FROM food WHERE LOWER(description) LIKE ? {BRAND_FILTER} LIMIT 1",
                (f"{token},%",)
            ).fetchone()
            if row:
                return row, "canonical"

    # Strategy 5: Multi-token AND with brand filtering
    if len(tokens) >= 2:
        conditions = " AND ".join(f"LOWER(description) LIKE '%{t}%'" for t in tokens[:3])
        row = conn.execute(
            f"SELECT fdc_id, description FROM food WHERE {conditions} {BRAND_FILTER} LIMIT 1"
        ).fetchone()
        if row:
            return row, "multi_token"

    # Strategy 6: Single meaningful token with brand filtering
    for token in tokens:
        if len(token) >= 5:  # require longer tokens for single-word search
            row = conn.execute(
                f"SELECT fdc_id, description FROM food WHERE LOWER(description) LIKE ? {BRAND_FILTER} LIMIT 1",
                (f"%{token}%",)
            ).fetchone()
            if row:
                return row, "single_token"

    # Strategy 7: LIKE on full string (last resort, includes branded)
    row = conn.execute(
        "SELECT fdc_id, description FROM food WHERE LOWER(description) LIKE ? LIMIT 1",
        (f"%{search}%",)
    ).fetchone()
    if row:
        return row, "like_fallback"

    return None, "none"


def match_usda(ingredient_name, syn_map):
    """Match an ingredient to USDA FoodData Central.

    Five-step resolution (like how maps match 'Mickey D's' → McDonald's):
      1. Exact synonym lookup (instant)
      2. Token-fuzzy synonym lookup (handles word order, modifiers)
      3. If synonym found but no FDC ID, use canonical for DB search
      4. Smart multi-strategy USDA text search
      5. Give up cleanly — flag for normalization review
    """
    clean = ingredient_name.lower().strip()
    tokens = _tokenize(ingredient_name)

    # Step 1: Exact synonym match
    norm_record = syn_map.get(clean)

    # Step 2: Token-fuzzy synonym match (handles 'olive oil extra virgin' etc.)
    if not norm_record:
        norm_record = _token_match_synonyms(tokens, syn_map)

    # If normalization gave us a direct FDC ID, done
    if norm_record and norm_record.get("usda_fdc_id"):
        return {
            "item": ingredient_name,
            "display_name": norm_record.get("display_name", ingredient_name.title()),
            "_match_source": "synonym",
            "usda_fdc_id": norm_record["usda_fdc_id"],
            "usda_description": norm_record.get("display_name", ""),
        }

    # Step 3: Use canonical name as improved search term
    if norm_record:
        search_clean = norm_record.get("canonical", clean)
        search_tokens = _tokenize(search_clean)
    else:
        search_clean = clean
        search_tokens = tokens

    # Step 4: Smart USDA search
    if not os.path.exists(USDA_DB):
        return {
            "item": ingredient_name,
            "display_name": ingredient_name.title(),
            "_match_source": "none",
            "usda_fdc_id": None,
            "usda_description": None,
        }

    conn = sqlite3.connect(USDA_DB)
    row, strategy = _smart_usda_search(conn, search_tokens, search_clean)
    conn.close()

    if row:
        source = f"synonym_search" if norm_record else f"usda_{strategy}"
        return {
            "item": ingredient_name,
            "display_name": ingredient_name.title(),
            "_match_source": source,
            "usda_fdc_id": row[0],
            "usda_description": row[1],
        }

    # Step 5: Unmatched
    return {
        "item": ingredient_name,
        "display_name": ingredient_name.title(),
        "_match_source": "unmatched",
        "usda_fdc_id": None,
        "usda_description": None,
    }


def get_nutrition(fdc_id):
    """Get basic nutrition for a USDA food by FDC ID."""
    if not fdc_id or not os.path.exists(USDA_DB):
        return None
    conn = sqlite3.connect(USDA_DB)
    rows = conn.execute("""
        SELECT n.name, fn.amount
        FROM food_nutrient fn
        JOIN nutrient n ON fn.nutrient_id = n.id
        WHERE fn.fdc_id = ?
        AND n.name IN ('Energy', 'Protein', 'Total lipid (fat)', 'Carbohydrate, by difference')
    """, (fdc_id,)).fetchall()
    conn.close()

    if not rows:
        return None

    nutri = {}
    for name, amount in rows:
        if "Energy" in name:
            nutri["calories"] = round(amount, 1) if amount else None
        elif "Protein" in name:
            nutri["protein_g"] = round(amount, 1) if amount else None
        elif "lipid" in name.lower():
            nutri["fat_g"] = round(amount, 1) if amount else None
        elif "Carbohydrate" in name:
            nutri["carbs_g"] = round(amount, 1) if amount else None
    return nutri if nutri else None


def parse_ingredients(text):
    """Parse ingredient list, recipe lines, or vendor invoices.
    Extracts: item name, amount, unit, price, package_size.
    Keeps ALL data — nothing gets stripped."""
    ingredients = []
    for line in text.strip().split("\n"):
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("---"):
            continue

        original = line
        price = None
        package_size = None
        package_unit = None
        amount = ""
        unit = ""
        item = ""

        # --- Tab-delimited input: "Ground Chuck 80/20\t5lb\t$24.99" ---
        # Split by tab first, then parse each field
        if "\t" in line:
            fields = [f.strip() for f in line.split("\t") if f.strip()]
            item_field = fields[0] if fields else ""
            for field in fields[1:]:
                # Check for price
                pm = re.match(r'^\$\s*([\d,]+\.?\d*)$', field)
                if pm:
                    price = float(pm.group(1).replace(",", ""))
                    continue
                # Check for package size: "5lb", "120ct", "30lb case", "6 bags"
                pkm = re.match(r'^(\d+/?#?\d*)\s*(lb|lbs|oz|gal|dz|ct|qt|cs|pk|bags?|case|heads?)\b', field, re.IGNORECASE)
                if pkm:
                    package_size = pkm.group(1)
                    package_unit = pkm.group(2).lower().rstrip("s")  # normalize: bags→bag
                    if package_unit == "case":
                        package_unit = "lb"  # "30lb case" → treat as lb
                    continue
            line = item_field  # continue parsing with just the item name
        else:
            # --- Extract price ($18.50, $89.50) ---
            price_match = re.search(r'\$\s*([\d,]+\.?\d*)', line)
            if price_match:
                price = float(price_match.group(1).replace(",", ""))
                line = line[:price_match.start()].strip()

        # --- Extract package size from vendor format (non-tab input) ---
        # "All Purpose Flour 50lb" or "Large Eggs 15dz" or "Unsalted Butter 36/1lb"
        if package_size is None:
            pkg_match = re.search(r'\s+(\d+/?#?\d*)\s*(lb|lbs|oz|gal|dz|ct|qt|cs|pk|bag)\s*$', line, re.IGNORECASE)
            if pkg_match:
                package_size = pkg_match.group(1)
                package_unit = pkg_match.group(2).lower()
                line = line[:pkg_match.start()].strip()

        # --- Extract recipe-style amounts ("2 lbs chicken", "1 can tomatoes") ---
        recipe_match = re.match(
            r'^([\d./]+)\s*(lbs?|oz|cups?|cup|tbsp|tsp|cans?|cloves?|bunch|heads?|stalks?|pieces?|slices?|pinch|dash|gal|qt|pt|ml|l|g|kg)?\s+(.+)',
            line, re.IGNORECASE
        )
        if recipe_match:
            amount = recipe_match.group(1)
            unit = (recipe_match.group(2) or "").strip()
            item = recipe_match.group(3).strip()
        else:
            item = line

        # --- Skip header lines like "Sysco Invoice #991234" ---
        if re.match(r'^(sysco|us foods|gfs|restaurant depot|invoice|order|po\s*#)', item, re.IGNORECASE):
            continue

        # --- Compute unit price if we have package info ---
        unit_price = None
        if price and package_size:
            try:
                pkg_qty = eval(package_size) if "/" in package_size else float(package_size)
                if pkg_qty > 0:
                    unit_price = round(price / pkg_qty, 4)
            except:
                pass

        if item:
            ing = {
                "original": original,
                "item": item,
                "display_name": item.title(),
                "amount": amount,
                "unit": unit,
            }
            if price is not None:
                ing["vendor_price"] = price
            if package_size:
                ing["package_size"] = package_size
                ing["package_unit"] = package_unit
            if unit_price is not None:
                ing["unit_price"] = unit_price
                ing["unit_price_label"] = f"${unit_price:.2f}/{package_unit or 'unit'}"
            ingredients.append(ing)

    return ingredients


# ============================================================
# SPICE / SEASONING CATEGORIES
# Used to detect which ingredients are spices, score their
# flavor contribution, and calculate taste-per-dollar.
# ============================================================
SPICE_CATEGORIES = {
    # item_keyword → (flavor_dimensions, flavor_intensity 1-10)
    "paprika": (["aromatic", "sweet", "heat"], 6),
    "smoked paprika": (["aromatic", "heat", "umami"], 8),
    "cumin": (["aromatic", "bitter"], 7),
    "oregano": (["aromatic", "bitter"], 5),
    "garlic powder": (["aromatic", "umami"], 6),
    "onion powder": (["aromatic", "sweet"], 4),
    "black pepper": (["heat", "aromatic"], 5),
    "red pepper flakes": (["heat"], 7),
    "cayenne": (["heat"], 9),
    "chili powder": (["heat", "aromatic"], 7),
    "cinnamon": (["sweet", "aromatic"], 6),
    "nutmeg": (["sweet", "aromatic"], 5),
    "turmeric": (["bitter", "aromatic"], 4),
    "ginger": (["heat", "aromatic", "sweet"], 6),
    "thyme": (["aromatic"], 4),
    "rosemary": (["aromatic", "bitter"], 5),
    "basil": (["aromatic", "sweet"], 4),
    "bay leaf": (["aromatic", "bitter"], 3),
    "coriander": (["aromatic", "sweet"], 4),
    "mustard": (["acid", "heat"], 5),
    "salt": (["salt"], 10),
    "kosher salt": (["salt"], 10),
    "brown sugar": (["sweet"], 8),
    "sugar": (["sweet"], 9),
    "vinegar": (["acid"], 8),
    "apple cider vinegar": (["acid", "sweet"], 7),
    "soy sauce": (["salt", "umami"], 9),
    "fish sauce": (["salt", "umami"], 9),
    "worcestershire": (["umami", "acid", "sweet"], 7),
    "vanilla": (["sweet", "aromatic"], 6),
    "vanilla extract": (["sweet", "aromatic"], 6),
}


def normalize_flavor_profile(raw_scores, scale=10):
    """Normalize a flavor profile so the max dimension = scale (default 10).

    If raw scores are {aromatic: 21, heat: 11, sweet: 10, umami: 6}
    Result at scale=10: {aromatic: 10.0, heat: 5.2, sweet: 4.8, umami: 2.9}

    This ensures bars never exceed 10/10 and the profile shows
    *relative* flavor balance, not absolute sums.
    """
    if not raw_scores:
        return {}
    max_val = max(raw_scores.values())
    if max_val <= 0:
        return {k: 0 for k in raw_scores}
    return {k: round((v / max_val) * scale, 1) for k, v in raw_scores.items()}


def build_flavor_from_usda(ingredients):
    """Build a flavor profile from USDA nutrient data.

    Maps real nutrients to perceivable flavor dimensions:
      Sodium (mg)     → salt      (high sodium = salty)
      Total Sugars    → sweet     (high sugar = sweet)
      Total lipid     → fat/rich  (high fat = rich mouthfeel)
      Vitamin C       → acid      (ascorbic acid = tart/bright)
      Energy (kcal)   → intensity (calorie-dense = concentrated flavor)
      Fiber           → bitter    (high-fiber ingredients tend bitter)
      Iron            → umami     (iron-rich foods correlate with savory depth)

    Returns normalized 0-10 profile based on actual USDA data.
    """
    if not os.path.exists(USDA_DB):
        return {}

    # Nutrient → flavor dimension mapping with thresholds
    # Thresholds are "high" values per 100g for that nutrient
    NUTRIENT_MAP = {
        "Sodium, Na":                    ("salt",    600),   # mg, > 600 = quite salty
        "Total Sugars":                  ("sweet",   15),    # g, > 15 = sweet
        "Total lipid (fat)":             ("fat",     20),    # g, > 20 = rich
        "Vitamin C, total ascorbic acid":("acid",    30),    # mg, > 30 = acidic/bright
        "Fiber, total dietary":          ("bitter",  15),    # g, proxy for bitterness
        "Iron, Fe":                      ("umami",   10),    # mg, proxy for savory depth
        "Energy":                        ("intensity", 400), # kcal, calorie density
    }

    conn = sqlite3.connect(USDA_DB)
    raw = {}

    for ing in ingredients:
        fdc_id = ing.get("usda_fdc_id")
        if not fdc_id:
            continue

        rows = conn.execute("""
            SELECT n.name, fn.amount
            FROM food_nutrient fn
            JOIN nutrient n ON fn.nutrient_id = n.id
            WHERE fn.fdc_id = ?
        """, (fdc_id,)).fetchall()

        for nutrient_name, amount in rows:
            if not amount or amount <= 0:
                continue
            for key, (dim, threshold) in NUTRIENT_MAP.items():
                if key in nutrient_name:
                    # Score: how far toward the "high" threshold
                    score = min(amount / threshold, 1.0) * 10
                    raw[dim] = raw.get(dim, 0) + score
                    break

    conn.close()

    if not raw:
        return {}

    return normalize_flavor_profile(raw)


def build_dish_flavor_profile(ingredients):
    """Build a complete flavor profile for a dish.

    Combines two sources:
      1. USDA nutrient data (real science) — for ingredients we have FDC matches
      2. SPICE_CATEGORIES lookup (curated) — for spice-specific dimensions like 'aromatic' and 'heat'

    Both are normalized to 0-10 and merged, with USDA taking priority
    for shared dimensions.
    """
    # Source 1: USDA nutrients
    usda_profile = build_flavor_from_usda(ingredients)

    # Source 2: Spice category profiles (for aromatic, heat, etc.)
    spice_raw = {}
    for ing in ingredients:
        item_lower = ing.get("item", "").lower().strip()
        for keyword, (dims, intensity) in SPICE_CATEGORIES.items():
            if keyword in item_lower or item_lower in keyword:
                for dim in dims:
                    spice_raw[dim] = spice_raw.get(dim, 0) + intensity
                break
    spice_profile = normalize_flavor_profile(spice_raw)

    # Merge: USDA is the authority for salt/sweet/fat/acid/umami
    # Spice categories fill in aromatic/heat/bitter if USDA doesn't cover them
    merged = {}
    all_dims = set(list(usda_profile.keys()) + list(spice_profile.keys()))
    for dim in all_dims:
        usda_val = usda_profile.get(dim, 0)
        spice_val = spice_profile.get(dim, 0)
        if dim in usda_profile and dim in spice_profile:
            # Both have data: weighted average (USDA 60%, spice 40%)
            merged[dim] = round(usda_val * 0.6 + spice_val * 0.4, 1)
        elif dim in usda_profile:
            merged[dim] = usda_val
        else:
            merged[dim] = spice_val

    # Final normalization to ensure max = 10
    return normalize_flavor_profile(merged)


def analyze_spice_value(enriched_ingredients):
    """Analyze spices in an ingredient list for taste-to-cost ratio.

    For each spice found:
      - flavor_score = sum of intensity across its flavor dimensions
      - unit_cost = price per unit from vendor data
      - value_score = flavor_score / unit_cost (higher = better bang for buck)

    This tells a restaurant: "Your oregano gives you 5 flavor points for $8.50/lb.
    Your smoked paprika gives 8 points for $12/lb. Paprika is better value."
    """
    spices = []

    for ing in enriched_ingredients:
        item_lower = ing.get("item", "").lower().strip()

        # Check against spice categories
        matched_cat = None
        for keyword, (dims, intensity) in SPICE_CATEGORIES.items():
            if keyword in item_lower or item_lower in keyword:
                matched_cat = (keyword, dims, intensity)
                break

        if not matched_cat:
            continue

        keyword, dims, intensity = matched_cat
        flavor_score = intensity * len(dims)  # total flavor contribution

        spice_data = {
            "item": ing.get("display_name", ing["item"]),
            "category": keyword,
            "flavor_dimensions": dims,
            "flavor_intensity": intensity,
            "flavor_score": flavor_score,
        }

        # Add cost data if available
        if ing.get("unit_price") is not None:
            spice_data["unit_price"] = ing["unit_price"]
            spice_data["unit_price_label"] = ing.get("unit_price_label", "")
            spice_data["value_score"] = round(flavor_score / ing["unit_price"], 2) if ing["unit_price"] > 0 else None
            spice_data["value_label"] = f"{spice_data['value_score']:.1f} flavor pts/$" if spice_data.get("value_score") else None
        elif ing.get("vendor_price") is not None:
            spice_data["vendor_price"] = ing["vendor_price"]
            # Can still compute relative value if we have a price
            spice_data["value_score"] = round(flavor_score / ing["vendor_price"], 4) if ing["vendor_price"] > 0 else None

        spices.append(spice_data)

    if not spices:
        return {"spices": []}

    # Sort by value score (best value first)
    priced = [s for s in spices if s.get("value_score")]
    if priced:
        priced.sort(key=lambda s: s["value_score"], reverse=True)
        best = priced[0]
    else:
        best = None

    # Aggregate flavor profile — weighted sum, then normalize to 0-10
    flavor_raw = {}
    for s in spices:
        for dim in s["flavor_dimensions"]:
            flavor_raw[dim] = flavor_raw.get(dim, 0) + s["flavor_intensity"]

    flavor_totals = normalize_flavor_profile(flavor_raw)

    return {
        "spices": spices,
        "best_value": best["item"] if best else None,
        "flavor_profile_aggregate": flavor_totals,
        "total_spice_cost": round(sum(s.get("vendor_price", 0) for s in spices), 2),
        "spice_count": len(spices),
    }


def aggregate_demand(tenant_dir_path=None):
    """Check demand across all tenants for spice blends.

    Scans all tenant recipes for common spice combinations.
    When N tenants need the same blend → production threshold met.

    This is the business model:
      1. Restaurants submit ingredients
      2. We see patterns (10 BBQ joints all need paprika+cumin+garlic)
      3. At threshold (e.g., 5 tenants), we produce an HTCAH spice blend
      4. Ship to those tenants as a branded product
    """
    tenant_dir = tenant_dir_path or TENANT_DIR
    demand = {}  # spice_combo_key → [tenant_slugs]

    for tf in glob.glob(os.path.join(tenant_dir, "*.yaml")):
        tenant = yaml.safe_load(open(tf))
        if not tenant or tenant.get("type") == "platform":
            continue

        tenant_slug = tenant.get("slug", "")
        for recipe_slug in tenant.get("recipes", []):
            recipe_path = os.path.join(RECIPE_DIR, f"{recipe_slug}.yaml")
            if not os.path.exists(recipe_path):
                continue

            recipe = yaml.safe_load(open(recipe_path))
            if not recipe:
                continue

            # Find spices in this recipe
            spice_names = []
            for ing in recipe.get("ingredients", []):
                item_lower = ing.get("item", "").lower()
                for keyword in SPICE_CATEGORIES:
                    if keyword in item_lower or item_lower in keyword:
                        spice_names.append(keyword)
                        break

            if len(spice_names) >= 2:
                # Generate pairs and triples (not full set — too strict)
                from itertools import combinations
                unique_spices = sorted(set(spice_names))
                for size in [2, 3]:
                    if len(unique_spices) >= size:
                        for combo in combinations(unique_spices, size):
                            combo_key = "+".join(combo)
                            if combo_key not in demand:
                                demand[combo_key] = []
                            if tenant_slug not in demand[combo_key]:
                                demand[combo_key].append(tenant_slug)

    # Find combos that hit threshold
    THRESHOLD = 2  # Low for now — raise to 5+ in production
    opportunities = []
    for combo, tenants in sorted(demand.items(), key=lambda x: len(x[1]), reverse=True):
        spices = combo.split("+")
        opportunities.append({
            "blend": spices,
            "blend_name": " + ".join(s.title() for s in spices),
            "tenant_count": len(tenants),
            "tenants": tenants,
            "threshold_met": len(tenants) >= THRESHOLD,
            "production_ready": len(tenants) >= THRESHOLD,
        })

    return {
        "opportunities": opportunities,
        "threshold": THRESHOLD,
        "total_tenants": len(set(t for ts in demand.values() for t in ts)),
        "blends_at_threshold": sum(1 for o in opportunities if o["threshold_met"]),
    }


def calculate_blend_ratios(blend_spices, tenant_dir_path=None):
    """Calculate per-tenant proportional ratios for a spice blend.

    Given a blend like ["garlic powder", "onion powder", "salt"],
    looks at each tenant's actual recipe data to figure out THEIR ideal ratio.

    Priority for ratio source:
      1. Recipe amounts (2 tbsp garlic, 1 tbsp onion → 67/33)
      2. Package sizes (5lb garlic, 2lb paprika → weight-proportional)
      3. Flavor intensity from SPICE_CATEGORIES (fallback)

    Returns per-tenant ratios + a default balanced ratio.
    """
    tenant_dir = tenant_dir_path or TENANT_DIR
    blend_set = set(s.lower() for s in blend_spices)
    tenant_ratios = {}

    for tf in glob.glob(os.path.join(tenant_dir, "*.yaml")):
        tenant = yaml.safe_load(open(tf))
        if not tenant or tenant.get("type") == "platform":
            continue

        tenant_slug = tenant.get("slug", "")
        tenant_name = tenant.get("name", tenant_slug)

        # Collect quantity signals for each spice from all of this tenant's recipes
        spice_quantities = {s: {"amounts": [], "packages": [], "intensity": 0} for s in blend_set}
        has_any = False

        for recipe_slug in tenant.get("recipes", []):
            recipe_path = os.path.join(RECIPE_DIR, f"{recipe_slug}.yaml")
            if not os.path.exists(recipe_path):
                continue
            recipe = yaml.safe_load(open(recipe_path))
            if not recipe:
                continue

            for ing in recipe.get("ingredients", []):
                item_lower = ing.get("item", "").lower().strip()
                matched_spice = None
                for spice in blend_set:
                    if spice in item_lower or item_lower in spice:
                        matched_spice = spice
                        break
                if not matched_spice:
                    continue

                has_any = True

                # Signal 1: Recipe amount (e.g., "2" tbsp)
                amt_str = str(ing.get("amount", "")).strip()
                if amt_str:
                    try:
                        if "/" in amt_str:
                            parts = amt_str.split("/")
                            amt = float(parts[0]) / float(parts[1])
                        else:
                            amt = float(amt_str)
                        spice_quantities[matched_spice]["amounts"].append(amt)
                    except (ValueError, ZeroDivisionError):
                        pass

                # Signal 2: Package size (e.g., 5 lb)
                pkg = ing.get("package_size")
                if pkg:
                    try:
                        pkg_val = eval(str(pkg)) if "/" in str(pkg) else float(pkg)
                        spice_quantities[matched_spice]["packages"].append(pkg_val)
                    except:
                        pass

        if not has_any:
            continue

        # Look up flavor intensity as fallback
        for spice in blend_set:
            if spice in SPICE_CATEGORIES:
                spice_quantities[spice]["intensity"] = SPICE_CATEGORIES[spice][1]

        # Determine ratio using best available signal
        raw_weights = {}
        ratio_source = "balanced"  # default

        # Try recipe amounts first
        amt_totals = {s: sum(q["amounts"]) for s, q in spice_quantities.items() if q["amounts"]}
        if len(amt_totals) == len(blend_set) and sum(amt_totals.values()) > 0:
            raw_weights = amt_totals
            ratio_source = "recipe_amounts"
        else:
            # Try package sizes
            pkg_totals = {s: sum(q["packages"]) for s, q in spice_quantities.items() if q["packages"]}
            if len(pkg_totals) == len(blend_set) and sum(pkg_totals.values()) > 0:
                raw_weights = pkg_totals
                ratio_source = "package_sizes"
            else:
                # Fallback: flavor intensity weighting
                int_weights = {s: q["intensity"] or 1 for s, q in spice_quantities.items()}
                if sum(int_weights.values()) > 0:
                    raw_weights = int_weights
                    ratio_source = "flavor_intensity"
                else:
                    raw_weights = {s: 1 for s in blend_set}

        # Normalize to percentages
        total = sum(raw_weights.values())
        if total > 0:
            ratios = {s: round((v / total) * 100, 1) for s, v in raw_weights.items()}
        else:
            ratios = {s: round(100 / len(blend_set), 1) for s in blend_set}

        tenant_ratios[tenant_slug] = {
            "tenant_name": tenant_name,
            "ratios": ratios,
            "ratio_source": ratio_source,
            "raw_weights": raw_weights,
        }

    # Default balanced ratio
    default_ratio = {s: round(100 / len(blend_set), 1) for s in blend_set}

    return {
        "blend": list(blend_set),
        "blend_name": " + ".join(s.title() for s in sorted(blend_set)),
        "tenant_ratios": tenant_ratios,
        "default_ratio": default_ratio,
        "tenant_count": len(tenant_ratios),
    }


def aggregate_demand_with_ratios(tenant_dir_path=None):
    """Full demand analysis: find overlapping blends AND compute per-tenant ratios.

    This is aggregate_demand() + calculate_blend_ratios() combined.
    Returns production-ready blend specs with individual tenant proportions.
    """
    base = aggregate_demand(tenant_dir_path)

    # For each blend that hit threshold, calculate ratios
    for opp in base["opportunities"]:
        if opp["threshold_met"]:
            ratio_data = calculate_blend_ratios(opp["blend"], tenant_dir_path)
            opp["tenant_ratios"] = ratio_data["tenant_ratios"]
            opp["default_ratio"] = ratio_data["default_ratio"]
            opp["has_custom_ratios"] = any(
                tr["ratio_source"] != "balanced"
                for tr in ratio_data["tenant_ratios"].values()
            )

    return base


import glob  # add to existing imports at top


def parse_recipe(text):
    """Parse a full recipe (ingredients above ---, steps below)."""
    if "---" in text:
        parts = text.split("---", 1)
        ing_text = parts[0]
        steps_text = parts[1]
    else:
        # Guess: lines with amounts are ingredients, rest are steps
        lines = text.strip().split("\n")
        ing_lines = []
        step_lines = []
        for line in lines:
            if re.match(r'^[\d./]', line.strip()):
                ing_lines.append(line)
            elif line.strip():
                step_lines.append(line)
        ing_text = "\n".join(ing_lines)
        steps_text = "\n".join(step_lines)

    ingredients = parse_ingredients(ing_text)
    steps = [s.strip() for s in steps_text.strip().split("\n") if s.strip()]
    return ingredients, steps


RESERVED_SLUGS = {
    "www", "api", "app", "admin", "blog", "shop", "mail", "smtp", "imap",
    "ftp", "cdn", "static", "assets", "docs", "help", "support", "status",
    "billing", "pay", "dashboard", "login", "auth", "oauth", "sso",
    "menu", "menus", "recipes", "episodes", "about", "onboard", "signup",
    "dev", "staging", "test", "demo", "beta", "preview", "sandbox",
    "ns1", "ns2", "mx", "spf", "dkim", "dmarc", "autoconfig", "autodiscover",
    "platform", "htcah", "howtocookathome",
}


def create_tenant_yaml(data, token):
    """Create a tenant YAML config from onboarding form data."""
    slug = slugify(data["name"])
    city = data.get("city", "").strip()
    state = data.get("state", "").strip().upper()[:2]
    zip_code = data.get("zip_code", "").strip()[:5]

    # Block reserved slugs — these are subdomains we need for the platform
    if slug in RESERVED_SLUGS:
        raise ValueError(f"'{data['name']}' is a reserved name. Please choose a different restaurant name.")

    # Handle slug collisions with location fallback
    # "joes-pizza" taken? Try "joes-pizza-austin", then "joes-pizza-austin-tx"
    candidate = slug
    tenant_yaml = os.path.join(TENANT_DIR, f"{candidate}.yaml")
    if os.path.exists(tenant_yaml):
        if city:
            candidate = f"{slug}-{slugify(city)}"
            tenant_yaml = os.path.join(TENANT_DIR, f"{candidate}.yaml")
        if os.path.exists(tenant_yaml) and state:
            candidate = f"{slug}-{slugify(city)}-{state.lower()}" if city else f"{slug}-{state.lower()}"
            tenant_yaml = os.path.join(TENANT_DIR, f"{candidate}.yaml")
        if os.path.exists(tenant_yaml):
            raise ValueError(
                f"'{data['name']}' is already registered"
                + (f" in {city}, {state}" if city else "")
                + ". Please contact support or choose a different name."
            )
    slug = candidate

    # Domain assignment: subdomain or path-based fallback
    base_domain = data.get("base_domain", "howtocookathome.com")
    subdomain_name = data.get("subdomain", slug)  # default to slug
    full_domain = f"{subdomain_name}.{base_domain}" if subdomain_name else None

    # Type and entity from domain picker (step 2)
    tenant_type = data.get("type", "restaurant")  # creator, corporate, restaurant, family
    entity_type = data.get("entity", "individual")  # individual, llc, nonprofit, corp
    roles = data.get("roles", [])

    config = {
        "tenant_id": slug,
        "slug": slug,
        "type": tenant_type,
        "entity_type": entity_type,
        "name": data["name"],
        "tagline": data.get("tagline", ""),
        "city": city,
        "state": state,
        "zip_code": zip_code,
        "email_hash": hashlib.sha256(data.get("email", "").encode()).hexdigest()[:16],
        "token_hash": hashlib.sha256(token.encode()).hexdigest(),
        "created_at": datetime.now(tz=__import__("datetime").timezone.utc).isoformat(),
        "domain": full_domain,
        "base_domain": base_domain,
        "subdomain": subdomain_name,
        "url_path": f"/t/{slug}/",  # always available as fallback
        "roles": roles,
        "tier": "seed",  # free tier — everyone starts here
        "colors": {
            "primary": data.get("colors", {}).get("primary", "#1A1A2E"),
            "accent": data.get("colors", {}).get("accent", "#C4975A"),
            "background": data.get("colors", {}).get("background", "#F5F0E8"),
            "surface": "#FFFFFF",
            "text": "#333333",
            "text_light": "#666666",
            "success": "#2D6A4F",
        },
        "fonts": {
            "heading": "Georgia, 'Times New Roman', serif",
            "body": "Arial, Helvetica, sans-serif",
        },
        "logo_text": data["name"],
        "nav": [
            {"label": "Recipes", "href": "/recipes"},
            {"label": "About", "href": "/about"},
        ],
        "footer": {
            "copyright": data["name"],
            "powered_by": "Powered by HowToCookAtHome",
            "powered_by_href": "https://howtocookathome.com",
        },
        "recipes": [],  # will be populated after enrichment
    }

    os.makedirs(TENANT_DIR, exist_ok=True)
    path = os.path.join(TENANT_DIR, f"{slug}.yaml")
    with open(path, "w") as f:
        yaml.dump(config, f, default_flow_style=False, allow_unicode=True)

    return slug, path


def create_inventory_yaml(slug, ingredients_enriched):
    """Create a vendor inventory YAML — what the restaurant BUYS.

    This is NOT a dish. This is the supply shelf:
      - What ingredients they stock
      - What they pay (vendor prices)
      - Package sizes
      - USDA matches + nutrition
    """
    inv_slug = f"{slug}-inventory"

    nutrition_details = {}
    for ing in ingredients_enriched:
        if ing.get("usda_fdc_id"):
            nutri = get_nutrition(ing["usda_fdc_id"])
            if nutri:
                nutrition_details[ing["display_name"]] = {
                    "fdc_id": ing["usda_fdc_id"],
                    "per_100g": nutri,
                }

    inventory = {
        "slug": inv_slug,
        "type": "inventory",
        "tenant": slug,
        "created_at": datetime.now(tz=__import__("datetime").timezone.utc).isoformat(),
        "items": ingredients_enriched,
        "summary": {
            "total_items": len(ingredients_enriched),
            "usda_matched": sum(1 for i in ingredients_enriched if i.get("usda_fdc_id")),
            "priced_items": sum(1 for i in ingredients_enriched if i.get("vendor_price")),
            "total_cost": round(sum(i.get("vendor_price", 0) or 0 for i in ingredients_enriched), 2),
        },
        "nutrition": nutrition_details,
    }

    inv_dir = os.path.join(CONTENT_DIR, "inventories")
    os.makedirs(inv_dir, exist_ok=True)
    path = os.path.join(inv_dir, f"{inv_slug}.yaml")
    with open(path, "w") as f:
        yaml.dump(inventory, f, default_flow_style=False, allow_unicode=True)

    return inv_slug, inventory


def create_dishes_from_inventory(slug, inventory_items, restaurant_type="general"):
    """Auto-generate plausible menu dishes from a vendor inventory.

    Uses restaurant_type to pick appropriate templates:
      - "burger" / items suggest burgers → Smash Burger, Cheeseburger, Bacon Burger, Fries
      - "bbq" / items suggest BBQ → Brisket Plate, Pulled Pork, Ribs
      - "pizza" / items suggest pizza → Margherita, Pepperoni, etc.
      - "general" → protein plates + sides (fallback)

    This is the ESTIMATION step — the restaurant refines later.
    """
    # Categorize inventory items
    proteins = []
    spices = []
    staples = []
    dairy = []
    produce = []
    sauces = []
    buns_bread = []
    fries_sides = []

    # Condiment/sauce terms that should NOT be treated as spices
    # even if they contain spice keywords (e.g. "yellow mustard" contains "mustard")
    CONDIMENT_SIGNALS = {"yellow mustard", "dijon mustard", "honey mustard", "mustard sauce",
                         "ketchup", "mayo", "mayonnaise", "bbq sauce", "hot sauce",
                         "ranch", "aioli", "sriracha", "teriyaki", "soy sauce"}

    for ing in inventory_items:
        item_lower = ing.get("item", "").lower()

        # Check if it's a condiment/sauce BEFORE spice check
        is_condiment = any(c in item_lower for c in CONDIMENT_SIGNALS)

        # Check spice categories (skip if it's clearly a condiment)
        is_spice = False
        if not is_condiment:
            for keyword in SPICE_CATEGORIES:
                if keyword in item_lower or item_lower in keyword:
                    is_spice = True
                    spices.append(ing)
                    break
        if is_spice:
            continue

        # Categorize the rest — expanded keyword lists
        if any(p in item_lower for p in ["brisket", "pork", "chicken", "beef", "steak", "ribs",
                                          "salmon", "shrimp", "fish", "lamb", "turkey", "sausage",
                                          "ground chuck", "ground beef", "ground round", "patty",
                                          "bacon", "ham", "prosciutto", "chorizo"]):
            proteins.append(ing)
        elif any(p in item_lower for p in ["bun", "brioche", "roll", "bread", "tortilla", "pita",
                                            "naan", "crust", "dough", "wrap"]):
            buns_bread.append(ing)
        elif any(p in item_lower for p in ["fries", "fry", "tots", "rings", "coleslaw", "slaw"]):
            fries_sides.append(ing)
        elif any(p in item_lower for p in ["butter", "cream", "cheese", "milk", "egg", "mozzarella",
                                            "parmesan", "ricotta", "yogurt", "custard"]):
            dairy.append(ing)
        elif any(p in item_lower for p in ["tomato", "onion", "garlic", "pepper", "basil",
                                            "lettuce", "cilantro", "jalap", "pickle",
                                            "avocado", "mushroom", "spinach"]):
            produce.append(ing)
        elif any(p in item_lower for p in ["vinegar", "mustard", "worcestershire", "soy sauce",
                                            "hot sauce", "ketchup", "mayo", "mayonnaise",
                                            "bbq sauce", "ranch", "aioli"]):
            sauces.append(ing)
        else:
            staples.append(ing)

    # --- Auto-detect restaurant type from inventory ---
    if restaurant_type == "general":
        all_names = " ".join(i.get("item", "").lower() for i in inventory_items)
        has_buns = len(buns_bread) > 0
        has_ground = any("ground" in i.get("item", "").lower() or "patty" in i.get("item", "").lower()
                         for i in proteins)
        has_fries = len(fries_sides) > 0
        has_bbq = any(p in " ".join(i.get("item", "").lower() for i in proteins)
                       for p in ["brisket", "ribs", "pork butt", "pork shoulder"])
        has_pizza = any(k in all_names for k in [
            "dough", "crust", "semolina", "san marzano", "pizza"])
        has_mozzarella = any("mozzarella" in i.get("item", "").lower() for i in dairy)
        has_bakery = any(k in all_names for k in [
            "flour", "yeast", "baking powder", "baking soda"]) and \
            any(k in all_names for k in ["butter", "eggs", "egg", "sugar", "cream"])

        if has_ground and has_buns:
            restaurant_type = "burger"
        elif has_bbq:
            restaurant_type = "bbq"
        elif has_pizza or (has_mozzarella and any(k in all_names for k in ["tomato", "basil", "oregano"])):
            restaurant_type = "pizza"
        elif has_bakery:
            restaurant_type = "bakery"

    dishes = []

    def get_unit_cost(ing):
        """Get the per-unit cost for ingredient cost calculation."""
        if ing.get("unit_price"):
            return ing["unit_price"]
        elif ing.get("vendor_price"):
            return ing["vendor_price"]
        return 0

    def make_dish(name, dish_ings, cuisine="General"):
        """Create a dish dict with cost calculation + nutrition + steps."""
        food_cost = 0
        dish_ingredients = []
        nutrition_details = {}

        for ing, usage_amt, usage_unit in dish_ings:
            uc = get_unit_cost(ing)
            item_cost = round(uc * usage_amt, 2) if uc else 0
            food_cost += item_cost

            fdc_id = ing.get("usda_fdc_id")
            dish_ingredients.append({
                "item": ing.get("item", ""),
                "display_name": ing.get("display_name", ""),
                "usage_amount": usage_amt,
                "usage_unit": usage_unit,
                "item_cost": item_cost,
                "usda_fdc_id": fdc_id,
                "_match_source": ing.get("_match_source", ""),
            })

            # Carry nutrition from USDA
            if fdc_id:
                nutri = get_nutrition(fdc_id)
                if nutri:
                    nutrition_details[ing.get("display_name", ing.get("item", ""))] = {
                        "fdc_id": fdc_id,
                        "per_100g": nutri,
                    }

        food_cost = round(food_cost, 2)
        suggested_price = round(food_cost / 0.30, 2) if food_cost > 0 else 0

        # Apply minimum price floors — bulk items calculate too low otherwise
        name_lower = name.lower()
        if any(w in name_lower for w in ["fries", "fry", "tots", "rings"]):
            suggested_price = max(suggested_price, 3.99)
        elif any(w in name_lower for w in ["custard", "shake", "ice cream", "sundae"]):
            suggested_price = max(suggested_price, 4.99)
        elif any(w in name_lower for w in ["burger", "smash", "deluxe", "sandwich"]):
            suggested_price = max(suggested_price, 7.99)
        elif any(w in name_lower for w in ["pizza", "margherita"]):
            suggested_price = max(suggested_price, 12.99)
        elif any(w in name_lower for w in ["pasta", "pomodoro", "penne", "spaghetti"]):
            suggested_price = max(suggested_price, 11.99)
        elif any(w in name_lower for w in ["bruschetta", "salad", "antipast"]):
            suggested_price = max(suggested_price, 7.99)
        elif any(w in name_lower for w in ["biscuit", "scone", "muffin", "cookie", "roll"]):
            suggested_price = max(suggested_price, 4.99)
        elif any(w in name_lower for w in ["cake", "loaf", "bread"]):
            suggested_price = max(suggested_price, 5.99)
        elif any(w in name_lower for w in ["brisket", "ribs", "combo"]):
            suggested_price = max(suggested_price, 16.99)
        elif any(w in name_lower for w in ["plate", "platter", "combo"]):
            suggested_price = max(suggested_price, 9.99)

        # Generate cooking steps based on ingredients
        steps = _generate_steps(name, dish_ings)

        # Build flavor profile from USDA + spice data (normalized 0-10)
        flavor_scores = build_dish_flavor_profile(dish_ingredients)

        # Spice value analysis (for cost ratios)
        spice_data = analyze_spice_value(dish_ingredients)

        return {
            "name": name,
            "slug": slugify(name),
            "cuisine": cuisine,
            "ingredients": dish_ingredients,
            "food_cost": food_cost,
            "suggested_menu_price": suggested_price,
            "menu_price": suggested_price,
            "food_cost_pct": 30.0,
            "steps": steps,
            "nutrition_details": nutrition_details,
            "flavor_scores": flavor_scores,
            "spice_analysis": spice_data if spice_data.get("spices") else None,
            "spice_count": sum(1 for i, _, _ in dish_ings
                              if any(k in i.get("item", "").lower() for k in SPICE_CATEGORIES)),
        }

    def _generate_steps(name, dish_ings):
        """Generate plausible cooking steps from ingredient composition."""
        name_lower = name.lower()

        # --- Detect dish type from name ---
        is_burger = any(w in name_lower for w in ["burger", "smash", "deluxe double"])
        is_fries = any(w in name_lower for w in ["fries", "fry", "tots"])
        is_custard = any(w in name_lower for w in ["custard", "shake", "ice cream"])

        # ---- BURGER STEPS ----
        if is_burger:
            has_cheese = any("cheese" in ing.get("item", "").lower() for ing, _, _ in dish_ings)
            has_bacon = any("bacon" in ing.get("item", "").lower() for ing, _, _ in dish_ings)
            has_onion = any("onion" in ing.get("item", "").lower() for ing, _, _ in dish_ings)
            is_double = "double" in name_lower or "deluxe" in name_lower

            steps = []
            weight = "4oz" if is_double else "2.5oz"
            count = "four" if is_double else "two"
            steps.append(f"Divide ground beef into {count} loose balls ({weight} each). Keep them loosely packed — do not overwork.")
            steps.append("Heat a flat-top griddle or cast iron skillet over high heat until smoking. Add a thin layer of oil.")
            if has_onion:
                steps.append("Toss thinly sliced onions on the griddle to start caramelizing alongside the patties.")
            steps.append(f"Place beef balls on the griddle and immediately smash flat with a sturdy spatula or press. Season with salt and pepper.")
            steps.append("Cook without moving for 2-3 minutes until edges are deeply browned and crispy. Flip once.")
            if has_cheese:
                steps.append("Immediately add cheese to each patty after flipping. Cover briefly or dome with a metal bowl to melt.")
            if has_bacon:
                steps.append("While patties cook, lay bacon strips on the griddle until crispy, about 3-4 minutes per side.")
            steps.append("Toast buns cut-side down on the griddle until golden, about 30-45 seconds.")
            toppings = []
            for ing, _, _ in dish_ings:
                il = ing.get("item", "").lower()
                if any(w in il for w in ["ketchup", "mustard", "mayo", "pickle", "lettuce", "tomato"]):
                    toppings.append(ing.get("display_name", ing.get("item", "")))
            if toppings:
                steps.append(f"Build the burger: bottom bun, patties stacked, then {', '.join(toppings[:4]).lower()}. Top bun.")
            else:
                steps.append("Stack patties on the toasted bun. Add condiments and toppings.")
            steps.append("Serve immediately — smash burgers are best fresh off the griddle.")
            return steps

        # ---- FRIES STEPS ----
        if is_fries:
            return [
                "Heat fryer oil to 375°F (or fill a heavy pot with 2-3 inches of oil).",
                "Spread frozen fries in a single layer — do not overcrowd. Fry in batches if needed.",
                "Fry for 3-4 minutes until golden and crispy. Shake the basket halfway through.",
                "Drain on a wire rack or paper towels. Season immediately with salt while still hot.",
                "Serve in a paper-lined basket or tray.",
            ]

        # ---- CUSTARD / SHAKE STEPS ----
        if is_custard:
            return [
                "Scoop fresh custard into a chilled serving dish or cup.",
                "For shakes: blend custard with whole milk until smooth and thick.",
                "Serve immediately — custard is best at just-set temperature.",
            ]

        # ---- PIZZA STEPS ----
        is_pizza = any(w in name_lower for w in ["pizza", "margherita"])
        if is_pizza:
            return [
                "Preheat oven as hot as it will go (500-550°F). If using a pizza stone, place it in the oven 30-45 minutes ahead.",
                "Stretch the dough by hand on a floured surface into a 12-inch round. Do not use a rolling pin — it pushes out the air bubbles.",
                "Spread crushed tomatoes or sauce evenly, leaving a 1-inch border for the crust.",
                "Add cheese (tear fresh mozzarella by hand for best melt) and any toppings. Less is more.",
                "Slide pizza onto the hot stone or baking sheet. Bake 8-12 minutes until crust is golden and cheese is bubbling with brown spots.",
                "Rest 2-3 minutes before slicing. Finish with fresh basil, olive oil drizzle, or grated parmesan.",
            ]

        # ---- PASTA STEPS ----
        is_pasta = any(w in name_lower for w in ["pasta", "pomodoro", "spaghetti", "penne"])
        if is_pasta:
            return [
                "Bring a large pot of well-salted water to a rolling boil.",
                "While water heats, warm olive oil in a pan over medium heat. Add sliced garlic and cook until fragrant, about 30 seconds.",
                "Add crushed tomatoes, a pinch of salt, and a pinch of red pepper flakes. Simmer 15-20 minutes until thickened.",
                "Cook pasta until just shy of al dente (1 minute less than package time). Reserve 1 cup pasta water before draining.",
                "Toss drained pasta into the sauce. Add pasta water a splash at a time, tossing until the sauce clings to each strand.",
                "Finish with torn basil, grated parmesan, and a drizzle of good olive oil.",
            ]

        # ---- BRUSCHETTA STEPS ----
        is_bruschetta = "bruschetta" in name_lower
        if is_bruschetta:
            return [
                "Dice tomatoes into small cubes. Toss with minced garlic, torn basil, a drizzle of olive oil, and a pinch of salt.",
                "Let the mixture sit 10-15 minutes at room temperature so flavors meld.",
                "Slice bread, brush with olive oil, and grill or broil until golden and crispy on both sides.",
                "Spoon the tomato mixture generously onto each toast. Serve immediately.",
            ]

        # ---- BBQ PLATE STEPS ----
        is_bbq = any(w in name_lower for w in [
            "brisket", "pulled pork", "ribs", "smoked", "combo", "two-meat"])
        if is_bbq:
            steps = []
            if "brisket" in name_lower:
                steps = [
                    "Trim brisket fat cap to about 1/4 inch. Apply dry rub generously on all sides, pressing it in.",
                    "Let the rubbed brisket rest uncovered in the fridge overnight (or at least 1 hour at room temp).",
                    "Set smoker to 225°F with your preferred hardwood (oak, hickory, or mesquite).",
                    "Smoke fat-side up until the bark sets and internal temp hits 165°F (about 1 hour per pound).",
                    "Wrap tightly in butcher paper. Return to smoker until internal temp reaches 200-205°F and a probe slides in like butter.",
                    "Rest wrapped brisket for at least 1 hour (up to 4 hours in a cooler). Slice against the grain.",
                    "Serve on butcher paper with pickles, white bread, and sauce on the side.",
                ]
            elif "pulled pork" in name_lower or "pork" in name_lower:
                steps = [
                    "Apply dry rub generously to the pork shoulder, covering all sides. Rest overnight if time allows.",
                    "Set smoker to 225°F with cherry, apple, or hickory wood.",
                    "Smoke until internal temp reaches 195-205°F — this will take 10-14 hours for a full shoulder.",
                    "The bark should be deep mahogany. If it stalls around 160°F, wrap in foil or butcher paper.",
                    "Rest for 30-60 minutes, then pull apart with forks or bear claws. Mix bark pieces throughout.",
                    "Pile onto bread or a bun, top with sauce and pickles.",
                ]
            elif "ribs" in name_lower:
                steps = [
                    "Remove the membrane from the back of the ribs — slide a butter knife under it and pull with a paper towel.",
                    "Apply dry rub on both sides. Let rest 30 minutes to overnight.",
                    "Set smoker to 250°F. Place ribs bone-side down.",
                    "Follow the 3-2-1 method: 3 hours unwrapped, 2 hours wrapped in foil with a splash of apple cider vinegar, 1 hour unwrapped with sauce.",
                    "Ribs are done when meat pulls back from the bone about 1/4 inch and bends easily when picked up.",
                    "Let rest 10 minutes, then slice between the bones. Serve with extra sauce on the side.",
                ]
            elif "chicken" in name_lower:
                steps = [
                    "Season chicken quarters with dry rub on all sides, including under the skin where possible.",
                    "Set smoker to 275°F (chicken benefits from slightly higher heat than other BBQ).",
                    "Smoke for 2-3 hours until internal temp reaches 175°F in the thickest part of the thigh.",
                    "Optional: finish over high heat or broil for 2-3 minutes to crisp the skin.",
                    "Rest 5 minutes, then serve with sauce on the side.",
                ]
            else:
                steps = [
                    "Apply dry rub generously. Let rest at room temp for 30 minutes.",
                    "Set smoker to 225-250°F with your preferred hardwood.",
                    "Smoke low and slow until internal temperature reaches target doneness.",
                    "Rest before slicing. Serve with sauce, pickles, and white bread.",
                ]
            return steps

        # ---- BAKERY STEPS ----
        is_biscuit = "biscuit" in name_lower
        is_cinnamon_roll = "cinnamon" in name_lower
        is_bread_loaf = "bread" in name_lower and "loaf" in name_lower
        is_cake = "cake" in name_lower or "pound" in name_lower
        is_scone = "scone" in name_lower
        is_cookie = "cookie" in name_lower

        if is_biscuit:
            return [
                "Preheat oven to 425°F. Keep butter ice cold — cube it and put it back in the freezer.",
                "Whisk flour, salt, and baking powder. Cut in frozen butter using a pastry cutter or your fingers until pea-sized chunks remain.",
                "Add cold buttermilk and stir just until dough comes together. Do not overmix.",
                "Turn out onto floured surface. Pat to 1-inch thick, fold in thirds like a letter. Repeat twice for flaky layers.",
                "Cut with a sharp biscuit cutter — press straight down, don't twist. Place touching on a baking sheet.",
                "Bake 12-15 minutes until golden on top. Brush with melted butter immediately out of the oven.",
            ]
        if is_cinnamon_roll:
            return [
                "Warm milk slightly, dissolve yeast and a pinch of sugar in it. Let it bloom 5 minutes until foamy.",
                "Mix flour, sugar, salt, egg, and softened butter into a soft dough. Knead 8-10 minutes until smooth and elastic.",
                "Cover and let rise in a warm spot for 1 hour until doubled.",
                "Roll dough into a large rectangle (~18x12 inches). Spread softened butter, then a generous layer of cinnamon sugar.",
                "Roll up tightly from the long side. Slice into 1.5-inch rounds with a sharp knife or dental floss.",
                "Place in a buttered baking dish, cover, and let rise 30 minutes. Preheat oven to 375°F.",
                "Bake 22-28 minutes until golden. While warm, drizzle with cream cheese icing.",
            ]
        if is_bread_loaf:
            return [
                "Dissolve yeast in warm water (105-110°F) with a pinch of sugar. Let bloom 5-10 minutes.",
                "Combine flour, salt, and any enrichments (butter, milk). Add yeast mixture and stir to form a shaggy dough.",
                "Knead on a floured surface for 10 minutes until smooth, elastic, and passes the windowpane test.",
                "Place in an oiled bowl, cover with a towel. Rise 1-1.5 hours until doubled in size.",
                "Punch down, shape into a loaf, and place in a greased loaf pan. Cover and rise 30-45 minutes.",
                "Preheat oven to 375°F. Score the top with a sharp knife. Bake 30-35 minutes until golden and hollow-sounding when tapped.",
                "Cool on a wire rack at least 15 minutes before slicing.",
            ]
        if is_cake:
            return [
                "Preheat oven to 325°F. Butter and flour the pan (or use a bundt pan for pound cake).",
                "Cream butter and sugar together for 3-5 minutes until light and fluffy — this is the most important step.",
                "Add eggs one at a time, beating well after each. Mix in vanilla.",
                "Fold in flour and salt gently. Do not overmix — stop as soon as you see no dry streaks.",
                "Pour into the prepared pan and smooth the top. Tap the pan on the counter to release air bubbles.",
                "Bake 55-65 minutes until a toothpick in the center comes out clean. Cool in pan 10 minutes, then turn out onto a rack.",
            ]
        if is_scone:
            return [
                "Preheat oven to 400°F. Keep ingredients cold.",
                "Whisk flour, sugar, salt, and baking powder. Cut in cold butter until mixture resembles coarse sand.",
                "Add cold cream and stir just until dough holds together. Fold in any mix-ins (berries, chocolate, etc).",
                "Pat dough into a 1-inch thick circle on a floured surface. Cut into wedges.",
                "Place on a parchment-lined sheet. Brush tops with cream and sprinkle with sugar.",
                "Bake 15-18 minutes until golden. Cool slightly — best served warm.",
            ]
        if is_cookie:
            return [
                "Preheat oven to 350°F. Line baking sheets with parchment.",
                "Cream butter and sugar until light and fluffy, about 3 minutes.",
                "Beat in egg and vanilla until combined. Add flour and salt, mixing just until incorporated.",
                "Scoop rounded tablespoons of dough onto sheets, spacing 2 inches apart.",
                "Bake 10-12 minutes until edges are set but centers still look slightly underdone.",
                "Cool on the sheet 5 minutes (they'll firm up), then transfer to a wire rack.",
            ]

        # ---- SALAD STEPS ----
        if "salad" in name_lower:
            return [
                "Wash and dry greens thoroughly. Tear into bite-sized pieces.",
                "Prepare toppings: dice tomatoes, shave parmesan, slice any vegetables.",
                "Toss greens with a light drizzle of olive oil and a pinch of salt.",
                "Plate and arrange toppings. Finish with freshly cracked pepper and a squeeze of lemon if desired.",
            ]

        # ---- GENERAL / BBQ STEPS (fallback) ----
        steps = []
        protein_items = []
        spice_items = []
        sauce_items = []
        other_items = []

        for ing, amt, unit in dish_ings:
            item_lower = ing.get("item", "").lower()
            is_spice = any(k in item_lower for k in SPICE_CATEGORIES)
            is_protein = any(p in item_lower for p in [
                "brisket", "pork", "chicken", "beef", "steak", "ribs",
                "salmon", "shrimp", "fish", "lamb", "turkey", "sausage",
                "ground", "chuck", "bacon", "ham", "chorizo"])
            is_sauce = any(p in item_lower for p in [
                "vinegar", "mustard", "worcestershire", "soy sauce", "hot sauce"])

            if is_protein:
                protein_items.append(ing.get("display_name", ing.get("item", "")))
            elif is_spice:
                spice_items.append(ing.get("display_name", ing.get("item", "")))
            elif is_sauce:
                sauce_items.append(ing.get("display_name", ing.get("item", "")))
            else:
                other_items.append(ing.get("display_name", ing.get("item", "")))

        if spice_items:
            spice_list = ", ".join(spice_items[:4])
            steps.append(f"Combine the dry rub: {spice_list}. Mix thoroughly in a small bowl.")

        if protein_items:
            protein_list = " and ".join(protein_items[:2])
            steps.append(f"Pat {protein_list} dry with paper towels. Season generously with the spice rub on all sides.")
            steps.append(f"Let the seasoned {protein_items[0].lower()} rest at room temperature for 30 minutes to absorb the flavors.")

            p_lower = protein_items[0].lower()
            if any(x in p_lower for x in ["brisket", "pork butt", "pork shoulder"]):
                steps.append("Cook low and slow at 225°F for 1-1.5 hours per pound, until internal temperature reaches 195-205°F.")
                steps.append("Wrap tightly in butcher paper when bark has set (around 165°F internal). Return to heat.")
                steps.append("Rest for at least 30 minutes before slicing against the grain.")
            elif any(x in p_lower for x in ["ribs", "baby back"]):
                steps.append("Cook at 250°F using the 3-2-1 method: 3 hours unwrapped, 2 hours wrapped, 1 hour sauced.")
                steps.append("Ribs are done when meat pulls back from the bone about 1/4 inch.")
            elif any(x in p_lower for x in ["chicken"]):
                steps.append("Cook at 350°F until internal temperature reaches 165°F, about 35-45 minutes for thighs.")
                steps.append("Let rest 5-10 minutes before serving.")
            elif any(x in p_lower for x in ["salmon", "fish"]):
                steps.append("Cook skin-side down over medium-high heat for 4-5 minutes. Flip and cook 3-4 minutes more until flaky.")
            else:
                steps.append(f"Cook {protein_items[0].lower()} to desired doneness using your preferred method.")

        if sauce_items:
            sauce_list = ", ".join(sauce_items[:3])
            steps.append(f"For the sauce: combine {sauce_list} in a saucepan. Simmer on low heat for 10-15 minutes, stirring occasionally.")

        if other_items:
            steps.append(f"Plate with {', '.join(other_items[:3]).lower()} on the side.")

        if not steps:
            steps = ["Season ingredients according to the spice blend ratios.",
                     "Cook using your preferred method.",
                     "Plate and serve."]

        return steps

    # Helper: find an ingredient by keyword match
    def find_ing(category_list, *keywords, fallback=True):
        """Find first ingredient matching any keyword.
        If fallback=True (default), returns first item when no match.
        If fallback=False, returns None when no match."""
        for ing in category_list:
            item_lower = ing.get("item", "").lower()
            if any(k in item_lower for k in keywords):
                return ing
        return (category_list[0] if category_list else None) if fallback else None

    # =====================================================
    # BURGER RESTAURANT TEMPLATE
    # =====================================================
    if restaurant_type == "burger":
        patty = find_ing(proteins, "ground", "chuck", "patty", "beef")
        bun = find_ing(buns_bread, "bun", "brioche")
        cheese = find_ing(dairy, "cheese", "american", "cheddar")
        bacon_ing = find_ing(proteins, "bacon")
        lettuce = find_ing(produce, "lettuce", "iceberg")
        tomato = find_ing(produce, "tomato")
        onion = find_ing(produce, "onion")
        pickle = find_ing(produce, "pickle", "dill")
        ketchup = find_ing(sauces, "ketchup")
        mustard = find_ing(sauces, "mustard")
        mayo = find_ing(sauces, "mayo")
        fries = find_ing(fries_sides, "fries", "fry", "tots")
        salt = find_ing(spices, "salt", "kosher")
        pepper = find_ing(spices, "pepper", "black")
        oil = find_ing(staples + sauces, "oil", "canola", "vegetable")

        # 1. Classic Smash Burger — the signature item
        if patty:
            ings = [(patty, 0.33, "lb")]  # two 2.6oz smash patties
            if bun: ings.append((bun, 1, "ea"))
            if salt: ings.append((salt, 0.01, "lb"))  # pinch
            if pepper: ings.append((pepper, 0.005, "lb"))  # pinch
            if oil: ings.append((oil, 0.02, "lb"))  # griddle oil
            if onion: ings.append((onion, 0.1, "lb"))  # griddled onions
            if pickle: ings.append((pickle, 0.05, "lb"))  # 3-4 slices
            if ketchup: ings.append((ketchup, 0.03, "lb"))
            if mustard: ings.append((mustard, 0.02, "lb"))
            dishes.append(make_dish("Classic Smash Burger", ings, "American"))

        # 2. Smash Cheeseburger
        if patty and cheese:
            ings = [(patty, 0.33, "lb")]
            if bun: ings.append((bun, 1, "ea"))
            ings.append((cheese, 0.065, "lb"))  # 2 slices (~1oz each)
            if salt: ings.append((salt, 0.01, "lb"))
            if pepper: ings.append((pepper, 0.005, "lb"))
            if oil: ings.append((oil, 0.02, "lb"))
            if onion: ings.append((onion, 0.1, "lb"))
            if pickle: ings.append((pickle, 0.05, "lb"))
            if ketchup: ings.append((ketchup, 0.03, "lb"))
            if mustard: ings.append((mustard, 0.02, "lb"))
            dishes.append(make_dish("Smash Cheeseburger", ings, "American"))

        # 3. Bacon Cheeseburger
        if patty and cheese and bacon_ing:
            ings = [(patty, 0.33, "lb")]
            if bun: ings.append((bun, 1, "ea"))
            ings.append((cheese, 0.065, "lb"))
            ings.append((bacon_ing, 0.1, "lb"))  # 2-3 slices
            if salt: ings.append((salt, 0.01, "lb"))
            if pepper: ings.append((pepper, 0.005, "lb"))
            if oil: ings.append((oil, 0.02, "lb"))
            if lettuce: ings.append((lettuce, 0.05, "lb"))
            if tomato: ings.append((tomato, 0.1, "lb"))  # 1-2 slices
            if mayo: ings.append((mayo, 0.03, "lb"))
            dishes.append(make_dish("Bacon Cheeseburger", ings, "American"))

        # 4. The Deluxe — everything on it
        if patty:
            ings = [(patty, 0.5, "lb")]  # double patty
            if bun: ings.append((bun, 1, "ea"))
            if cheese: ings.append((cheese, 0.13, "lb"))  # double cheese
            if bacon_ing: ings.append((bacon_ing, 0.1, "lb"))
            if lettuce: ings.append((lettuce, 0.05, "lb"))
            if tomato: ings.append((tomato, 0.1, "lb"))
            if onion: ings.append((onion, 0.1, "lb"))
            if pickle: ings.append((pickle, 0.05, "lb"))
            if ketchup: ings.append((ketchup, 0.03, "lb"))
            if mustard: ings.append((mustard, 0.02, "lb"))
            if mayo: ings.append((mayo, 0.03, "lb"))
            if salt: ings.append((salt, 0.01, "lb"))
            if pepper: ings.append((pepper, 0.005, "lb"))
            dishes.append(make_dish("The Deluxe Double", ings, "American"))

        # 5. Crinkle Fries / Side
        if fries:
            ings = [(fries, 0.4, "lb")]  # ~6oz serving
            if oil: ings.append((oil, 0.05, "lb"))
            if salt: ings.append((salt, 0.005, "lb"))
            dishes.append(make_dish("Crinkle Cut Fries", ings, "American"))

        # 6. Custard / Shake (if they have custard mix)
        custard = find_ing(dairy, "custard", "ice cream", "shake")
        if custard:
            ings = [(custard, 0.25, "lb")]
            dishes.append(make_dish("Fresh Custard", ings, "American"))

    # =====================================================
    # BBQ RESTAURANT TEMPLATE
    # =====================================================
    elif restaurant_type == "bbq":
        salt = find_ing(spices, "salt", "kosher")
        pepper = find_ing(spices, "pepper", "black")
        paprika = find_ing(spices, "paprika", "smoked")
        garlic_p = find_ing(spices, "garlic powder", "garlic")
        onion_p = find_ing(spices, "onion powder", "onion")
        brown_sugar = find_ing(spices + staples, "brown sugar", "sugar")
        cayenne = find_ing(spices, "cayenne", "chili")
        vinegar = find_ing(sauces, "vinegar", "cider")
        mustard_s = find_ing(sauces, "mustard")
        worchest = find_ing(sauces, "worcestershire")
        bread = find_ing(buns_bread + staples, "bread", "bun", "roll")
        pickles = find_ing(produce, "pickle", "dill")

        def _bbq_rub():
            """Standard rub ingredients for BBQ."""
            rub = []
            if paprika: rub.append((paprika, 0.02, "lb"))   # ~1 tbsp
            if garlic_p: rub.append((garlic_p, 0.01, "lb"))
            if onion_p: rub.append((onion_p, 0.01, "lb"))
            if brown_sugar: rub.append((brown_sugar, 0.02, "lb"))
            if salt: rub.append((salt, 0.01, "lb"))
            if pepper: rub.append((pepper, 0.005, "lb"))
            if cayenne: rub.append((cayenne, 0.003, "lb"))
            return rub

        def _bbq_sauce_ings():
            """Sauce components for BBQ."""
            s = []
            if vinegar: s.append((vinegar, 0.06, "lb"))    # ~1 oz
            if mustard_s: s.append((mustard_s, 0.03, "lb"))
            if worchest: s.append((worchest, 0.03, "lb"))
            return s

        # Find key proteins
        brisket = find_ing(proteins, "brisket", fallback=False)
        pork = find_ing(proteins, "pork butt", "pork shoulder", "pork", fallback=False)
        ribs = find_ing(proteins, "ribs", "baby back", "spare", fallback=False)
        chicken = find_ing(proteins, "chicken", fallback=False)

        # 1. Smoked Brisket Plate
        if brisket:
            ings = [(brisket, 0.75, "lb")]  # 12oz sliced serving
            ings.extend(_bbq_rub())
            ings.extend(_bbq_sauce_ings())
            if bread: ings.append((bread, 2, "ea"))   # 2 slices white bread
            if pickles: ings.append((pickles, 0.06, "lb"))
            dishes.append(make_dish("Smoked Brisket Plate", ings, "American BBQ"))

        # 2. Pulled Pork Sandwich
        if pork:
            ings = [(pork, 0.5, "lb")]   # 8oz pulled
            ings.extend(_bbq_rub())
            ings.extend(_bbq_sauce_ings())
            if bread: ings.append((bread, 1, "ea"))  # bun/bread
            if pickles: ings.append((pickles, 0.06, "lb"))
            dishes.append(make_dish("Pulled Pork Sandwich", ings, "American BBQ"))

        # 3. Rack of Ribs (half rack)
        if ribs:
            ings = [(ribs, 0.75, "lb")]  # half rack
            ings.extend(_bbq_rub())
            ings.extend(_bbq_sauce_ings())
            if bread: ings.append((bread, 2, "ea"))
            dishes.append(make_dish("Half Rack of Ribs", ings, "American BBQ"))

        # 4. Smoked Chicken (quarter)
        if chicken:
            ings = [(chicken, 0.5, "lb")]  # quarter bird
            ings.extend(_bbq_rub())
            ings.extend(_bbq_sauce_ings())
            dishes.append(make_dish("Smoked Chicken Quarter", ings, "American BBQ"))

        # 5. Two-Meat Combo
        combo_meats = [m for m in [brisket, pork, ribs] if m]
        if len(combo_meats) >= 2:
            ings = [(combo_meats[0], 0.4, "lb"), (combo_meats[1], 0.4, "lb")]
            ings.extend(_bbq_rub())
            ings.extend(_bbq_sauce_ings())
            if bread: ings.append((bread, 2, "ea"))
            if pickles: ings.append((pickles, 0.06, "lb"))
            dishes.append(make_dish("Two-Meat Combo Plate", ings, "American BBQ"))

        # 6. Sides
        if fries_sides:
            for side in fries_sides[:2]:
                s_name = side.get("display_name", "Side")
                clean_name = re.sub(r'\s*\d+\s*(lb|oz|ct).*$', '', s_name, flags=re.IGNORECASE).strip()
                ings = [(side, 0.35, "lb")]
                if salt: ings.append((salt, 0.005, "lb"))
                dishes.append(make_dish(clean_name, ings, "American BBQ"))

    # =====================================================
    # PIZZA / ITALIAN RESTAURANT TEMPLATE
    # =====================================================
    elif restaurant_type == "pizza":
        mozz = find_ing(dairy, "mozzarella")
        parm = find_ing(dairy, "parmesan", "parmigiano", "reggiano", "pecorino")
        tomato = find_ing(produce + staples, "tomato", "san marzano", "marinara")
        basil = find_ing(produce + spices, "basil")
        garlic = find_ing(produce + spices, "garlic")
        oregano = find_ing(spices, "oregano")
        olive_oil = find_ing(sauces + staples, "olive oil", "oil")
        red_pepper = find_ing(spices, "red pepper", "pepper flake", "chili flake")
        salt = find_ing(spices, "salt", "kosher")
        pepper = find_ing(spices, "pepper", "black")
        flour = find_ing(staples, "flour", "semolina")

        # Dough base (used per pizza)
        def _dough_base():
            d = []
            if flour: d.append((flour, 0.5, "lb"))      # ~8oz dough ball
            if olive_oil: d.append((olive_oil, 0.03, "lb"))  # drizzle
            if salt: d.append((salt, 0.008, "lb"))       # pinch
            return d

        # 1. Margherita
        if mozz and tomato:
            ings = _dough_base()
            ings.append((tomato, 0.25, "lb"))   # ~4oz crushed tomato
            ings.append((mozz, 0.375, "lb"))     # 6oz fresh mozz
            if basil: ings.append((basil, 0.01, "lb"))
            if olive_oil and (olive_oil,) not in [(i,) for i, _, _ in ings]:
                pass  # already in dough
            dishes.append(make_dish("Margherita Pizza", ings, "Italian"))

        # 2. Classic Cheese
        if mozz:
            ings = _dough_base()
            if tomato: ings.append((tomato, 0.25, "lb"))
            ings.append((mozz, 0.5, "lb"))       # extra cheese
            if parm: ings.append((parm, 0.06, "lb"))  # ~1oz grated
            if oregano: ings.append((oregano, 0.005, "lb"))
            dishes.append(make_dish("Classic Cheese Pizza", ings, "Italian"))

        # 3. Garlic & Oil (Aglio e Olio pizza)
        if olive_oil and garlic:
            ings = _dough_base()
            ings.append((garlic, 0.06, "lb"))    # 4-5 cloves
            if olive_oil: ings.append((olive_oil, 0.06, "lb"))  # generous
            if parm: ings.append((parm, 0.06, "lb"))
            if red_pepper: ings.append((red_pepper, 0.003, "lb"))
            dishes.append(make_dish("Garlic & Oil Pizza", ings, "Italian"))

        # 4. Pasta Pomodoro (if flour for fresh pasta)
        if flour and tomato:
            ings = [(flour, 0.25, "lb")]   # ~4oz fresh pasta
            ings.append((tomato, 0.375, "lb"))  # 6oz sauce
            if garlic: ings.append((garlic, 0.03, "lb"))
            if basil: ings.append((basil, 0.01, "lb"))
            if olive_oil: ings.append((olive_oil, 0.03, "lb"))
            if parm: ings.append((parm, 0.06, "lb"))
            if salt: ings.append((salt, 0.008, "lb"))
            if pepper: ings.append((pepper, 0.003, "lb"))
            dishes.append(make_dish("Pasta Pomodoro", ings, "Italian"))

        # 5. Bruschetta (antipasto)
        if tomato and garlic:
            ings = []
            if tomato: ings.append((tomato, 0.25, "lb"))
            if garlic: ings.append((garlic, 0.02, "lb"))
            if basil: ings.append((basil, 0.01, "lb"))
            if olive_oil: ings.append((olive_oil, 0.03, "lb"))
            if salt: ings.append((salt, 0.005, "lb"))
            dishes.append(make_dish("Bruschetta", ings, "Italian"))

        # 6. House Salad
        salad_green = find_ing(produce, "lettuce", "arugula", "greens", "spinach")
        if salad_green:
            ings = [(salad_green, 0.15, "lb")]
            if tomato: ings.append((tomato, 0.1, "lb"))
            if olive_oil: ings.append((olive_oil, 0.03, "lb"))
            if parm: ings.append((parm, 0.03, "lb"))
            dishes.append(make_dish("House Salad", ings, "Italian"))

    # =====================================================
    # BAKERY TEMPLATE
    # =====================================================
    elif restaurant_type == "bakery":
        flour_ing = find_ing(staples, "all purpose", "flour", "bread flour")
        bread_flour = find_ing(staples, "bread flour")
        butter = find_ing(dairy + staples, "butter")
        eggs = find_ing(dairy + staples, "egg", "eggs")
        sugar = find_ing(staples + spices, "sugar", "granulated")
        milk = find_ing(dairy, "milk", "whole milk")
        cream = find_ing(dairy, "cream", "heavy cream")
        vanilla = find_ing(spices + staples, "vanilla")
        yeast = find_ing(staples, "yeast")
        salt = find_ing(spices, "salt", "kosher")
        cinnamon = find_ing(spices, "cinnamon")
        nutmeg = find_ing(spices, "nutmeg")

        # 1. Buttermilk Biscuits
        if flour_ing and butter:
            ings = [(flour_ing, 0.25, "lb")]     # ~1 cup flour
            ings.append((butter, 0.125, "lb"))    # 1 stick = 4oz = 0.25lb, half stick
            if salt: ings.append((salt, 0.005, "lb"))
            if milk: ings.append((milk, 0.25, "lb"))  # ~1/2 cup
            dishes.append(make_dish("Buttermilk Biscuits (4 ct)", ings, "American"))

        # 2. Cinnamon Rolls (if yeast + cinnamon)
        if flour_ing and yeast and cinnamon and butter:
            ings = [(flour_ing, 0.375, "lb")]    # ~1.5 cups
            ings.append((butter, 0.19, "lb"))     # 3oz (dough + filling)
            ings.append((sugar, 0.125, "lb"))     # ~1/4 cup
            ings.append((cinnamon, 0.01, "lb"))
            if yeast: ings.append((yeast, 0.01, "lb"))
            if eggs: ings.append((eggs, 0.12, "lb"))  # 1 egg ~2oz
            if milk: ings.append((milk, 0.19, "lb"))
            if vanilla: ings.append((vanilla, 0.01, "lb"))
            if salt: ings.append((salt, 0.005, "lb"))
            dishes.append(make_dish("Cinnamon Rolls (6 ct)", ings, "American"))

        # 3. Fresh Bread Loaf
        if (bread_flour or flour_ing) and yeast:
            f = bread_flour or flour_ing
            ings = [(f, 0.5, "lb")]              # ~2 cups
            if yeast: ings.append((yeast, 0.01, "lb"))
            if salt: ings.append((salt, 0.008, "lb"))
            if butter: ings.append((butter, 0.06, "lb"))
            if milk: ings.append((milk, 0.25, "lb"))
            dishes.append(make_dish("Fresh Bread Loaf", ings, "Artisan"))

        # 4. Vanilla Pound Cake
        if flour_ing and butter and eggs and sugar:
            ings = [(flour_ing, 0.25, "lb")]
            ings.append((butter, 0.25, "lb"))     # 1 stick
            ings.append((sugar, 0.19, "lb"))      # ~3/4 cup
            ings.append((eggs, 0.25, "lb"))       # 2 eggs
            if vanilla: ings.append((vanilla, 0.01, "lb"))
            if salt: ings.append((salt, 0.005, "lb"))
            dishes.append(make_dish("Vanilla Pound Cake (slice)", ings, "American"))

        # 5. Whipped Cream Scones
        if flour_ing and cream:
            ings = [(flour_ing, 0.25, "lb")]
            ings.append((cream, 0.25, "lb"))      # ~1/2 cup
            ings.append((sugar, 0.06, "lb"))      # 2 tbsp
            if butter: ings.append((butter, 0.06, "lb"))
            if salt: ings.append((salt, 0.005, "lb"))
            if vanilla: ings.append((vanilla, 0.008, "lb"))
            dishes.append(make_dish("Cream Scones (4 ct)", ings, "British"))

        # 6. Classic Sugar Cookies
        if flour_ing and butter and sugar and eggs:
            ings = [(flour_ing, 0.19, "lb")]
            ings.append((butter, 0.125, "lb"))
            ings.append((sugar, 0.125, "lb"))
            ings.append((eggs, 0.12, "lb"))
            if vanilla: ings.append((vanilla, 0.008, "lb"))
            if salt: ings.append((salt, 0.003, "lb"))
            dishes.append(make_dish("Sugar Cookies (dozen)", ings, "American"))

    # =====================================================
    # GENERAL / FALLBACK TEMPLATE
    # =====================================================
    else:
        for protein in proteins:
            p_name = protein.get("display_name", protein.get("item", "Protein"))
            clean_name = re.sub(r'\s*\d+\s*(lb|oz|ct).*$', '', p_name, flags=re.IGNORECASE).strip()
            dish_ings = [(protein, 0.5, "lb")]
            for s in spices[:3]:
                dish_ings.append((s, 0.02, "lb"))
            if sauces:
                dish_ings.append((sauces[0], 0.06, "lb"))
            if produce:
                dish_ings.append((produce[0], 0.15, "lb"))
            dishes.append(make_dish(f"{clean_name} Plate", dish_ings))

        # Side dish
        if dairy or produce:
            side_main = (dairy + produce)[0]
            s_name = side_main.get("display_name", "Side")
            clean_name = re.sub(r'\s*\d+\s*(lb|oz|ct).*$', '', s_name, flags=re.IGNORECASE).strip()
            ings = [(side_main, 0.25, "lb")]
            if spices: ings.append((spices[0], 0.005, "lb"))
            dishes.append(make_dish(f"{clean_name} Side", ings))

    # Final fallback
    if not dishes:
        dish_ings = []
        for ing in inventory_items[:6]:
            dish_ings.append((ing, 0.25, "unit"))
        dishes.append(make_dish("Chef's Special", dish_ings))

    # Save dishes to recipe YAMLs
    recipe_slugs = []
    for dish in dishes:
        recipe_slug = f"{slug}-{dish['slug']}"
        recipe = {
            "slug": recipe_slug,
            "title": dish["name"],
            "cuisine": dish["cuisine"],
            "type": "dish",
            "source": {
                "type": "auto_generated",
                "tenant": slug,
                "from_inventory": f"{slug}-inventory",
                "created_at": datetime.now(tz=__import__("datetime").timezone.utc).isoformat(),
                "note": "Auto-generated from vendor inventory. Restaurant should refine.",
            },
            "ingredients": dish["ingredients"],
            "menu_pricing": {
                "food_cost": dish["food_cost"],
                "suggested_menu_price": dish["suggested_menu_price"],
                "menu_price": dish["menu_price"],
                "food_cost_pct": dish["food_cost_pct"],
            },
            "steps": dish.get("steps", ["Season ingredients.", "Cook to desired doneness.", "Plate and serve."]),
            "techniques": [],
            "flavor_profile": {
                "scores": dish.get("flavor_scores", {}),
                "matched_products": [],
            },
            "enrichment": {
                "nutrition": {
                    "usda_matched": f"{sum(1 for i in dish['ingredients'] if i.get('usda_fdc_id'))}/{len(dish['ingredients'])} ingredients matched",
                    "details": dish.get("nutrition_details", {}),
                },
            },
            "spice_analysis": dish.get("spice_analysis"),
            "history": {"version": "1.0"},
        }
        os.makedirs(RECIPE_DIR, exist_ok=True)
        path = os.path.join(RECIPE_DIR, f"{recipe_slug}.yaml")
        with open(path, "w") as f:
            yaml.dump(recipe, f, default_flow_style=False, allow_unicode=True)
        recipe_slugs.append(recipe_slug)

    return dishes, recipe_slugs


def create_recipe_yaml(slug, title, cuisine, ingredients_enriched, steps):
    """Create an enriched recipe YAML from submitted data.
    Used for full recipe submissions (not invoices/price lists)."""
    recipe_slug = slugify(title) if title else f"{slug}-ingredients"

    # Build nutrition details
    nutrition_details = {}
    for ing in ingredients_enriched:
        if ing.get("usda_fdc_id"):
            nutri = get_nutrition(ing["usda_fdc_id"])
            if nutri:
                nutrition_details[ing["display_name"]] = {
                    "fdc_id": ing["usda_fdc_id"],
                    "per_100g": nutri,
                }

    recipe = {
        "slug": recipe_slug,
        "title": title or f"{slug} Recipe",
        "cuisine": cuisine or "General",
        "type": "dish",
        "source": {
            "type": "partner_onboard",
            "tenant": slug,
            "ingested_at": datetime.now(tz=__import__("datetime").timezone.utc).isoformat(),
        },
        "ingredients": ingredients_enriched,
        "steps": steps or ["(Add your preparation steps here)"],
        "techniques": [],
        "flavor_profile": {
            "scores": {},
            "matched_products": [],
        },
        "enrichment": {
            "nutrition": {
                "usda_matched": f"{sum(1 for i in ingredients_enriched if i.get('usda_fdc_id'))}/{len(ingredients_enriched)} ingredients matched",
                "details": nutrition_details,
            },
        },
        "history": {
            "version": "1.0",
        },
    }

    os.makedirs(RECIPE_DIR, exist_ok=True)
    path = os.path.join(RECIPE_DIR, f"{recipe_slug}.yaml")
    with open(path, "w") as f:
        yaml.dump(recipe, f, default_flow_style=False, allow_unicode=True)

    return recipe_slug, recipe


def process_onboard(data):
    """Full onboard pipeline: form data → tenant + recipe → site."""
    steps = []

    # Validate
    if not data.get("name"):
        return {"error": "Name is required."}

    # 1. Generate token
    token = generate_token()
    steps.append({"ok": True, "msg": f"Generated access token (no cookies)"})

    # 2. Create tenant (YAML + database)
    slug, tenant_path = create_tenant_yaml(data, token)
    try:
        from app.db import DB
        db = DB()
        # Read back the YAML we just wrote and upsert into DB
        tenant_config = yaml.safe_load(open(tenant_path))
        db.upsert_tenant(tenant_config)
    except Exception as e:
        steps.append({"ok": False, "msg": f"DB sync warning: {e}"})
    steps.append({"ok": True, "msg": f"Created tenant config: {slug}"})

    # 3. Parse ingredients
    syn_map = load_normalization()
    submit_type = data.get("submit_type", "paste")

    if submit_type == "recipe":
        raw_ings, raw_steps = parse_recipe(data.get("recipe_text", ""))
        title = data.get("recipe_title", "")
        cuisine = data.get("cuisine", "")
    elif submit_type == "pricelist":
        raw_ings = parse_ingredients(data.get("ingredients", ""))
        raw_steps = []
        title = f"{data['name']} Price List"
        cuisine = ""
    else:
        raw_ings = parse_ingredients(data.get("ingredients", ""))
        raw_steps = []
        title = f"{data['name']} Ingredients"
        cuisine = ""

    steps.append({"ok": True, "msg": f"Parsed {len(raw_ings)} ingredients"})

    # 4. Match each ingredient to USDA (preserve ALL parsed data)
    enriched = []
    matched_count = 0
    total_cost = 0.0
    has_pricing = False

    for ing in raw_ings:
        result = match_usda(ing["item"], syn_map)
        # Carry forward everything the parser found
        result["amount"] = ing.get("amount", "")
        result["unit"] = ing.get("unit", "")
        result["original"] = ing.get("original", "")
        result["display_name"] = ing.get("display_name", ing["item"].title())
        if ing.get("vendor_price") is not None:
            result["vendor_price"] = ing["vendor_price"]
            total_cost += ing["vendor_price"]
            has_pricing = True
        if ing.get("package_size"):
            result["package_size"] = ing["package_size"]
            result["package_unit"] = ing.get("package_unit", "")
        if ing.get("unit_price") is not None:
            result["unit_price"] = ing["unit_price"]
            result["unit_price_label"] = ing["unit_price_label"]
        enriched.append(result)
        if result.get("usda_fdc_id"):
            matched_count += 1

    steps.append({"ok": True, "msg": f"USDA matched: {matched_count}/{len(enriched)} ingredients"})

    if has_pricing:
        steps.append({"ok": True, "msg": f"Vendor pricing: ${total_cost:.2f} total across {sum(1 for e in enriched if e.get('vendor_price'))} items"})

    # 5. Get nutrition data + build cost analysis
    nutri_count = 0
    for ing in enriched:
        if ing.get("usda_fdc_id"):
            n = get_nutrition(ing["usda_fdc_id"])
            if n:
                nutri_count += 1

    steps.append({"ok": True, "msg": f"Nutrition data: {nutri_count} ingredients with cal/pro/fat/carb"})

    # 5b. Calculate taste-to-cost ratios for spice ingredients
    spice_analysis = analyze_spice_value(enriched)
    if spice_analysis.get("spices"):
        steps.append({"ok": True, "msg": f"Spice analysis: {len(spice_analysis['spices'])} spices profiled, best value: {spice_analysis.get('best_value', 'n/a')}"})

    # 6. Create inventory + dishes (or recipe if it's a full recipe submission)
    recipe_slugs = []

    if submit_type in ("pricelist", "paste") and not raw_steps:
        # This is a vendor invoice or ingredient list → inventory + auto-generated dishes
        inv_slug, inventory = create_inventory_yaml(slug, enriched)
        steps.append({"ok": True, "msg": f"Created vendor inventory: {inv_slug} ({inventory['summary']['total_items']} items, ${inventory['summary']['total_cost']:.2f})"})

        dishes, recipe_slugs = create_dishes_from_inventory(slug, enriched)
        steps.append({"ok": True, "msg": f"Generated {len(dishes)} menu dishes from inventory"})
        for d in dishes:
            steps.append({"ok": True, "msg": f"  → {d['name']}: food cost ${d['food_cost']:.2f} → suggested price ${d['suggested_menu_price']:.2f}"})
    else:
        # This is an actual recipe with steps → create as a dish
        recipe_slug, recipe = create_recipe_yaml(slug, title, cuisine, enriched, raw_steps)
        recipe_slugs = [recipe_slug]
        steps.append({"ok": True, "msg": f"Created dish: {recipe_slug}"})

    # 7. Update tenant config with recipe list + inventory ref
    tenant_config = yaml.safe_load(open(os.path.join(TENANT_DIR, f"{slug}.yaml")))
    tenant_config["recipes"] = recipe_slugs
    if submit_type in ("pricelist", "paste") and not raw_steps:
        tenant_config["inventory"] = f"{slug}-inventory"
    with open(os.path.join(TENANT_DIR, f"{slug}.yaml"), "w") as f:
        yaml.dump(tenant_config, f, default_flow_style=False, allow_unicode=True)

    steps.append({"ok": True, "msg": f"Updated tenant config with {len(recipe_slugs)} dishes"})

    # 8. Menu engineering analysis
    menu_analysis = None
    try:
        from app.menu import analyze_menu, render_menu_wireframe
        menu_analysis = analyze_menu(slug)
        if menu_analysis and not menu_analysis.get("error"):
            summary = menu_analysis.get("summary", {})
            steps.append({"ok": True, "msg": f"Menu analysis: {summary.get('stars', 0)} stars, {summary.get('puzzles', 0)} puzzles, {summary.get('plowhorses', 0)} plowhorses, {summary.get('dogs', 0)} dogs"})

            # Generate menu wireframe HTML
            wireframe_html = render_menu_wireframe(menu_analysis, tenant_config.get("colors"))
            output_dir = os.path.join(BASE_DIR, "output", "tenants", slug)
            os.makedirs(output_dir, exist_ok=True)
            wireframe_path = os.path.join(output_dir, "menu.html")
            with open(wireframe_path, "w") as f:
                f.write(wireframe_html)
            steps.append({"ok": True, "msg": f"Menu wireframe generated at /t/{slug}/menu.html"})

            # Add recommendations to tenant config
            if menu_analysis.get("recommendations"):
                tenant_config["menu_recommendations"] = menu_analysis["recommendations"]
                with open(os.path.join(TENANT_DIR, f"{slug}.yaml"), "w") as f:
                    yaml.dump(tenant_config, f, default_flow_style=False, allow_unicode=True)
    except Exception as e:
        steps.append({"ok": False, "msg": f"Menu analysis error: {str(e)}"})

    # 9. Blend ratio calculation (what goes in their custom spice bag)
    blend_spec = None
    try:
        blend_data = aggregate_demand_with_ratios()
        tenant_blends = []
        for opp in blend_data.get("opportunities", []):
            if slug in opp.get("tenants", []) and opp.get("threshold_met"):
                ratio_info = opp.get("tenant_ratios", {}).get(slug, {})
                tenant_blends.append({
                    "blend_name": opp["blend_name"],
                    "spices": opp["blend"],
                    "your_ratio": ratio_info.get("ratios", opp.get("default_ratio", {})),
                    "ratio_source": ratio_info.get("ratio_source", "default"),
                    "shared_with": len(opp["tenants"]),
                })
        if tenant_blends:
            blend_spec = tenant_blends
            steps.append({"ok": True, "msg": f"Custom blend specs: {len(tenant_blends)} production-ready blends matched"})
    except Exception as e:
        steps.append({"ok": False, "msg": f"Blend analysis error: {str(e)}"})

    # 10. Build tenant site
    try:
        from app import publish
        all_recipes = publish.load_all_recipes()
        usda = publish.get_usda_stats()
        publish.build_tenant(tenant_config, all_recipes, usda)
        steps.append({"ok": True, "msg": f"Built branded site at /t/{slug}/"})
    except Exception as e:
        steps.append({"ok": False, "msg": f"Site generation error: {str(e)}"})

    # 11. Automated review — audit data quality + generate feedback
    review_result = None
    try:
        from app.review import review_tenant as run_review, render_review_page
        review_result = run_review(slug)
        if review_result and not review_result.get("error"):
            score = review_result.get("overall_score", 0)
            grade = review_result.get("overall_grade", review_result.get("grade", "?"))
            steps.append({"ok": True, "msg": f"Platform review: {score}/100 (Grade: {grade})"})

            # Generate review.html in tenant output
            review_html = render_review_page(review_result)
            output_dir = os.path.join(BASE_DIR, "output", "tenants", slug)
            os.makedirs(output_dir, exist_ok=True)
            with open(os.path.join(output_dir, "review.html"), "w") as f:
                f.write(review_html)
            steps.append({"ok": True, "msg": f"Review report at /t/{slug}/review.html"})

            # Surface top 3 priority actions
            top_actions = review_result.get("priority_actions", [])[:3]
            for i, act in enumerate(top_actions, 1):
                steps.append({"ok": True, "msg": f"  → Action {i}: {act['action']}"})
        else:
            steps.append({"ok": False, "msg": f"Review skipped: {review_result.get('error', 'unknown')}"})
    except Exception as e:
        steps.append({"ok": False, "msg": f"Review engine error: {str(e)}"})

    # Read back the final tenant config to get domain info
    final_config = yaml.safe_load(open(os.path.join(TENANT_DIR, f"{slug}.yaml")))

    return {
        "slug": slug,
        "token": token,
        "domain": final_config.get("domain"),         # e.g. bigreds.howtocookathome.com
        "url_path": final_config.get("url_path"),      # e.g. /t/big-reds-bbq/
        "type": final_config.get("type"),
        "tier": final_config.get("tier", "seed"),
        "steps": steps,
        "recipe_count": 1,
        "ingredient_count": len(enriched),
        "matched_count": matched_count,
        "menu_analysis": menu_analysis if menu_analysis and not menu_analysis.get("error") else None,
        "blend_spec": blend_spec,
        "review": review_result,
    }
