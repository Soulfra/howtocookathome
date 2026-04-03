#!/usr/bin/env python3
"""
LOCAL_AI.PY — Point a small model at HTCAH data.

The model doesn't need to be smart. The DATA is smart.
The model just reads SQLite and speaks English.

Runs on: phone, laptop, potato with a battery.
Requires: ollama serve (or any OpenAI-compatible local endpoint)
"""
import sqlite3, os, json, requests

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "data", "htcah.db")  # app data (tenants, dishes, orders)
USDA_PATH = os.path.join(BASE_DIR, "data", "usda", "foundation.db")  # read-only USDA reference
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
MODEL = os.environ.get("OLLAMA_MODEL", "llama3.2")


def query_model(prompt, model=None):
    """Ask the local model. Falls back to just returning the data if no model running."""
    try:
        r = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={"model": model or MODEL, "prompt": prompt, "stream": False},
            timeout=60,
        )
        r.raise_for_status()
        return r.json()["response"]
    except Exception:
        return None  # Model not running — that's fine, return raw data


def get_context(tenant_slug=None):
    """Pull the context window for the model — everything it needs to answer."""
    ctx = {}

    # Platform stats
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    ctx["tenants"] = [dict(r) for r in db.execute("SELECT slug, name, type FROM tenants WHERE type != 'platform'").fetchall()]
    ctx["tenant_count"] = len(ctx["tenants"])
    ctx["total_inventory"] = db.execute("SELECT COUNT(*) FROM inventory_items").fetchone()[0]
    ctx["total_dishes"] = db.execute("SELECT COUNT(*) FROM dishes").fetchone()[0]
    ctx["usda_match_rate"] = db.execute(
        "SELECT ROUND(100.0 * SUM(CASE WHEN usda_fdc_id IS NOT NULL THEN 1 ELSE 0 END) / COUNT(*), 1) FROM inventory_items WHERE match_source != 'noise_skip'"
    ).fetchone()[0]

    if tenant_slug:
        # Tenant-specific context
        ctx["inventory"] = [dict(r) for r in db.execute(
            "SELECT item_name, display_name, vendor_price, usda_description, match_source FROM inventory_items WHERE tenant_slug=?",
            (tenant_slug,)
        ).fetchall()]
        ctx["dishes"] = [dict(r) for r in db.execute(
            "SELECT name, food_cost, menu_price, margin_pct, classification FROM dishes WHERE tenant_slug=?",
            (tenant_slug,)
        ).fetchall()]
        ctx["flavor_scores"] = [dict(r) for r in db.execute(
            "SELECT d.name, f.dimension, f.score FROM flavor_scores f JOIN dishes d ON f.dish_slug = d.slug WHERE d.tenant_slug=? ORDER BY d.name, f.dimension",
            (tenant_slug,)
        ).fetchall()]

    # USDA stats (same database)
    ctx["usda_food_count"] = db.execute("SELECT COUNT(*) FROM food").fetchone()[0]

    db.close()
    return ctx


def build_prompt(question, tenant_slug=None):
    """Build a prompt with real data baked in. The model reads, not thinks."""
    ctx = get_context(tenant_slug)

    system = f"""You are a restaurant intelligence assistant for the HowToCookAtHome platform.
You have access to USDA FoodData Central ({ctx['usda_food_count']} foods) and {ctx['tenant_count']} restaurant tenants.
Platform stats: {ctx['total_inventory']} inventory items, {ctx['total_dishes']} dishes, {ctx['usda_match_rate']}% USDA match rate.

Tenants: {', '.join(t['name'] for t in ctx['tenants'])}"""

    if tenant_slug and "dishes" in ctx:
        system += f"\n\nTenant: {tenant_slug}"
        system += f"\nDishes: {json.dumps(ctx['dishes'], indent=2)}"
        if ctx.get("flavor_scores"):
            system += f"\nFlavor profiles: {json.dumps(ctx['flavor_scores'], indent=2)}"
        if ctx.get("inventory"):
            # Just show first 10 to keep context small
            system += f"\nInventory (sample): {json.dumps(ctx['inventory'][:10], indent=2)}"

    return f"{system}\n\nQuestion: {question}\n\nAnswer concisely using the data above."


def ask(question, tenant_slug=None):
    """The whole thing. Ask a question, get an answer grounded in real data."""
    prompt = build_prompt(question, tenant_slug)

    # Try the local model first
    answer = query_model(prompt)
    if answer:
        return {"answer": answer, "source": "local_model", "model": MODEL}

    # No model? Return the raw context — the data IS the answer
    ctx = get_context(tenant_slug)
    return {"answer": None, "source": "raw_data", "context": ctx}


# --- CLI interface ---
if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python3 -m app.ai 'your question' [tenant-slug]")
        print("\nExamples:")
        print("  python3 -m app.ai 'what restaurants are on the platform?'")
        print("  python3 -m app.ai 'what is the margin on brisket?' big-reds-bbq")
        print("  python3 -m app.ai 'which dishes have the highest umami?' big-reds-bbq")
        sys.exit(0)

    question = sys.argv[1]
    tenant = sys.argv[2] if len(sys.argv) > 2 else None

    result = ask(question, tenant)

    if result["source"] == "local_model":
        print(result["answer"])
    else:
        print("(No local model running — returning raw data)")
        print(json.dumps(result["context"], indent=2, default=str))
