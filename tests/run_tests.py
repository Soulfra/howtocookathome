#!/usr/bin/env python3
"""
HTCAH Test Runner — no external dependencies needed.
Run: python tests/run_tests.py

Tests cover: database layer, onboarding engine, dish generation,
pricing floors, cooking steps, server config, YAML sync, orders/sales.
"""
import sys
import os
import time
import tempfile
import traceback

# Make sure app imports work
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = 0
FAIL = 0
ERRORS = []


def check(name, condition, msg=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  \033[32mPASS\033[0m  {name}")
    else:
        FAIL += 1
        ERRORS.append(f"{name}: {msg}")
        print(f"  \033[31mFAIL\033[0m  {name} -- {msg}")


def run_section(title, fn):
    global FAIL
    print(f"\n{'='*50}")
    print(f"  {title}")
    print(f"{'='*50}")
    try:
        fn()
    except Exception as e:
        FAIL += 1
        ERRORS.append(f"{title}: CRASHED — {e}")
        print(f"  \033[31mCRASH\033[0m {e}")
        traceback.print_exc()


# ==================================================
# DATABASE TESTS
# ==================================================
def test_database():
    from app.db import DB, USDA_PATH

    tmp = tempfile.mkdtemp()
    db = DB(db_path=os.path.join(tmp, "test.db"))

    # Schema
    tables = {r[0] for r in db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    expected = {
        "tenants", "inventory_items", "dishes", "dish_ingredients",
        "flavor_scores", "nutrition", "reviews", "review_checks",
        "orders", "order_items", "daily_sales", "blends", "blend_tenants",
    }
    check("all 13 tables created", expected.issubset(tables), f"missing: {expected - tables}")
    check("WAL mode", db.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal")
    check("foreign keys on", db.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1)

    if os.path.exists(USDA_PATH):
        count = db.conn.execute("SELECT count(*) FROM usda.food").fetchone()[0]
        check("USDA attached", count > 0, f"got {count} foods")

    # Tenants
    db.upsert_tenant({"slug": "t1", "name": "Test Burger", "type": "partner", "city": "Austin", "state": "TX"})
    t = db.get_tenant("t1")
    check("create tenant", t is not None and t["name"] == "Test Burger")
    check("tenant location", t["city"] == "Austin" and t["state"] == "TX")

    db.upsert_tenant({"slug": "t1", "name": "Updated", "city": "Dallas"})
    check("update tenant", db.get_tenant("t1")["name"] == "Updated")

    db.upsert_tenant({"slug": "plat", "name": "Platform", "type": "platform"})
    check("list excludes platform", all(t["slug"] != "plat" for t in db.list_tenants()))

    check("claim works", db.claim_slug("t1") is True)
    check("double claim fails", db.claim_slug("t1") is False)
    check("claim nonexistent fails", db.claim_slug("nobody") is False)

    # Inventory
    db.add_inventory_item("t1", {"item_name": "ground beef", "vendor_price": 45.0, "unit_price": 4.50, "category": "protein"})
    items = db.get_inventory("t1")
    check("add inventory", len(items) == 1 and items[0]["unit_price"] == 4.50)

    db.add_inventory_item("t1", {"item_name": "ground beef", "vendor_price": 50.0, "category": "protein"})
    check("upsert inventory", db.get_inventory("t1")[0]["vendor_price"] == 50.0)
    check("inventory stats", db.get_inventory_stats("t1")["total"] == 1)

    # Dishes
    db.add_dish(
        {"slug": "t1-burger", "tenant_slug": "t1", "name": "Smash Burger",
         "food_cost": 2.85, "menu_price": 9.99, "margin_pct": 71.5,
         "classification": "star", "steps": ["Form", "Smash", "Cheese"]},
        ingredients=[{"item_name": "ground beef", "usage_amount": 0.33, "item_cost": 1.49}],
    )
    dishes = db.get_dishes("t1")
    check("add dish", len(dishes) == 1 and dishes[0]["name"] == "Smash Burger")
    check("dish ingredients", len(dishes[0]["ingredients"]) == 1)
    check("dish steps", dishes[0]["steps"] == ["Form", "Smash", "Cheese"])
    check("margin query hit", len(db.dishes_by_margin("t1", 70)) == 1)
    check("margin query miss", len(db.dishes_by_margin("t1", 90)) == 0)

    # Orders & Sales
    db.add_dish({"slug": "t1-fries", "tenant_slug": "t1", "name": "Fries", "food_cost": 0.80, "menu_price": 4.99})
    oid = db.record_order("t1", [
        {"dish_slug": "t1-burger", "item_name": "Burger", "quantity": 2, "unit_price": 10.0},
        {"dish_slug": "t1-fries", "item_name": "Fries", "quantity": 1, "unit_price": 4.99},
    ], {"source": "square", "ordered_at": "2026-04-01T18:30:00"})
    check("record order", oid is not None)
    check("sales summary", db.get_sales_summary("t1", days=30)["order_count"] == 1)

    for _ in range(9):
        db.record_order("t1", [{"dish_slug": "t1-burger", "item_name": "Burger", "quantity": 1, "unit_price": 10.0}],
                        {"ordered_at": "2026-04-01T12:00:00"})
    db.record_order("t1", [{"dish_slug": "t1-fries", "item_name": "Fries", "quantity": 1, "unit_price": 4.99}],
                    {"ordered_at": "2026-04-01T12:00:00"})
    db.rebuild_daily_sales("t1")
    pop = db.get_dish_popularity("t1", days=30)
    check("popularity computed", "t1-burger" in pop and pop["t1-burger"]["qty"] > 0)
    check("popularity scaling", pop["t1-burger"]["popularity"] == 100.0)

    # Reviews
    rid = db.save_review("t1", 72, "C+", "Needs work",
                         {"inv": {"score": 8, "grade": "B+"}, "dishes": {"score": 6, "grade": "C"}},
                         ["Fix pricing"])
    r = db.get_review(rid)
    check("save review", r["overall_score"] == 72 and r["grade"] == "C+")
    check("review checks", len(r["checks"]) == 2)
    check("priority actions", r["priority_actions"] == ["Fix pricing"])

    # YAML sync
    result = db.sync_from_yaml()
    check("sync tenants", result["tenants"] > 0, f'got {result["tenants"]}')
    check("sync dishes", result["dishes"] > 0, f'got {result["dishes"]}')

    # Platform stats
    stats = db.platform_stats()
    check("stats keys", all(k in stats for k in ["tenants", "dishes", "orders", "total_revenue"]))

    db.close()


# ==================================================
# ONBOARDING TESTS
# ==================================================
def test_onboarding():
    from app.onboard import slugify, create_dishes_from_inventory

    check("slugify basic", slugify("Big Red's BBQ") == "big-reds-bbq")
    check("slugify passthrough", slugify("smash-stack-burgers") == "smash-stack-burgers")

    inv = [
        {"item": "Ground Beef 80/20", "unit_price": 4.50, "unit": "lb", "category": "protein"},
        {"item": "American Cheese", "unit_price": 3.20, "unit": "lb", "category": "dairy"},
        {"item": "Brioche Buns", "unit_price": 0.45, "unit": "ea", "category": "bread"},
        {"item": "Bacon", "unit_price": 6.00, "unit": "lb", "category": "protein"},
        {"item": "Lettuce", "unit_price": 1.50, "unit": "lb", "category": "produce"},
        {"item": "Tomato", "unit_price": 2.00, "unit": "lb", "category": "produce"},
        {"item": "Onion", "unit_price": 1.00, "unit": "lb", "category": "produce"},
        {"item": "Pickles", "unit_price": 3.00, "unit": "gal", "category": "produce"},
        {"item": "Ketchup", "unit_price": 4.00, "unit": "gal", "category": "condiment"},
        {"item": "Mustard", "unit_price": 3.50, "unit": "gal", "category": "condiment"},
        {"item": "Salt", "unit_price": 1.00, "unit": "lb", "category": "spice"},
        {"item": "Black Pepper", "unit_price": 8.00, "unit": "lb", "category": "spice"},
        {"item": "Frozen Fries", "unit_price": 2.50, "unit": "lb", "category": "frozen"},
        {"item": "Fry Oil", "unit_price": 15.00, "unit": "gal", "category": "oil"},
        {"item": "Vanilla Custard Base", "unit_price": 8.00, "unit": "gal", "category": "dairy"},
    ]

    dishes, yaml_strs = create_dishes_from_inventory("test-burger-joint", inv)
    names = [d["name"] for d in dishes]
    print(f"  Generated: {names}")

    check("produces dishes", len(dishes) > 0)
    check("produces YAML", len(yaml_strs) == len(dishes))
    check("has burger dishes", any("burger" in n.lower() or "smash" in n.lower() for n in names))

    # Price floors
    for d in dishes:
        price = d.get("suggested_price", d.get("suggested_menu_price", d.get("menu_price", 0)))
        name = d["name"].lower()
        if "fries" in name:
            check(f"{d['name']} price floor", price >= 3.99, f"${price}")
        if "burger" in name or "smash" in name or "deluxe" in name:
            check(f"{d['name']} price floor", price >= 7.99, f"${price}")
        if "custard" in name:
            check(f"{d['name']} price floor", price >= 4.99, f"${price}")

    # Every dish has ingredients and steps
    for d in dishes:
        check(f"{d['name']} has ingredients", len(d.get("ingredients", [])) > 0)
        check(f"{d['name']} has steps", len(d.get("steps", [])) > 0)

    # Burger steps are burger-specific
    for d in dishes:
        if "burger" in d["name"].lower():
            step_text = " ".join(d.get("steps", [])).lower()
            check(f"{d['name']} has burger steps",
                  "smash" in step_text or "griddle" in step_text or "form" in step_text)

    # Bacon burger has bacon
    for d in dishes:
        if "bacon" in d["name"].lower():
            ing_names = [i.get("item", "").lower() for i in d.get("ingredients", [])]
            check(f"{d['name']} has bacon", any("bacon" in i for i in ing_names))


# ==================================================
# SERVER TESTS
# ==================================================
def test_server():
    from app.server import RESERVED_SLUGS, PLATFORM_HOSTS, app

    check("reserved slugs", {"www", "api", "admin", "htcah"}.issubset(RESERVED_SLUGS))
    check("platform hosts", {"localhost", "howtocookathome.com"}.issubset(PLATFORM_HOSTS))
    check("WSGI app callable", callable(app))


# ==================================================
# RUN ALL
# ==================================================
if __name__ == "__main__":
    start = time.time()

    run_section("DATABASE", test_database)
    run_section("ONBOARDING ENGINE", test_onboarding)
    run_section("SERVER", test_server)

    elapsed = time.time() - start

    print(f"\n{'='*50}")
    if FAIL == 0:
        print(f"  \033[32m{PASS} PASSED\033[0m in {elapsed:.1f}s")
    else:
        print(f"  \033[32m{PASS} passed\033[0m, \033[31m{FAIL} failed\033[0m in {elapsed:.1f}s")
        for e in ERRORS:
            print(f"    \033[31m✗\033[0m {e}")
    print(f"{'='*50}\n")

    sys.exit(1 if FAIL else 0)
