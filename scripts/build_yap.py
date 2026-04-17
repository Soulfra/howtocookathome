#!/usr/bin/env python3
"""
Build this week's yap DRAFT as structured YAML.

Auto-drafts pick 3 recipes from content/recipes/ (seeded by ISO week so
each week is reproducible) and fill in the savings math at default values.
Matt edits the file Saturday night; render_yap.py turns it into a PDF;
send_yap.py mails it Sunday.

Usage:
    python3 scripts/build_yap.py              # current ISO week
    python3 scripts/build_yap.py --week 2026-16
    python3 scripts/build_yap.py --force      # overwrite existing draft
"""
import argparse
import datetime as dt
import os
import random
import sys
from pathlib import Path

import yaml

BASE_DIR = Path(__file__).resolve().parent.parent
RECIPES_DIR = BASE_DIR / "content" / "recipes"
YAP_DIR = BASE_DIR / "content" / "yap"


def current_week_id():
    today = dt.date.today()
    iso = today.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def parse_week(s):
    """Accepts 2026-W16 / 2026-16 / 2026W16."""
    s = s.strip().upper().replace("W", "-").replace("--", "-")
    parts = s.split("-")
    if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
        raise SystemExit(f"Bad --week '{s}'. Use e.g. 2026-W16.")
    y, w = int(parts[0]), int(parts[1])
    return f"{y}-W{w:02d}"


def load_recipes():
    items = []
    for p in sorted(RECIPES_DIR.glob("*.yaml")):
        try:
            with open(p) as f:
                r = yaml.safe_load(f) or {}
            if r.get("title") and r.get("slug"):
                items.append({
                    "slug": r["slug"],
                    "title": r["title"],
                    "cuisine": r.get("cuisine", ""),
                })
        except Exception:
            continue
    return items


def pick_three(recipes, week_id):
    """Deterministic pick by week so the same --week always drafts the same recipes."""
    if not recipes:
        return []
    rng = random.Random(week_id)
    n = min(3, len(recipes))
    return rng.sample(recipes, n)


def default_draft(week_id, recipes):
    iso_y, iso_w = week_id.split("-W")
    iso_w = int(iso_w)
    # Monday date of this ISO week, for the "for the week of" tagline
    monday = dt.date.fromisocalendar(int(iso_y), iso_w, 1)
    return {
        "week": week_id,
        "week_of": monday.isoformat(),
        "subject": f"Yap — Week of {monday.strftime('%b %d')}",
        "headline": "Cook three. Fund one.",
        "dek": ("Three honest meals this week. Turn the dinner-out money "
                "into something better than a receipt you'll throw out."),
        "recipes": [
            {
                "slug": r["slug"],
                "title": r["title"],
                "why": "",  # Matt fills this — one sentence.
            } for r in recipes
        ],
        "savings_math": {
            "dinner_out": 22,
            "dinner_home": 6,
            "meals_per_week": 3,
            "commentary": (
                "At $22 out vs $6 home × 3 meals, that's $48 this week. "
                "Pointed anywhere, that's a night out with actual teeth."
            ),
        },
        "cta_patron": {
            "headline": "If you're cooking to tip better",
            "body": ("Pick one spot this week. Go in Friday. Leave 30%. "
                     "Learn your server's name. Be the regular they tell stories about."),
        },
        "cta_experience": {
            "headline": "If you're cooking to fund the show",
            "body": ("Name the ticket. Set the tab. Watch the $48 stack into $192 "
                     "by month's end. Buy the thing before it sells out."),
        },
        "closing": ("One email a week. That's the whole deal. — Matt"),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--week", default=None, help="ISO week, e.g. 2026-W16")
    ap.add_argument("--force", action="store_true", help="Overwrite existing draft")
    args = ap.parse_args()

    week_id = parse_week(args.week) if args.week else current_week_id()
    YAP_DIR.mkdir(parents=True, exist_ok=True)
    out_path = YAP_DIR / f"{week_id}.yaml"

    if out_path.exists() and not args.force:
        print(f"  Draft already exists: {out_path}")
        print(f"  Edit it, or rerun with --force to overwrite.")
        return 0

    recipes = load_recipes()
    picked = pick_three(recipes, week_id)
    draft = default_draft(week_id, picked)

    with open(out_path, "w") as f:
        yaml.safe_dump(draft, f, sort_keys=False, width=88, allow_unicode=True)

    print(f"  Drafted: {out_path}")
    print(f"  Picks:   {', '.join(r['title'] for r in picked) or '(no recipes found)'}")
    print(f"  Next:    edit this file, then run scripts/render_yap.py --week {week_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
