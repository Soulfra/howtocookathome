#!/usr/bin/env python3
"""
BRANDED.PY — Import and query USDA Branded Foods (UPC barcodes).

The consumer side of HTCAH. Restaurant owners use Foundation Foods
(whole ingredients). Consumers scan UPC barcodes on packaged products
and get real nutrition data from USDA, not marketing labels.

Data source: USDA FoodData Central → branded_food.csv
  - 1.97M products, 1.19M with UPC barcodes
  - Includes: brand, ingredients list, serving size, category
  - Links to food_nutrient via fdc_id for full nutrition

Architecture:
  foundation.db has the food + food_nutrient tables (shared).
  This module adds a branded_food table for UPC/brand lookups.
  One DB, two audiences.
"""
import sqlite3
import csv
import os
import sys
import zipfile
import io

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "data", "usda", "foundation.db")
ZIP_PATH = os.path.join(BASE_DIR, "docs", "usda", "FoodData_Central_csv_2025-04-24.zip")


def import_branded_foods(db_path=None, zip_path=None, limit=None):
    """Import branded_food.csv from the USDA zip into foundation.db.

    Creates the branded_food table with UPC index for fast barcode lookups.
    Idempotent — drops and recreates the table each time.

    Args:
        db_path: Path to SQLite database (default: foundation.db)
        zip_path: Path to USDA CSV zip (default: docs/usda/FoodData*.zip)
        limit: Max rows to import (None = all). Use for testing.

    Returns:
        dict with import stats
    """
    db_path = db_path or DB_PATH
    zip_path = zip_path or ZIP_PATH

    if not os.path.exists(zip_path):
        print(f"  ERROR: USDA zip not found at {zip_path}")
        print(f"  Download from: https://fdc.nal.usda.gov/download-datasets")
        return {"error": "zip_not_found"}

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")

    # Create table
    conn.executescript("""
        DROP TABLE IF EXISTS branded_food;
        CREATE TABLE branded_food (
            fdc_id              INTEGER PRIMARY KEY,
            brand_owner         TEXT,
            brand_name          TEXT,
            gtin_upc            TEXT,
            ingredients         TEXT,
            serving_size        REAL,
            serving_size_unit   TEXT,
            household_serving   TEXT,
            branded_food_category TEXT,
            package_weight      TEXT,
            market_country      TEXT DEFAULT 'United States',
            discontinued_date   TEXT
        );
    """)

    # Extract and import from zip
    print(f"  Extracting branded_food.csv from {os.path.basename(zip_path)}...")
    csv_filename = None
    with zipfile.ZipFile(zip_path) as z:
        for name in z.namelist():
            if "branded_food" in name.lower() and name.endswith(".csv"):
                csv_filename = name
                break

        if not csv_filename:
            print("  ERROR: branded_food.csv not found in zip")
            conn.close()
            return {"error": "csv_not_found"}

        with z.open(csv_filename) as f:
            reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8"))

            batch = []
            total = 0
            with_upc = 0
            skipped = 0

            print("  Importing branded foods...")
            for row in reader:
                fdc_id = row.get("fdc_id", "").strip()
                if not fdc_id:
                    skipped += 1
                    continue

                upc = row.get("gtin_upc", "").strip().strip('"')
                if upc:
                    with_upc += 1

                # Parse serving size
                serving = row.get("serving_size", "").strip()
                try:
                    serving_val = float(serving) if serving else None
                except ValueError:
                    serving_val = None

                batch.append((
                    int(fdc_id),
                    row.get("brand_owner", "").strip(),
                    row.get("brand_name", "").strip(),
                    upc if upc else None,
                    row.get("ingredients", "").strip(),
                    serving_val,
                    row.get("serving_size_unit", "").strip(),
                    row.get("household_serving_fulltext", "").strip(),
                    row.get("branded_food_category", "").strip(),
                    row.get("package_weight", "").strip(),
                    row.get("market_country", "United States").strip(),
                    row.get("discontinued_date", "").strip() or None,
                ))

                total += 1

                if len(batch) >= 10000:
                    conn.executemany("""
                        INSERT OR REPLACE INTO branded_food
                        (fdc_id, brand_owner, brand_name, gtin_upc, ingredients,
                         serving_size, serving_size_unit, household_serving,
                         branded_food_category, package_weight, market_country,
                         discontinued_date)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, batch)
                    batch = []
                    if total % 100000 == 0:
                        print(f"    ... {total:,} rows imported")

                if limit and total >= limit:
                    break

            # Flush remaining
            if batch:
                conn.executemany("""
                    INSERT OR REPLACE INTO branded_food
                    (fdc_id, brand_owner, brand_name, gtin_upc, ingredients,
                     serving_size, serving_size_unit, household_serving,
                     branded_food_category, package_weight, market_country,
                     discontinued_date)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, batch)

    conn.commit()

    # Create indexes AFTER bulk insert (much faster)
    print("  Building indexes...")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bf_upc ON branded_food(gtin_upc)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bf_brand ON branded_food(brand_name)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bf_category ON branded_food(branded_food_category)")
    conn.commit()

    stats = {
        "total": total,
        "with_upc": with_upc,
        "skipped": skipped,
        "upc_coverage": round(with_upc / total * 100, 1) if total else 0,
    }

    print(f"\n  Done:")
    print(f"    Total branded foods: {stats['total']:,}")
    print(f"    With UPC barcode:    {stats['with_upc']:,} ({stats['upc_coverage']}%)")
    print(f"    Skipped:             {stats['skipped']:,}")

    conn.close()
    return stats


def lookup_upc(upc, db_path=None):
    """Look up a UPC barcode and return product info + nutrition.

    Args:
        upc: The UPC/GTIN barcode string (e.g., "00027000612323")

    Returns:
        dict with product info and nutrition, or None if not found.
    """
    db_path = db_path or DB_PATH
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Clean UPC — strip leading zeros, try both with and without
    upc = upc.strip().lstrip("0") if upc else ""
    if not upc:
        conn.close()
        return None

    # Try exact match first, then with leading zeros padded to common lengths
    candidates = [upc]
    for length in [12, 13, 14]:
        padded = upc.zfill(length)
        if padded not in candidates:
            candidates.append(padded)
    # Also try the raw input
    candidates = list(dict.fromkeys(candidates))  # dedupe, preserve order

    product = None
    for candidate in candidates:
        product = conn.execute(
            "SELECT * FROM branded_food WHERE gtin_upc = ?", (candidate,)
        ).fetchone()
        if product:
            break

    if not product:
        # Fuzzy: try LIKE match for partial UPCs
        product = conn.execute(
            "SELECT * FROM branded_food WHERE gtin_upc LIKE ?", (f"%{upc}%",)
        ).fetchone()

    if not product:
        conn.close()
        return None

    result = dict(product)
    fdc_id = result["fdc_id"]

    # Get the food description
    food = conn.execute(
        "SELECT description, data_type FROM food WHERE fdc_id = ?", (fdc_id,)
    ).fetchone()
    if food:
        result["description"] = food["description"]
        result["data_type"] = food["data_type"]

    # Get nutrition data
    nutrients = conn.execute("""
        SELECT n.name, n.unit_name, fn.amount
        FROM food_nutrient fn
        JOIN nutrient n ON fn.nutrient_id = n.id
        WHERE fn.fdc_id = ?
        ORDER BY n.name
    """, (fdc_id,)).fetchall()

    result["nutrients"] = {}
    result["nutrition_summary"] = {}

    # Key nutrients people care about
    key_nutrients = {
        "Energy": "calories",
        "Protein": "protein",
        "Total lipid (fat)": "fat",
        "Carbohydrate, by difference": "carbs",
        "Fiber, total dietary": "fiber",
        "Sugars, Total": "sugar",
        "Sugars, total including NLEA": "sugar",
        "Sodium, Na": "sodium",
        "Cholesterol": "cholesterol",
        "Fatty acids, total saturated": "saturated_fat",
        "Fatty acids, total trans": "trans_fat",
    }

    for row in nutrients:
        name = row["name"]
        result["nutrients"][name] = {
            "amount": row["amount"],
            "unit": row["unit_name"],
        }
        if name in key_nutrients:
            result["nutrition_summary"][key_nutrients[name]] = {
                "amount": row["amount"],
                "unit": row["unit_name"],
            }

    # Calculate calories from macros if USDA didn't provide Energy
    ns = result["nutrition_summary"]
    if "calories" not in ns:
        carbs = ns.get("carbs", {}).get("amount", 0) or 0
        protein = ns.get("protein", {}).get("amount", 0) or 0
        fat = ns.get("fat", {}).get("amount", 0) or 0
        if carbs or protein or fat:
            estimated_cal = round(carbs * 4 + protein * 4 + fat * 9, 1)
            ns["calories"] = {"amount": estimated_cal, "unit": "KCAL", "estimated": True}

    # Also add Total Sugars if we have it but not the NLEA version
    if "sugar" not in ns and "Total Sugars" in result["nutrients"]:
        ns["sugar"] = result["nutrients"]["Total Sugars"]

    conn.close()
    return result


def search_branded(query, category=None, limit=20, db_path=None):
    """Search branded foods by name/brand.

    Args:
        query: Search text (matches brand_name, brand_owner, or description)
        category: Optional category filter
        limit: Max results (default 20)

    Returns:
        List of matching products
    """
    db_path = db_path or DB_PATH
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    sql = """
        SELECT bf.fdc_id, bf.brand_owner, bf.brand_name, bf.gtin_upc,
               bf.branded_food_category, bf.serving_size, bf.serving_size_unit,
               bf.household_serving, f.description
        FROM branded_food bf
        LEFT JOIN food f ON bf.fdc_id = f.fdc_id
        WHERE (bf.brand_name LIKE ? OR bf.brand_owner LIKE ? OR f.description LIKE ?)
    """
    params = [f"%{query}%", f"%{query}%", f"%{query}%"]

    if category:
        sql += " AND bf.branded_food_category LIKE ?"
        params.append(f"%{category}%")

    sql += " AND bf.gtin_upc IS NOT NULL"  # only products with barcodes
    sql += " ORDER BY bf.brand_name LIMIT ?"
    params.append(limit)

    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# --------------------------------------------------
# CLI
# --------------------------------------------------
if __name__ == "__main__":
    args = sys.argv[1:]

    if not args or args[0] == "import":
        limit = None
        if "--limit" in args:
            idx = args.index("--limit")
            limit = int(args[idx + 1])
        print(f"\n  HTCAH Branded Foods Import")
        print(f"  ==========================\n")
        import_branded_foods(limit=limit)

    elif args[0] == "scan":
        upc = args[1] if len(args) > 1 else ""
        if not upc:
            print("Usage: python -m app.branded scan <UPC>")
            sys.exit(1)
        result = lookup_upc(upc)
        if result:
            print(f"\n  Product: {result.get('description', 'Unknown')}")
            print(f"  Brand:   {result.get('brand_owner', '')} / {result.get('brand_name', '')}")
            print(f"  UPC:     {result.get('gtin_upc', '')}")
            print(f"  Category: {result.get('branded_food_category', '')}")
            print(f"  Serving: {result.get('household_serving', '')}")
            if result.get("nutrition_summary"):
                print(f"\n  Nutrition (per serving):")
                for key, val in result["nutrition_summary"].items():
                    print(f"    {key:15s}: {val['amount']} {val['unit']}")
            if result.get("ingredients"):
                ing = result["ingredients"][:200]
                print(f"\n  Ingredients: {ing}{'...' if len(result['ingredients']) > 200 else ''}")
        else:
            print(f"\n  No product found for UPC: {upc}")

    elif args[0] == "search":
        query = " ".join(args[1:])
        results = search_branded(query)
        print(f"\n  Found {len(results)} results for '{query}':\n")
        for r in results[:10]:
            print(f"  [{r.get('gtin_upc', 'no-upc')}] {r.get('brand_name', '')} — {r.get('description', '')}")

    else:
        print("Usage:")
        print("  python -m app.branded import [--limit N]")
        print("  python -m app.branded scan <UPC>")
        print("  python -m app.branded search <query>")
