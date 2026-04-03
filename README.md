# HowToCookAtHome

Multi-tenant restaurant platform with USDA-backed nutrition data, menu engineering, and branded site generation.

## What It Does

Restaurants sign up, submit their menu and vendor pricing, and get a branded site with USDA nutrition matching, menu engineering (Star/Plowhorse/Puzzle/Dog), food cost analysis, UPC barcode scanning, and automated scoring.

## Quick Start
```bash
pip install -r requirements.txt
python3 -m app.server          # http://localhost:3050
```

## Build USDA Database (one-time)
```bash
./scripts/build_db.sh          # Downloads ~450MB from USDA
```

## Run Tests
```bash
pytest tests/ -v
```

## License

Proprietary. See [LICENSE](LICENSE).
