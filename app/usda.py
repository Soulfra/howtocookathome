#!/usr/bin/env python3
"""
USDA_IMPORT.PY — Build the Foundation Layer
=============================================
Imports USDA FoodData Central into a SQLite database that serves
as the canonical ingredient list for the entire platform.

Schema matches the USDA field descriptions (Download_Field_Descriptions_Oct2020.pdf):
  food → food_category        (food groups)
  food → food_nutrient → nutrient  (nutrition data per 100g)
  food → food_portion          (serving sizes)
  food → input_food            (ingredient relationships)

Two modes:
  1. Full CSV import  — when USDA files exist in data/usda/
  2. Demo foundation  — ~45 real USDA foods with known-good data

SETUP:
  1. Go to https://fdc.nal.usda.gov/download-datasets/
  2. Download "Foundation Foods" CSV
  3. Unzip into data/usda/
  4. Run: python usda_import.py
"""

import csv
import json
import os
import sys
import sqlite3

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data", "usda")
CONTENT_DIR = os.path.join(BASE_DIR, "content")
DB_PATH = os.path.join(DATA_DIR, "foundation.db")


# === NUTRIENT CODES ===
# From SR-Legacy NUTR_DEF + FoodData Central nutrient table
# These are the nutrient_nbr values we care about for cooking
NUTRIENT_MAP = {
    # nutrient_nbr → our column name
    "203": "protein_g",
    "204": "fat_g",
    "205": "carbs_g",
    "208": "calories_kcal",
    "269": "sugar_g",
    "291": "fiber_g",
    "301": "calcium_mg",
    "303": "iron_mg",
    "306": "potassium_mg",
    "307": "sodium_mg",
    "401": "vitamin_c_mg",
    "601": "cholesterol_mg",
    "606": "saturated_fat_g",
    # FDC uses different IDs (1003 instead of 203, etc.)
    # We handle both formats in the import
    "1003": "protein_g",
    "1004": "fat_g",
    "1005": "carbs_g",
    "1008": "calories_kcal",
    "1079": "fiber_g",
    "1087": "calcium_mg",
    "1089": "iron_mg",
    "1092": "potassium_mg",
    "1093": "sodium_mg",
    "1162": "vitamin_c_mg",
    "1253": "cholesterol_mg",
    "1258": "saturated_fat_g",
}

# Cooking-relevant food categories (skip baby food, supplements, etc.)
COOKING_CATEGORIES = {
    "Dairy and Egg Products",
    "Spices and Herbs",
    "Fats and Oils",
    "Poultry Products",
    "Soups, Sauces, and Gravies",
    "Sausages and Luncheon Meats",
    "Fruits and Fruit Juices",
    "Pork Products",
    "Vegetables and Vegetable Products",
    "Nut and Seed Products",
    "Beef Products",
    "Beverages",
    "Finfish and Shellfish Products",
    "Legumes and Legume Products",
    "Lamb, Veal, and Game Products",
    "Baked Products",
    "Sweets",
    "Cereal Grains and Pasta",
    "Snacks",
    "Restaurant Foods",
    "Meals, Entrees, and Side Dishes",
    "American Indian/Alaska Native Foods",
}

# Category → flavor dimension mapping for the profiling engine
CATEGORY_FLAVORS = {
    "Spices and Herbs": ["aromatic"],
    "Fats and Oils": ["fat"],
    "Dairy and Egg Products": ["fat", "umami"],
    "Fruits and Fruit Juices": ["sweet", "acid"],
    "Vegetables and Vegetable Products": ["aromatic", "bitter"],
    "Finfish and Shellfish Products": ["umami"],
    "Sweets": ["sweet"],
    "Cereal Grains and Pasta": [],
    "Legumes and Legume Products": ["umami"],
    "Nut and Seed Products": ["fat", "aromatic"],
    "Beef Products": ["umami", "fat"],
    "Pork Products": ["umami", "fat"],
    "Poultry Products": ["umami"],
    "Lamb, Veal, and Game Products": ["umami"],
    "Soups, Sauces, and Gravies": ["umami", "salt"],
}


# ================================================================
# UNIFIED SCHEMA — used by both demo and full import
# Matches USDA FoodData Central field structure
# ================================================================

CREATE_TABLES = """
-- Central food table (the HUB — everything connects here via fdc_id)
CREATE TABLE IF NOT EXISTS food (
    fdc_id          INTEGER PRIMARY KEY,
    description     TEXT NOT NULL,
    food_category_id INTEGER,
    data_type       TEXT,
    publication_date TEXT,
    -- Our additions (not in USDA schema)
    short_name      TEXT,
    flavor_dimensions TEXT,
    FOREIGN KEY (food_category_id) REFERENCES food_category(id)
);

-- Food groups
CREATE TABLE IF NOT EXISTS food_category (
    id              INTEGER PRIMARY KEY,
    code            TEXT,
    description     TEXT NOT NULL
);

-- Nutrient definitions (what's being measured)
CREATE TABLE IF NOT EXISTS nutrient (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    unit_name       TEXT,
    nutrient_nbr    TEXT,
    rank            REAL
);

-- Per-food nutrient values (amount per 100g)
CREATE TABLE IF NOT EXISTS food_nutrient (
    id              INTEGER PRIMARY KEY,
    fdc_id          INTEGER NOT NULL,
    nutrient_id     INTEGER NOT NULL,
    amount          REAL,
    data_points     INTEGER,
    FOREIGN KEY (fdc_id) REFERENCES food(fdc_id),
    FOREIGN KEY (nutrient_id) REFERENCES nutrient(id)
);

-- Serving sizes / portion weights
CREATE TABLE IF NOT EXISTS food_portion (
    id              INTEGER PRIMARY KEY,
    fdc_id          INTEGER NOT NULL,
    seq_num         INTEGER,
    amount          REAL,
    measure_unit_id INTEGER,
    modifier        TEXT,
    gram_weight     REAL,
    portion_description TEXT,
    FOREIGN KEY (fdc_id) REFERENCES food(fdc_id)
);

-- Ingredient relationships (what foods are made of other foods)
CREATE TABLE IF NOT EXISTS input_food (
    id              INTEGER PRIMARY KEY,
    fdc_id          INTEGER NOT NULL,
    fdc_id_of_input_food INTEGER,
    seq_num         INTEGER,
    amount          REAL,
    gram_weight     REAL,
    FOREIGN KEY (fdc_id) REFERENCES food(fdc_id)
);

-- ================================================================
-- CONVENIENCE VIEW: flattened nutrition (what intake.py queries)
-- Pivots food_nutrient rows into one row per food with columns
-- ================================================================
CREATE VIEW IF NOT EXISTS nutrition_flat AS
SELECT
    f.fdc_id,
    f.description,
    f.short_name,
    fc.description as category,
    MAX(CASE WHEN n.nutrient_nbr IN ('208','1008') THEN fn.amount END) as calories_kcal,
    MAX(CASE WHEN n.nutrient_nbr IN ('203','1003') THEN fn.amount END) as protein_g,
    MAX(CASE WHEN n.nutrient_nbr IN ('204','1004') THEN fn.amount END) as fat_g,
    MAX(CASE WHEN n.nutrient_nbr IN ('205','1005') THEN fn.amount END) as carbs_g,
    MAX(CASE WHEN n.nutrient_nbr IN ('307','1093') THEN fn.amount END) as sodium_mg,
    MAX(CASE WHEN n.nutrient_nbr IN ('291','1079') THEN fn.amount END) as fiber_g,
    MAX(CASE WHEN n.nutrient_nbr IN ('269')        THEN fn.amount END) as sugar_g,
    MAX(CASE WHEN n.nutrient_nbr IN ('606','1258') THEN fn.amount END) as saturated_fat_g,
    MAX(CASE WHEN n.nutrient_nbr IN ('401','1162') THEN fn.amount END) as vitamin_c_mg,
    MAX(CASE WHEN n.nutrient_nbr IN ('301','1087') THEN fn.amount END) as calcium_mg,
    MAX(CASE WHEN n.nutrient_nbr IN ('303','1089') THEN fn.amount END) as iron_mg,
    MAX(CASE WHEN n.nutrient_nbr IN ('306','1092') THEN fn.amount END) as potassium_mg,
    MAX(CASE WHEN n.nutrient_nbr IN ('601','1253') THEN fn.amount END) as cholesterol_mg
FROM food f
LEFT JOIN food_category fc ON f.food_category_id = fc.id
LEFT JOIN food_nutrient fn ON f.fdc_id = fn.fdc_id
LEFT JOIN nutrient n ON fn.nutrient_id = n.id
GROUP BY f.fdc_id;

-- Version tracking: when was this data imported, from what source
CREATE TABLE IF NOT EXISTS import_meta (
    key             TEXT PRIMARY KEY,
    value           TEXT
);

-- Indexes for fast lookups
CREATE INDEX IF NOT EXISTS idx_food_category ON food(food_category_id);
CREATE INDEX IF NOT EXISTS idx_food_desc ON food(description);
CREATE INDEX IF NOT EXISTS idx_food_short ON food(short_name);
CREATE INDEX IF NOT EXISTS idx_food_data_type ON food(data_type);
CREATE INDEX IF NOT EXISTS idx_fn_fdc ON food_nutrient(fdc_id);
CREATE INDEX IF NOT EXISTS idx_fn_nutrient ON food_nutrient(nutrient_id);
CREATE INDEX IF NOT EXISTS idx_portion_fdc ON food_portion(fdc_id);
CREATE INDEX IF NOT EXISTS idx_input_fdc ON input_food(fdc_id);
"""


def init_db():
    """Create a fresh database with the unified schema."""
    os.makedirs(DATA_DIR, exist_ok=True)
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(CREATE_TABLES)
    conn.commit()
    return conn


# ================================================================
# FULL CSV IMPORT
# ================================================================

def import_csv_file(filepath, conn, table_name, field_filter=None):
    """Generic CSV importer. Returns row count."""
    if not os.path.exists(filepath):
        print(f"    SKIP: {os.path.basename(filepath)} not found")
        return 0

    cur = conn.cursor()
    count = 0
    with open(filepath, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            if field_filter and not field_filter(row):
                continue
            count += 1
    return count


def full_csv_import(conn):
    """Import all USDA CSV files into the relational schema."""

    # 1. food_category.csv → food_category table
    cat_path = os.path.join(DATA_DIR, "food_category.csv")
    categories = {}       # numeric id → description
    cat_name_to_id = {}   # description text → numeric id (for branded foods)
    if os.path.exists(cat_path):
        with open(cat_path, newline='', encoding='utf-8') as f:
            for row in csv.DictReader(f):
                cid = int(row["id"])
                desc = row["description"]
                categories[cid] = desc
                cat_name_to_id[desc] = cid
                conn.execute(
                    "INSERT OR IGNORE INTO food_category (id, code, description) VALUES (?, ?, ?)",
                    (cid, row.get("code", ""), desc)
                )
        print(f"    food_category: {len(categories)} groups")

    # 2. nutrient.csv → nutrient table
    nut_path = os.path.join(DATA_DIR, "nutrient.csv")
    nutrient_ids = {}  # id → nutrient_nbr
    if os.path.exists(nut_path):
        count = 0
        with open(nut_path, newline='', encoding='utf-8') as f:
            for row in csv.DictReader(f):
                nid = int(row["id"])
                nbr = row.get("nutrient_nbr", "")
                nutrient_ids[nid] = nbr
                conn.execute(
                    "INSERT OR IGNORE INTO nutrient (id, name, unit_name, nutrient_nbr, rank) VALUES (?, ?, ?, ?, ?)",
                    (nid, row["name"], row.get("unit_name", ""), nbr, float(row.get("rank", 0) or 0))
                )
                count += 1
        print(f"    nutrient: {count} definitions")

    # 3. food.csv → food table (filtered to cooking categories)
    #    NOTE: food_category_id is NUMERIC for foundation/sr_legacy/sample foods
    #    but a TEXT DESCRIPTION for branded foods. We handle both.
    food_path = os.path.join(DATA_DIR, "food.csv")
    food_ids = set()
    skipped_types = {}
    if os.path.exists(food_path):
        with open(food_path, newline='', encoding='utf-8') as f:
            for row in csv.DictReader(f):
                raw_cat = row.get("food_category_id", "")

                # Resolve category ID: try numeric first, then text lookup
                cat_id = None
                cat_name = ""
                try:
                    cat_id = int(raw_cat)
                    cat_name = categories.get(cat_id, "")
                except (ValueError, TypeError):
                    # Branded foods use text description as category
                    # Try matching to our known categories
                    cat_name = raw_cat
                    cat_id = cat_name_to_id.get(raw_cat)

                if cat_name not in COOKING_CATEGORIES:
                    dt = row.get("data_type", "")
                    skipped_types[dt] = skipped_types.get(dt, 0) + 1
                    continue

                fdc_id = int(row["fdc_id"])
                food_ids.add(fdc_id)

                # Generate short name from description (text before first comma)
                desc = row["description"]
                short = desc.split(",")[0].strip()

                flavors = CATEGORY_FLAVORS.get(cat_name, [])

                conn.execute(
                    "INSERT OR IGNORE INTO food (fdc_id, description, food_category_id, data_type, publication_date, short_name, flavor_dimensions) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (fdc_id, desc, cat_id, row.get("data_type", ""),
                     row.get("publication_date", ""), short, json.dumps(flavors))
                )
        print(f"    food: {len(food_ids)} cooking-relevant foods")
        if skipped_types:
            for dt, cnt in sorted(skipped_types.items(), key=lambda x: -x[1]):
                print(f"      skipped {cnt} {dt} (non-cooking category)")

    # 4. food_nutrient.csv → food_nutrient table (only for foods we kept)
    fn_path = os.path.join(DATA_DIR, "food_nutrient.csv")
    if os.path.exists(fn_path):
        count = 0
        batch = []
        with open(fn_path, newline='', encoding='utf-8') as f:
            for row in csv.DictReader(f):
                fdc_id = int(row["fdc_id"])
                if fdc_id not in food_ids:
                    continue
                nid = int(row.get("nutrient_id", 0) or 0)
                # Only keep nutrients we care about
                nbr = nutrient_ids.get(nid, "")
                if nbr not in NUTRIENT_MAP:
                    continue

                batch.append((
                    int(row.get("id", 0) or 0),
                    fdc_id, nid,
                    float(row.get("amount", 0) or 0),
                    int(row.get("data_points", 0) or 0),
                ))
                count += 1

                if len(batch) >= 5000:
                    conn.executemany(
                        "INSERT OR IGNORE INTO food_nutrient (id, fdc_id, nutrient_id, amount, data_points) VALUES (?, ?, ?, ?, ?)",
                        batch
                    )
                    batch = []

        if batch:
            conn.executemany(
                "INSERT OR IGNORE INTO food_nutrient (id, fdc_id, nutrient_id, amount, data_points) VALUES (?, ?, ?, ?, ?)",
                batch
            )
        print(f"    food_nutrient: {count} values")

    # 5. food_portion.csv → food_portion table
    fp_path = os.path.join(DATA_DIR, "food_portion.csv")
    if os.path.exists(fp_path):
        count = 0
        with open(fp_path, newline='', encoding='utf-8') as f:
            for row in csv.DictReader(f):
                fdc_id = int(row["fdc_id"])
                if fdc_id not in food_ids:
                    continue
                conn.execute(
                    "INSERT OR IGNORE INTO food_portion (id, fdc_id, seq_num, amount, measure_unit_id, modifier, gram_weight, portion_description) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (int(row.get("id", 0) or 0), fdc_id,
                     int(row.get("seq_num", 0) or 0),
                     float(row.get("amount", 0) or 0),
                     int(row.get("measure_unit_id", 0) or 0),
                     row.get("modifier", ""),
                     float(row.get("gram_weight", 0) or 0),
                     row.get("portion_description", ""))
                )
                count += 1
        print(f"    food_portion: {count} serving sizes")

    # 6. input_food.csv → input_food table (if available)
    if_path = os.path.join(DATA_DIR, "input_food.csv")
    if os.path.exists(if_path):
        count = 0
        with open(if_path, newline='', encoding='utf-8') as f:
            for row in csv.DictReader(f):
                fdc_id = int(row["fdc_id"])
                if fdc_id not in food_ids:
                    continue
                conn.execute(
                    "INSERT OR IGNORE INTO input_food (id, fdc_id, fdc_id_of_input_food, seq_num, amount, gram_weight) VALUES (?, ?, ?, ?, ?, ?)",
                    (int(row.get("id", 0) or 0), fdc_id,
                     int(row.get("fdc_id_of_input_food", 0) or 0),
                     int(row.get("seq_num", 0) or 0),
                     float(row.get("amount", 0) or 0),
                     float(row.get("gram_weight", 0) or 0))
                )
                count += 1
        print(f"    input_food: {count} ingredient links")

    conn.commit()
    return len(food_ids)


# ================================================================
# DEMO FOUNDATION (when CSVs aren't available)
# ================================================================

def generate_demo(conn):
    """Build a demo foundation from known USDA data."""
    print("  No USDA CSV files found in data/usda/")
    print("  Generating demo foundation from known USDA data...\n")

    # Real USDA FDC IDs, names, and nutrition (verified values)
    DEMO_FOODS = [
        # (fdc_id, description, short_name, category_name, cal, protein, fat, carbs, sodium)
        # Proteins
        (171077, "Chicken, broilers or fryers, breast, skinless, boneless, meat only, raw", "Chicken Breast", "Poultry Products", 120, 22.5, 2.6, 0, 116),
        (171078, "Chicken, broilers or fryers, thigh, meat only, raw", "Chicken Thigh", "Poultry Products", 177, 19.7, 10.9, 0, 80),
        (174036, "Beef, ground, 80% lean meat / 20% fat, raw", "Ground Beef", "Beef Products", 254, 17.2, 20, 0, 66),
        (175167, "Fish, salmon, Atlantic, wild, raw", "Salmon", "Finfish and Shellfish Products", 142, 19.8, 6.3, 0, 44),
        (175015, "Pork, fresh, loin, tenderloin, separable lean only, raw", "Pork Tenderloin", "Pork Products", 143, 22.4, 5.3, 0, 57),
        (171287, "Egg, whole, raw, fresh", "Egg", "Dairy and Egg Products", 143, 12.6, 9.5, 0.7, 142),

        # Vegetables
        (170457, "Tomatoes, red, ripe, raw, year round average", "Tomato", "Vegetables and Vegetable Products", 18, 0.9, 0.2, 3.9, 5),
        (169226, "Onions, raw", "Onion", "Vegetables and Vegetable Products", 40, 1.1, 0.1, 9.3, 4),
        (169230, "Garlic, raw", "Garlic", "Vegetables and Vegetable Products", 149, 6.4, 0.5, 33.1, 17),
        (170108, "Peppers, hot chili, red, raw", "Chili Pepper", "Vegetables and Vegetable Products", 40, 1.9, 0.4, 8.8, 9),
        (169228, "Peppers, sweet, green, raw", "Bell Pepper", "Vegetables and Vegetable Products", 20, 0.9, 0.2, 4.6, 3),
        (170393, "Potatoes, flesh and skin, raw", "Potato", "Vegetables and Vegetable Products", 77, 2.0, 0.1, 17.5, 6),
        (169986, "Carrots, raw", "Carrot", "Vegetables and Vegetable Products", 41, 0.9, 0.2, 9.6, 69),
        (168411, "Broccoli, raw", "Broccoli", "Vegetables and Vegetable Products", 34, 2.8, 0.4, 6.6, 33),
        (169967, "Celery, raw", "Celery", "Vegetables and Vegetable Products", 14, 0.7, 0.2, 3.0, 80),
        (170381, "Mushrooms, white, raw", "Mushroom", "Vegetables and Vegetable Products", 22, 3.1, 0.3, 3.3, 5),

        # Herbs & Spices
        (170923, "Spices, cumin seed", "Cumin", "Spices and Herbs", 375, 17.8, 22.3, 44.2, 168),
        (170929, "Spices, oregano, dried", "Oregano", "Spices and Herbs", 265, 9.0, 4.3, 68.9, 25),
        (170931, "Spices, pepper, black", "Black Pepper", "Spices and Herbs", 251, 10.4, 3.3, 63.9, 20),
        (170934, "Spices, cinnamon, ground", "Cinnamon", "Spices and Herbs", 247, 4.0, 1.2, 80.6, 10),
        (170921, "Spices, coriander seed", "Coriander Seed", "Spices and Herbs", 298, 12.4, 17.8, 54.9, 35),
        (170926, "Spices, ginger, ground", "Ground Ginger", "Spices and Herbs", 335, 9.0, 4.2, 71.6, 27),
        (170936, "Spices, turmeric, ground", "Turmeric", "Spices and Herbs", 312, 9.7, 3.3, 67.1, 27),
        (170928, "Spices, paprika", "Paprika", "Spices and Herbs", 282, 14.1, 13.0, 53.9, 68),
        (170906, "Coriander (cilantro) leaves, raw", "Cilantro", "Spices and Herbs", 23, 2.1, 0.5, 3.7, 46),
        (170935, "Basil, fresh", "Basil", "Spices and Herbs", 23, 3.2, 0.6, 2.7, 4),
        (169231, "Ginger root, raw", "Fresh Ginger", "Spices and Herbs", 80, 1.8, 0.8, 17.8, 13),

        # Fats & Oils
        (171413, "Oil, olive, salad or cooking", "Olive Oil", "Fats and Oils", 884, 0, 100, 0, 2),
        (173410, "Butter, salted", "Butter", "Fats and Oils", 717, 0.9, 81.1, 0.1, 643),
        (172336, "Oil, vegetable, canola", "Canola Oil", "Fats and Oils", 884, 0, 100, 0, 0),
        (171025, "Oil, sesame, salad or cooking", "Sesame Oil", "Fats and Oils", 884, 0, 100, 0, 0),

        # Grains
        (169756, "Rice, white, long-grain, regular, raw, unenriched", "White Rice", "Cereal Grains and Pasta", 365, 7.1, 0.7, 80, 5),
        (168925, "Pasta, dry, unenriched", "Pasta", "Cereal Grains and Pasta", 371, 13.0, 1.5, 74.7, 6),
        (169761, "Wheat flour, white, all-purpose, unenriched", "All-Purpose Flour", "Cereal Grains and Pasta", 364, 10.3, 1.0, 76.3, 2),

        # Fruits
        (168155, "Limes, raw", "Lime", "Fruits and Fruit Juices", 30, 0.7, 0.2, 10.5, 2),
        (168153, "Lemons, raw, without peel", "Lemon", "Fruits and Fruit Juices", 29, 1.1, 0.3, 9.3, 2),

        # Condiments
        (173767, "Soy sauce made from soy (tamari)", "Soy Sauce", "Soups, Sauces, and Gravies", 60, 10.5, 0.1, 5.6, 5586),
        (174583, "Fish sauce, ready to serve", "Fish Sauce", "Soups, Sauces, and Gravies", 35, 5.1, 0.0, 3.6, 7851),
        (173468, "Vinegar, distilled", "Vinegar", "Soups, Sauces, and Gravies", 18, 0, 0, 0.04, 2),

        # Sweeteners
        (169655, "Sugars, granulated", "Sugar", "Sweets", 387, 0, 0, 100, 1),
        (169640, "Honey", "Honey", "Sweets", 304, 0.3, 0, 82.4, 4),

        # Dairy
        (170172, "Coconut milk, raw (liquid expressed from grated meat and water)", "Coconut Milk", "Dairy and Egg Products", 230, 2.3, 23.8, 5.5, 15),

        # Legumes
        (175223, "Lentils, raw", "Lentils", "Legumes and Legume Products", 352, 25.8, 1.1, 60.1, 6),
        (173757, "Chickpeas (garbanzo beans, bengal gram), mature seeds, raw", "Chickpeas", "Legumes and Legume Products", 364, 19.3, 6.0, 60.6, 24),

        # Nuts
        (170187, "Peanuts, all types, raw", "Peanuts", "Nut and Seed Products", 567, 25.8, 49.2, 16.1, 18),
    ]

    # Build category lookup (auto-assign IDs based on unique names)
    cat_names = sorted(set(f[3] for f in DEMO_FOODS))
    cat_ids = {}
    for i, name in enumerate(cat_names, start=1):
        cat_ids[name] = i
        conn.execute(
            "INSERT INTO food_category (id, code, description) VALUES (?, ?, ?)",
            (i, str(i).zfill(4), name)
        )

    # Demo nutrient definitions (using FDC-style IDs)
    demo_nutrients = [
        (1008, "Energy", "kcal", "208", 300),
        (1003, "Protein", "g", "203", 600),
        (1004, "Total lipid (fat)", "g", "204", 800),
        (1005, "Carbohydrate, by difference", "g", "205", 1100),
        (1093, "Sodium, Na", "mg", "307", 5800),
    ]
    for nid, name, unit, nbr, rank in demo_nutrients:
        conn.execute(
            "INSERT INTO nutrient (id, name, unit_name, nutrient_nbr, rank) VALUES (?, ?, ?, ?, ?)",
            (nid, name, unit, nbr, rank)
        )

    # Insert foods and their nutrients
    fn_id = 1
    for fdc_id, desc, short, cat_name, cal, protein, fat, carbs, sodium in DEMO_FOODS:
        cat_id = cat_ids[cat_name]
        flavors = CATEGORY_FLAVORS.get(cat_name, [])

        conn.execute(
            "INSERT INTO food (fdc_id, description, food_category_id, data_type, short_name, flavor_dimensions) VALUES (?, ?, ?, ?, ?, ?)",
            (fdc_id, desc, cat_id, "demo_foundation", short, json.dumps(flavors))
        )

        # Insert nutrients as separate rows (matching the relational schema)
        for nid, value in [(1008, cal), (1003, protein), (1004, fat), (1005, carbs), (1093, sodium)]:
            conn.execute(
                "INSERT INTO food_nutrient (id, fdc_id, nutrient_id, amount, data_points) VALUES (?, ?, ?, ?, ?)",
                (fn_id, fdc_id, nid, value, 1)
            )
            fn_id += 1

    conn.commit()

    total = conn.execute("SELECT COUNT(*) FROM food").fetchone()[0]
    cats = conn.execute("SELECT COUNT(*) FROM food_category").fetchone()[0]
    nutrients = conn.execute("SELECT COUNT(*) FROM food_nutrient").fetchone()[0]

    print(f"    Built: {DB_PATH}")
    print(f"    Foods: {total}")
    print(f"    Categories: {cats}")
    print(f"    Nutrient values: {nutrients}")

    # Verify the convenience view works
    row = conn.execute(
        "SELECT fdc_id, short_name, calories_kcal, protein_g FROM nutrition_flat WHERE fdc_id = 171077"
    ).fetchone()
    if row:
        print(f"\n    View check: {row[1]} = {row[2]} kcal, {row[3]}g protein  ✓")

    return total


# ================================================================
# MAIN
# ================================================================

def stamp_version(conn, source, total_foods):
    """Record import metadata for version tracking."""
    from datetime import datetime
    meta = {
        "import_date": datetime.now().isoformat(),
        "source": source,
        "total_foods": str(total_foods),
        "schema_version": "2",
        "usda_release": "",
    }

    # Try to detect the USDA release from the zip filename
    import glob
    zips = glob.glob(os.path.join(CONTENT_DIR, "usda", "FoodData_Central_csv_*.zip"))
    if zips:
        # Extract date from filename like FoodData_Central_csv_2025-04-24.zip
        import re
        m = re.search(r'(\d{4}-\d{2}-\d{2})', os.path.basename(zips[0]))
        if m:
            meta["usda_release"] = m.group(1)

    for k, v in meta.items():
        conn.execute(
            "INSERT OR REPLACE INTO import_meta (key, value) VALUES (?, ?)",
            (k, v)
        )
    conn.commit()

    print(f"\n  VERSION STAMP:")
    print(f"    Source: {meta['source']}")
    print(f"    USDA Release: {meta['usda_release'] or 'unknown'}")
    print(f"    Import Date: {meta['import_date']}")
    print(f"    Foods: {meta['total_foods']}")


def main():
    print("\n  USDA_IMPORT.PY — Build the Foundation Layer")
    print("  =============================================\n")

    conn = init_db()

    # Check for real USDA CSV files
    food_csv = os.path.join(DATA_DIR, "food.csv")
    if os.path.exists(food_csv):
        print("  Found USDA CSV files. Importing full database...\n")
        total = full_csv_import(conn)
        print(f"\n    Total cooking-relevant foods: {total}")

        # Verify the convenience view
        row = conn.execute(
            "SELECT COUNT(*) FROM nutrition_flat WHERE calories_kcal IS NOT NULL"
        ).fetchone()
        print(f"    Foods with nutrition data: {row[0]}")

        stamp_version(conn, "FoodData Central CSV (full)", total)
    else:
        print("  No USDA CSV files in data/usda/.")
        print("  To import the full database:")
        print("    1. Go to https://fdc.nal.usda.gov/download-datasets/")
        print("    2. Download Foundation Foods CSV")
        print("    3. Unzip into data/usda/")
        print("    4. Re-run this script\n")
        total = generate_demo(conn)
        stamp_version(conn, "demo_foundation (built-in)", total)

    conn.close()

    # Show the architecture
    print(f"\n  THE STACK:")
    print(f"  ─────────────────────────────────────")
    print(f"    intake.py        ← recipes come in here")
    print(f"    normalization.yaml ← synonyms map to USDA IDs")
    print(f"    foundation.db    ← USDA data (nutrition, categories, flavors)")
    print(f"    publish.py       ← everything goes out here")
    print(f"  ─────────────────────────────────────")

    print(f"\n  SCHEMA (gateways):")
    print(f"  ─────────────────────────────────────")
    print(f"    food_category ←── food ──→ food_nutrient ──→ nutrient")
    print(f"                       │")
    print(f"                       ├──→ food_portion")
    print(f"                       └──→ input_food")
    print(f"                       │")
    print(f"                  nutrition_flat (view — joins all of the above)")
    print(f"  ─────────────────────────────────────")
    print(f"\n  Done.\n")


if __name__ == "__main__":
    main()
