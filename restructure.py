#!/usr/bin/env python3
"""
RESTRUCTURE.PY — Migrate flat directory layout to organized structure.

Run: python3 restructure.py
  - Creates new directories
  - Moves files to their new homes
  - Does NOT delete originals until verified (uses copy first, then prints rm commands)

This script is idempotent — safe to run multiple times.
"""
import os, shutil, sys

BASE = os.path.dirname(os.path.abspath(__file__))

# ── New directories to create ──
DIRS = [
    "app",
    "content/episodes/season-1",
    "content/recipes",
    "content/inventories",
    "content/tenants",
    "docs/original",
    "docs/usda",
    "archive/old-price-lists",
]

# ── File moves: (src, dst) relative to BASE ──
# Python app code → app/
MOVES = [
    ("server.py",           "app/server.py"),
    ("onboard_api.py",      "app/onboard.py"),
    ("tenant_publish.py",   "app/publish.py"),
    ("cookbook_gen.py",      "app/cookbook.py"),
    ("db.py",               "app/db.py"),
    ("menu_engine.py",      "app/menu.py"),
    ("review_engine.py",    "app/review.py"),
    ("eval_engine.py",      "app/eval.py"),
    ("local_ai.py",         "app/ai.py"),
    ("usda_import.py",      "app/usda.py"),
    ("migrate_yaml_to_db.py", "app/migrate.py"),

    # Content YAML configs → content/
    ("source/brand.yaml",           "content/brand.yaml"),
    ("source/tiers.yaml",           "content/tiers.yaml"),
    ("source/platform.yaml",        "content/platform.yaml"),
    ("source/intake.yaml",          "content/intake.yaml"),
    ("source/connectors.yaml",      "content/connectors.yaml"),
    ("source/database.yaml",        "content/database.yaml"),
    ("source/domains.yaml",         "content/domains.yaml"),
    ("source/normalization.yaml",   "content/normalization.yaml"),
    ("source/public-data.yaml",     "content/public-data.yaml"),
    ("source/architecture.yaml",    "content/architecture.yaml"),

    # Database files → data/
    ("htcah.db", "data/htcah.db"),
    # data/usda/ already in the right place

    # Docs & planning → docs/
    ("HANDOFF.md",                              "docs/HANDOFF.md"),
    ("HTCAH_Episode_Matrix_v1.xlsx",            "docs/HTCAH_Episode_Matrix_v1.xlsx"),
    ("HTCAH_Platform_Scope_Gap_Analysis.xlsx",  "docs/HTCAH_Platform_Scope_Gap_Analysis.xlsx"),
    ("HowToCookAtHome_Content_Bible.xlsx",      "docs/HowToCookAtHome_Content_Bible.xlsx"),
    ("HowToCookAtHome_Complete.zip",            "docs/original/HowToCookAtHome_Complete.zip"),

    # Generated artifacts that shouldn't be at root
    ("big-reds-bbq-cookbook.pdf",    "output/tenants/big-reds-bbq/big-reds-bbq-cookbook.pdf"),
    ("eval-report.html",            "output/eval-report.html"),
    ("eval-report.json",            "output/eval-report.json"),

    # Static assets — rename for consistency
    ("static/worksheet_print_ready.pdf", "static/worksheet.pdf"),

    # Archive old code
    ("source/archive/intake.py",                        "archive/intake.py"),
    ("source/archive/publish.py",                       "archive/publish.py"),
    ("source/archive/mama-rosa-trattoria-price-list.yaml", "archive/old-price-lists/mama-rosa-trattoria.yaml"),
    ("source/archive/mikes-bbq-pit-ingredients.yaml",      "archive/old-price-lists/mikes-bbq-pit.yaml"),
    ("source/archive/sarahs-bakery-price-list.yaml",       "archive/old-price-lists/sarahs-bakery.yaml"),
    ("source/archive/sarahs-bakery-v2-price-list.yaml",    "archive/old-price-lists/sarahs-bakery-v2.yaml"),
    ("source/archive/smokehouse-joe-price-list.yaml",      "archive/old-price-lists/smokehouse-joe.yaml"),
    ("source/archive/tonys-pizza-ingredients.yaml",        "archive/old-price-lists/tonys-pizza.yaml"),
]

# ── Directory-level moves (whole folders) ──
DIR_MOVES = [
    # Episodes
    ("source/episodes/season-1", "content/episodes/season-1"),
    # Recipes
    ("source/recipes",           "content/recipes"),
    # Inventories
    ("source/inventories",       "content/inventories"),
    # Tenant configs
    ("source/tenants",           "content/tenants"),
    # USDA reference docs
    ("source/usda",              "docs/usda"),
]


def main():
    dry_run = "--dry-run" in sys.argv
    prefix = "[DRY RUN] " if dry_run else ""

    # 1. Create directories
    for d in DIRS:
        path = os.path.join(BASE, d)
        if not os.path.exists(path):
            print(f"{prefix}mkdir {d}/")
            if not dry_run:
                os.makedirs(path, exist_ok=True)

    # 2. Create app/__init__.py
    init_path = os.path.join(BASE, "app", "__init__.py")
    if not os.path.exists(init_path):
        print(f"{prefix}create app/__init__.py")
        if not dry_run:
            os.makedirs(os.path.dirname(init_path), exist_ok=True)
            with open(init_path, "w") as f:
                f.write('"""HowToCookAtHome application package."""\n')

    # 3. Move individual files
    moved = 0
    skipped = 0
    for src_rel, dst_rel in MOVES:
        src = os.path.join(BASE, src_rel)
        dst = os.path.join(BASE, dst_rel)

        if not os.path.exists(src):
            # Already moved or doesn't exist
            if os.path.exists(dst):
                skipped += 1
            else:
                print(f"  SKIP (not found): {src_rel}")
            continue

        if os.path.exists(dst):
            print(f"  SKIP (dst exists): {dst_rel}")
            skipped += 1
            continue

        print(f"{prefix}move {src_rel} → {dst_rel}")
        if not dry_run:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.move(src, dst)
        moved += 1

    # 4. Move directories (copy contents for dirs that have overlapping targets)
    for src_rel, dst_rel in DIR_MOVES:
        src = os.path.join(BASE, src_rel)
        dst = os.path.join(BASE, dst_rel)

        if not os.path.isdir(src):
            if os.path.isdir(dst):
                skipped += 1
            continue

        # Move each file in the source dir
        for fname in sorted(os.listdir(src)):
            if fname.startswith('.'):
                continue
            sf = os.path.join(src, fname)
            df = os.path.join(dst, fname)
            if os.path.isfile(sf) and not os.path.exists(df):
                print(f"{prefix}move {os.path.join(src_rel, fname)} → {os.path.join(dst_rel, fname)}")
                if not dry_run:
                    os.makedirs(dst, exist_ok=True)
                    shutil.move(sf, df)
                moved += 1
            elif os.path.isdir(sf):
                # Recurse one level for season-1 etc.
                for sub in sorted(os.listdir(sf)):
                    if sub.startswith('.'):
                        continue
                    ssf = os.path.join(sf, sub)
                    ddf = os.path.join(df, sub)
                    if os.path.isfile(ssf) and not os.path.exists(ddf):
                        print(f"{prefix}move {os.path.join(src_rel, fname, sub)} → {os.path.join(dst_rel, fname, sub)}")
                        if not dry_run:
                            os.makedirs(df, exist_ok=True)
                            shutil.move(ssf, ddf)
                        moved += 1

    print(f"\n{prefix}Done: {moved} moved, {skipped} skipped")

    if not dry_run:
        # 5. Clean up empty source dirs
        for dirpath, dirnames, filenames in os.walk(os.path.join(BASE, "source"), topdown=False):
            # Remove .DS_Store files
            for f in filenames:
                if f == ".DS_Store":
                    os.remove(os.path.join(dirpath, f))
            # Remove empty dirs
            remaining = [f for f in os.listdir(dirpath) if not f.startswith('.')]
            if not remaining:
                print(f"  rmdir {os.path.relpath(dirpath, BASE)}/")
                os.rmdir(dirpath)

        print("\nNext step: run  python3 restructure.py --verify  to check everything")
    else:
        print("\nRun without --dry-run to execute moves.")


if __name__ == "__main__":
    main()
