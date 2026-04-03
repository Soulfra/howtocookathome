#!/bin/bash
# build_db.sh — Download USDA data and build foundation.db
#
# Usage:
#   ./scripts/build_db.sh           # Full build (foundation + branded)
#   ./scripts/build_db.sh --quick   # Foundation only (30MB, ~2 min)
#
# This script:
#   1. Downloads the USDA FoodData Central CSV zip (if not present)
#   2. Builds foundation.db with Foundation Foods (~84K foods, ~2.4M nutrients)
#   3. Optionally imports Branded Foods (~2M products with UPC barcodes)
#   4. Syncs htcah.db from YAML tenant configs
#
# On Render: runs once, then foundation.db lives on persistent disk.
# Locally: run this after cloning to set up the database.

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

USDA_DIR="docs/usda"
DATA_DIR="data/usda"
ZIP_URL="https://fdc.nal.usda.gov/fdc-datasets/FoodData_Central_csv_2025-04-24.zip"
ZIP_FILE="$USDA_DIR/FoodData_Central_csv_2025-04-24.zip"
DB_FILE="$DATA_DIR/foundation.db"

echo ""
echo "  HTCAH Database Builder"
echo "  ======================"
echo ""

# Ensure directories exist
mkdir -p "$USDA_DIR" "$DATA_DIR"

# Step 1: Download USDA zip if not present
if [ ! -f "$ZIP_FILE" ]; then
    echo "  Downloading USDA FoodData Central (~450MB)..."
    echo "  Source: $ZIP_URL"
    echo ""
    curl -L -o "$ZIP_FILE" "$ZIP_URL"
    echo "  Download complete."
else
    echo "  USDA zip already present: $ZIP_FILE"
fi

# Step 2: Build foundation.db (Foundation Foods)
if [ ! -f "$DB_FILE" ] || [ "$1" = "--rebuild" ]; then
    echo ""
    echo "  Building foundation.db (Foundation Foods)..."
    python3 -m app.foundation
    echo "  Foundation Foods imported."
else
    echo "  foundation.db already exists ($(du -h "$DB_FILE" | cut -f1))"
    echo "  Use --rebuild to recreate from scratch."
fi

# Step 3: Import Branded Foods (unless --quick)
if [ "$1" != "--quick" ]; then
    echo ""
    echo "  Importing Branded Foods (UPC barcodes, ~2M products)..."
    echo "  This adds ~1.7GB to foundation.db. Takes 3-5 minutes."
    python3 -m app.branded import
    echo "  Branded Foods imported."
else
    echo ""
    echo "  Skipping Branded Foods (--quick mode)."
    echo "  Run without --quick to add UPC barcode support."
fi

# Step 4: Sync htcah.db from YAML
echo ""
echo "  Syncing htcah.db from YAML configs..."
python3 -m app.db sync

# Summary
echo ""
echo "  Build complete!"
echo "  ==============="
echo "  foundation.db: $(du -h "$DB_FILE" | cut -f1) (USDA reference data)"
echo "  htcah.db:      $(du -h data/htcah.db 2>/dev/null | cut -f1 || echo 'new') (app data)"
echo ""
