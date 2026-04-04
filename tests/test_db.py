"""Tests for the dual-database layer (htcah.db + USDA reference)."""
import os
import sys
import tempfile
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.db import DB, USDA_PATH


@pytest.fixture
def db(tmp_path):
    """Fresh test database in a temp directory."""
    test_db = str(tmp_path / "test_htcah.db")
    d = DB(db_path=test_db)
    yield d
    d.close()


# --------------------------------------------------
# SCHEMA & CONNECTION
# --------------------------------------------------
class TestSchema:
    def test_creates_all_tables(self, db):
        tables = {r[0] for r in db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        expected = {
            "tenants", "inventory_items", "dishes", "dish_ingredients",
            "flavor_scores", "nutrition", "reviews", "review_checks",
            "orders", "order_items", "daily_sales", "blends", "blend_tenants",
        }
        assert expected.issubset(tables), f"Missing tables: {expected - tables}"

    def test_usda_attached_if_exists(self, db):
        if os.path.exists(USDA_PATH):
            count = db.conn.execute("SELECT count(*) FROM usda.food").fetchone()[0]
            assert count > 0, "USDA food table should have data"

    def test_wal_mode(self, db):
        mode = db.conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"

    def test_foreign_keys_on(self, db):
        fk = db.conn.execute("PRAGMA foreign_keys").fetchone()[0]
        assert fk == 1


# --------------------------------------------------
# TENANT CRUD
# --------------------------------------------------
class TestTenants:
    def _make_tenant(self, db, slug="test-burger", name="Test Burger"):
        return db.upsert_tenant({
            "slug": slug, "name": name, "type": "partner",
            "city": "Austin", "state": "TX", "zip_code": "78701",
        })

    def test_upsert_creates(self, db):
        slug = self._make_tenant(db)
        assert slug == "test-burger"
        t = db.get_tenant("test-burger")
        assert t is not None
        assert t["name"] == "Test Burger"
        assert t["city"] == "Austin"

    def test_upsert_updates(self, db):
        self._make_tenant(db)
        db.upsert_tenant({"slug": "test-burger", "name": "Updated Burger", "city": "Dallas"})
        t = db.get_tenant("test-burger")
        assert t["name"] == "Updated Burger"
        assert t["city"] == "Dallas"

    def test_list_excludes_platform(self, db):
        db.upsert_tenant({"slug": "htcah", "name": "HTCAH", "type": "platform"})
        self._make_tenant(db)
        tenants = db.list_tenants()
        slugs = [t["slug"] for t in tenants]
        assert "test-burger" in slugs
        assert "htcah" not in slugs

    def test_claim_slug(self, db):
        self._make_tenant(db)
        t = db.get_tenant("test-burger")
        assert t["claimed"] == 0
        result = db.claim_slug("test-burger")
        assert result is True
        t = db.get_tenant("test-burger")
        assert t["claimed"] == 1
        # Can't claim twice
        assert db.claim_slug("test-burger") is False

    def test_claim_nonexistent(self, db):
        assert db.claim_slug("nobody") is False


# --------------------------------------------------
# INVENTORY
# --------------------------------------------------
class TestInventory:
    def _setup(self, db):
        db.upsert_tenant({"slug": "t1", "name": "T1"})
        db.add_inventory_item("t1", {
            "item_name": "ground beef 80/20",
            "vendor_price": 45.00,
            "package_size": 10,
            "package_unit": "lb",
            "unit_price": 4.50,
            "category": "protein",
        })

    def test_add_and_retrieve(self, db):
        self._setup(db)
        items = db.get_inventory("t1")
        assert len(items) == 1
        assert items[0]["item_name"] == "ground beef 80/20"
        assert items[0]["unit_price"] == 4.50

    def test_upsert_updates_price(self, db):
        self._setup(db)
        db.add_inventory_item("t1", {
            "item_name": "ground beef 80/20",
            "vendor_price": 50.00,
            "category": "protein",
        })
        items = db.get_inventory("t1")
        assert len(items) == 1
        assert items[0]["vendor_price"] == 50.00

    def test_stats(self, db):
        self._setup(db)
        stats = db.get_inventory_stats("t1")
        assert stats["total"] == 1
        assert stats["priced"] == 1


# --------------------------------------------------
# DISHES
# --------------------------------------------------
class TestDishes:
    def _setup(self, db):
        db.upsert_tenant({"slug": "t1", "name": "T1"})
        db.add_dish(
            {
                "slug": "t1-smash-burger",
                "tenant_slug": "t1",
                "name": "Smash Burger",
                "food_cost": 2.85,
                "menu_price": 9.99,
                "margin_pct": 71.5,
                "popularity": 85,
                "classification": "star",
                "steps": ["Form ball", "Smash on griddle", "Add cheese"],
            },
            ingredients=[
                {"item_name": "ground beef 80/20", "usage_amount": 0.33, "usage_unit": "lb", "item_cost": 1.49},
                {"item_name": "american cheese", "usage_amount": 2, "usage_unit": "ea", "item_cost": 0.30},
                {"item_name": "brioche bun", "usage_amount": 1, "usage_unit": "ea", "item_cost": 0.45},
            ],
        )

    def test_add_dish_with_ingredients(self, db):
        self._setup(db)
        dishes = db.get_dishes("t1")
        assert len(dishes) == 1
        d = dishes[0]
        assert d["name"] == "Smash Burger"
        assert d["classification"] == "star"
        assert len(d["ingredients"]) == 3
        assert d["steps"] == ["Form ball", "Smash on griddle", "Add cheese"]

    def test_margin_query(self, db):
        self._setup(db)
        high = db.dishes_by_margin("t1", min_margin=70)
        assert len(high) == 1
        low = db.dishes_by_margin("t1", min_margin=90)
        assert len(low) == 0


# --------------------------------------------------
# ORDERS & SALES
# --------------------------------------------------
class TestOrders:
    def _setup(self, db):
        db.upsert_tenant({"slug": "t1", "name": "T1"})
        db.add_dish({
            "slug": "t1-burger", "tenant_slug": "t1", "name": "Burger",
            "food_cost": 3.00, "menu_price": 10.00,
        })
        db.add_dish({
            "slug": "t1-fries", "tenant_slug": "t1", "name": "Fries",
            "food_cost": 0.80, "menu_price": 4.99,
        })

    def test_record_order(self, db):
        self._setup(db)
        order_id = db.record_order("t1", [
            {"dish_slug": "t1-burger", "item_name": "Burger", "quantity": 2, "unit_price": 10.00},
            {"dish_slug": "t1-fries", "item_name": "Fries", "quantity": 1, "unit_price": 4.99},
        ], {"source": "square", "ordered_at": "2026-04-01T18:30:00"})
        assert order_id is not None

        summary = db.get_sales_summary("t1", days=30)
        assert summary["order_count"] == 1

    def test_daily_sales_rebuild(self, db):
        self._setup(db)
        # Record 3 orders across 2 days
        for day in ["2026-04-01", "2026-04-01", "2026-04-02"]:
            db.record_order("t1", [
                {"dish_slug": "t1-burger", "item_name": "Burger", "quantity": 1, "unit_price": 10.00},
            ], {"ordered_at": f"{day}T12:00:00"})

        db.rebuild_daily_sales("t1")
        pop = db.get_dish_popularity("t1", days=30)
        assert "t1-burger" in pop
        assert pop["t1-burger"]["qty"] == 3

    def test_popularity_scoring(self, db):
        self._setup(db)
        # Burger sells 10x, fries sell 2x
        for _ in range(10):
            db.record_order("t1", [
                {"dish_slug": "t1-burger", "item_name": "Burger", "quantity": 1, "unit_price": 10.00},
            ], {"ordered_at": "2026-04-01T12:00:00"})
        for _ in range(2):
            db.record_order("t1", [
                {"dish_slug": "t1-fries", "item_name": "Fries", "quantity": 1, "unit_price": 4.99},
            ], {"ordered_at": "2026-04-01T12:00:00"})

        db.rebuild_daily_sales("t1")
        pop = db.get_dish_popularity("t1", days=30)
        assert pop["t1-burger"]["popularity"] == 100.0  # most popular = 100
        assert pop["t1-fries"]["popularity"] == 20.0     # 2/10 = 20%


# --------------------------------------------------
# REVIEWS
# --------------------------------------------------
class TestReviews:
    def test_save_and_retrieve(self, db):
        db.upsert_tenant({"slug": "t1", "name": "T1"})
        review_id = db.save_review("t1", 72, "C+", "Needs menu work", {
            "inventory": {"score": 8, "grade": "B+", "issues": ["missing produce"]},
            "dishes": {"score": 6, "grade": "C", "issues": ["low variety"]},
        }, ["Add 3 sides", "Fix pricing"])

        r = db.get_review(review_id)
        assert r["overall_score"] == 72
        assert r["grade"] == "C+"
        assert len(r["checks"]) == 2
        assert r["priority_actions"] == ["Add 3 sides", "Fix pricing"]

    def test_history_ordering(self, db):
        db.upsert_tenant({"slug": "t1", "name": "T1"})
        db.save_review("t1", 50, "D", "Bad", {}, [])
        db.save_review("t1", 75, "B", "Better", {}, [])
        history = db.get_review_history("t1")
        assert len(history) == 2
        scores = {h["overall_score"] for h in history}
        assert scores == {50, 75}


# --------------------------------------------------
# YAML SYNC
# --------------------------------------------------
class TestSync:
    def test_sync_loads_data(self, db):
        """Sync from real YAML files if they exist."""
        content_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "content", "tenants"
        )
        if not os.path.isdir(content_dir):
            pytest.skip("No content directory")

        result = db.sync_from_yaml()
        assert result["tenants"] > 0
        # Dishes may be 0 if demo recipes were removed
        tenants = db.list_tenants()


# --------------------------------------------------
# PLATFORM STATS
# --------------------------------------------------
class TestPlatformStats:
    def test_stats_with_empty_db(self, db):
        stats = db.platform_stats()
        assert stats["tenants"] == 0
        assert stats["dishes"] == 0
        assert stats["orders"] == 0
        assert stats["total_revenue"] == 0

    def test_stats_with_data(self, db):
        db.upsert_tenant({"slug": "t1", "name": "T1"})
        db.add_dish({"slug": "t1-x", "tenant_slug": "t1", "name": "X", "menu_price": 10})
        stats = db.platform_stats()
        assert stats["tenants"] == 1
        assert stats["dishes"] == 1
