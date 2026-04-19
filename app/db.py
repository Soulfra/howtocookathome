#!/usr/bin/env python3
"""
DB.PY -- Dual-database layer for HTCAH.

Two SQLite databases, clear separation:

  data/usda/foundation.db  (READ-ONLY reference)
    USDA FoodData Central -- 84K+ foods, nutrients, categories, portions.
    Rebuilt when USDA releases new data. Never written to by the app.

  data/htcah.db  (READ-WRITE app data)
    tenants          -- restaurant accounts
    inventory_items  -- what they buy (vendor invoices)
    dishes           -- what they sell (menu items)
    dish_ingredients -- junction: dish -> inventory item + usage amounts
    flavor_scores    -- per-dish flavor dimensions (from USDA nutrients)
    nutrition        -- per-dish-ingredient nutrition from USDA
    reviews          -- timestamped review snapshots (versioned)
    review_checks    -- individual check scores per review
    orders           -- POS order data (Square, Toast, manual)
    order_items      -- line items per order -> dish slugs
    daily_sales      -- aggregated daily sales per dish (for menu engineering)
    blends           -- co-pack spice blend definitions
    blend_tenants    -- which tenants use which blends + their ratios

USDA data is ATTACHed as 'usda' for cross-database joins (nutrition lookups).

Usage:
    from app.db import DB
    db = DB()                        # opens/creates htcah.db, attaches USDA
    db.upsert_tenant({...})
    db.add_inventory_item(tenant_slug, {...})
    db.add_dish({...}, ingredients=[...])
    dishes = db.get_dishes(tenant_slug)
    review_id = db.save_review(tenant_slug, score, grade, summary, checks, actions)
    db.record_order(tenant_slug, items=[...])
    popularity = db.get_dish_popularity(tenant_slug, days=30)
"""
import sqlite3
import json
import os
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# HTCAH_DATA_DIR lets ops point the app at a persistent disk mount
# (Render: /var/data; local dev: the repo's data/ folder by default).
DATA_DIR = os.environ.get("HTCAH_DATA_DIR") or os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "htcah.db")
# USDA stays bundled with the code (read-only reference, built via scripts/build_db.sh)
USDA_PATH = os.path.join(BASE_DIR, "data", "usda", "foundation.db")


class DB:
    def __init__(self, db_path=None):
        self.path = db_path or DB_PATH
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        # Attach USDA as read-only reference if it exists
        if os.path.exists(USDA_PATH):
            self.conn.execute(f"ATTACH DATABASE '{USDA_PATH}' AS usda")
        self._init_schema()
        self._migrate()

    def _init_schema(self):
        """Create platform tables if they don't exist. USDA tables are in the attached db."""
        self.conn.executescript("""

        -- ==========================================
        -- TENANTS
        -- ==========================================
        CREATE TABLE IF NOT EXISTS tenants (
            slug          TEXT PRIMARY KEY,
            name          TEXT NOT NULL,
            type          TEXT DEFAULT 'partner',
            email_hash    TEXT,
            token_hash    TEXT,
            tagline       TEXT DEFAULT '',
            city          TEXT DEFAULT '',
            state         TEXT DEFAULT '',
            zip_code      TEXT DEFAULT '',
            country       TEXT DEFAULT 'US',
            claimed       INTEGER DEFAULT 0,
            claimed_at    TEXT,
            colors        TEXT DEFAULT '{}',
            fonts         TEXT DEFAULT '{}',
            nav           TEXT DEFAULT '[]',
            footer        TEXT DEFAULT '{}',
            logo_text     TEXT DEFAULT '',
            menu_recs     TEXT DEFAULT '[]',
            created_at    TEXT DEFAULT (datetime('now')),
            updated_at    TEXT DEFAULT (datetime('now'))
        );

        -- ==========================================
        -- INVENTORY ITEMS
        -- ==========================================
        CREATE TABLE IF NOT EXISTS inventory_items (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_slug     TEXT NOT NULL REFERENCES tenants(slug),
            item_name       TEXT NOT NULL,
            display_name    TEXT,
            original_line   TEXT,
            vendor_price    REAL,
            package_size    REAL,
            package_unit    TEXT,
            unit_price      REAL,
            unit_price_label TEXT,
            usda_fdc_id     INTEGER,
            usda_description TEXT,
            match_source    TEXT,
            category        TEXT,
            created_at      TEXT DEFAULT (datetime('now')),
            UNIQUE(tenant_slug, item_name)
        );
        CREATE INDEX IF NOT EXISTS idx_inv_tenant ON inventory_items(tenant_slug);
        CREATE INDEX IF NOT EXISTS idx_inv_usda ON inventory_items(usda_fdc_id);

        -- ==========================================
        -- DISHES
        -- ==========================================
        CREATE TABLE IF NOT EXISTS dishes (
            slug            TEXT PRIMARY KEY,
            tenant_slug     TEXT NOT NULL REFERENCES tenants(slug),
            name            TEXT NOT NULL,
            cuisine         TEXT DEFAULT '',
            type            TEXT DEFAULT 'auto_generated',
            food_cost       REAL DEFAULT 0,
            menu_price      REAL DEFAULT 0,
            margin_pct      REAL DEFAULT 0,
            popularity      REAL DEFAULT 0,
            classification  TEXT DEFAULT '',
            steps           TEXT DEFAULT '[]',
            auto_generated  INTEGER DEFAULT 1,
            version         TEXT DEFAULT '1.0',
            created_at      TEXT DEFAULT (datetime('now')),
            updated_at      TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_dish_tenant ON dishes(tenant_slug);

        -- ==========================================
        -- DISH INGREDIENTS (junction table)
        -- ==========================================
        CREATE TABLE IF NOT EXISTS dish_ingredients (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            dish_slug       TEXT NOT NULL REFERENCES dishes(slug),
            item_name       TEXT NOT NULL,
            usage_amount    REAL,
            usage_unit      TEXT,
            item_cost       REAL,
            usda_fdc_id     INTEGER,
            display_name    TEXT,
            match_source    TEXT,
            UNIQUE(dish_slug, item_name)
        );
        CREATE INDEX IF NOT EXISTS idx_di_dish ON dish_ingredients(dish_slug);

        -- ==========================================
        -- FLAVOR SCORES
        -- ==========================================
        CREATE TABLE IF NOT EXISTS flavor_scores (
            dish_slug       TEXT NOT NULL REFERENCES dishes(slug),
            dimension       TEXT NOT NULL,
            score           REAL NOT NULL,
            raw_score       REAL,
            source          TEXT DEFAULT 'usda',
            PRIMARY KEY (dish_slug, dimension)
        );

        -- ==========================================
        -- NUTRITION (per-dish-ingredient)
        -- ==========================================
        CREATE TABLE IF NOT EXISTS nutrition (
            dish_slug       TEXT NOT NULL,
            item_name       TEXT NOT NULL,
            fdc_id          INTEGER,
            calories        REAL,
            protein_g       REAL,
            fat_g           REAL,
            carbs_g         REAL,
            PRIMARY KEY (dish_slug, item_name)
        );

        -- ==========================================
        -- REVIEWS (versioned snapshots)
        -- ==========================================
        CREATE TABLE IF NOT EXISTS reviews (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_slug     TEXT NOT NULL REFERENCES tenants(slug),
            overall_score   INTEGER NOT NULL,
            grade           TEXT NOT NULL,
            summary         TEXT,
            priority_actions TEXT DEFAULT '[]',
            created_at      TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_review_tenant ON reviews(tenant_slug);

        -- ==========================================
        -- REVIEW CHECKS (detail rows per review)
        -- ==========================================
        CREATE TABLE IF NOT EXISTS review_checks (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            review_id       INTEGER NOT NULL REFERENCES reviews(id),
            check_name      TEXT NOT NULL,
            score           INTEGER NOT NULL,
            grade           TEXT NOT NULL,
            weight          REAL DEFAULT 1.0,
            issues          TEXT DEFAULT '[]',
            actions         TEXT DEFAULT '[]'
        );
        CREATE INDEX IF NOT EXISTS idx_rc_review ON review_checks(review_id);

        -- ==========================================
        -- ORDERS (POS data -- Square, Toast, manual entry)
        -- Each row = one transaction/ticket
        -- ==========================================
        CREATE TABLE IF NOT EXISTS orders (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_slug     TEXT NOT NULL REFERENCES tenants(slug),
            order_ref       TEXT,
            source          TEXT DEFAULT 'manual',
            subtotal        REAL DEFAULT 0,
            tax             REAL DEFAULT 0,
            total           REAL DEFAULT 0,
            tip             REAL DEFAULT 0,
            payment_method  TEXT DEFAULT '',
            order_type      TEXT DEFAULT 'dine_in',
            guest_count     INTEGER DEFAULT 1,
            ordered_at      TEXT NOT NULL,
            created_at      TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_order_tenant ON orders(tenant_slug);
        CREATE INDEX IF NOT EXISTS idx_order_date ON orders(ordered_at);

        -- ==========================================
        -- ORDER ITEMS (line items per order)
        -- Links back to dishes for popularity tracking
        -- ==========================================
        CREATE TABLE IF NOT EXISTS order_items (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id        INTEGER NOT NULL REFERENCES orders(id),
            dish_slug       TEXT REFERENCES dishes(slug),
            item_name       TEXT NOT NULL,
            quantity        INTEGER DEFAULT 1,
            unit_price      REAL DEFAULT 0,
            modifiers       TEXT DEFAULT '[]',
            created_at      TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_oi_order ON order_items(order_id);
        CREATE INDEX IF NOT EXISTS idx_oi_dish ON order_items(dish_slug);

        -- ==========================================
        -- DAILY SALES (aggregated -- rebuilt nightly or on demand)
        -- This powers menu engineering with REAL numbers
        -- ==========================================
        CREATE TABLE IF NOT EXISTS daily_sales (
            tenant_slug     TEXT NOT NULL REFERENCES tenants(slug),
            dish_slug       TEXT NOT NULL REFERENCES dishes(slug),
            sale_date       TEXT NOT NULL,
            qty_sold        INTEGER DEFAULT 0,
            revenue         REAL DEFAULT 0,
            food_cost_total REAL DEFAULT 0,
            PRIMARY KEY (tenant_slug, dish_slug, sale_date)
        );
        CREATE INDEX IF NOT EXISTS idx_ds_tenant_date ON daily_sales(tenant_slug, sale_date);

        -- ==========================================
        -- BLENDS (co-pack spice blend definitions)
        -- ==========================================
        CREATE TABLE IF NOT EXISTS blends (
            blend_name      TEXT PRIMARY KEY,
            spices          TEXT NOT NULL,
            default_ratio   TEXT DEFAULT '{}',
            threshold_met   INTEGER DEFAULT 0,
            created_at      TEXT DEFAULT (datetime('now'))
        );

        -- ==========================================
        -- BLEND TENANTS
        -- ==========================================
        CREATE TABLE IF NOT EXISTS blend_tenants (
            blend_name      TEXT NOT NULL REFERENCES blends(blend_name),
            tenant_slug     TEXT NOT NULL REFERENCES tenants(slug),
            custom_ratio    TEXT DEFAULT '{}',
            ratio_source    TEXT DEFAULT 'default',
            PRIMARY KEY (blend_name, tenant_slug)
        );

        -- ==========================================
        -- SUBSCRIBERS (weekly cook-at-home dividend list)
        -- Captured from /tip-better and /next-show landing pages.
        -- bucket = 'patron' (tip-better) | 'experience' (next-show)
        -- ==========================================
        CREATE TABLE IF NOT EXISTS subscribers (
            email               TEXT NOT NULL,
            bucket              TEXT NOT NULL,
            goal                TEXT DEFAULT '',
            weekly_saving       REAL DEFAULT 0,
            source              TEXT DEFAULT '',
            unsubscribe_token   TEXT DEFAULT '',
            unsubscribed_at     TEXT DEFAULT NULL,
            created_at          TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (email, bucket)
        );
        CREATE INDEX IF NOT EXISTS idx_sub_bucket ON subscribers(bucket);
        CREATE INDEX IF NOT EXISTS idx_sub_token ON subscribers(unsubscribe_token);

        """)
        self.conn.commit()

    def _migrate(self):
        """Add columns that may be missing from older databases."""
        existing = {r[1] for r in self.conn.execute("PRAGMA table_info(tenants)").fetchall()}
        migrations = [
            ("city",       "TEXT DEFAULT ''"),
            ("state",      "TEXT DEFAULT ''"),
            ("zip_code",   "TEXT DEFAULT ''"),
            ("country",    "TEXT DEFAULT 'US'"),
            ("claimed",    "INTEGER DEFAULT 0"),
            ("claimed_at", "TEXT"),
        ]
        for col, typedef in migrations:
            if col not in existing:
                self.conn.execute(f"ALTER TABLE tenants ADD COLUMN {col} {typedef}")
        # subscribers migration (added with unsubscribe flow)
        sub_cols = {r[1] for r in self.conn.execute("PRAGMA table_info(subscribers)").fetchall()}
        for col, typedef in [
            ("unsubscribe_token", "TEXT DEFAULT ''"),
            ("unsubscribed_at",   "TEXT DEFAULT NULL"),
        ]:
            if sub_cols and col not in sub_cols:
                self.conn.execute(f"ALTER TABLE subscribers ADD COLUMN {col} {typedef}")
        self.conn.commit()

    # --------------------------------------------------
    # TENANT CRUD
    # --------------------------------------------------
    def upsert_tenant(self, data):
        """Insert or update a tenant. data is a dict with at minimum 'slug' and 'name'."""
        self.conn.execute("""
            INSERT INTO tenants (slug, name, type, email_hash, token_hash, tagline,
                                 city, state, zip_code,
                                 colors, fonts, nav, footer, logo_text)
            VALUES (:slug, :name, :type, :email_hash, :token_hash, :tagline,
                    :city, :state, :zip_code,
                    :colors, :fonts, :nav, :footer, :logo_text)
            ON CONFLICT(slug) DO UPDATE SET
                name=:name, type=:type, tagline=:tagline,
                city=:city, state=:state, zip_code=:zip_code,
                colors=:colors, fonts=:fonts, nav=:nav, footer=:footer,
                logo_text=:logo_text, updated_at=datetime('now')
        """, {
            "slug": data["slug"],
            "name": data["name"],
            "type": data.get("type", "partner"),
            "email_hash": data.get("email_hash", ""),
            "token_hash": data.get("token_hash", ""),
            "tagline": data.get("tagline", ""),
            "city": data.get("city", ""),
            "state": data.get("state", ""),
            "zip_code": data.get("zip_code", ""),
            "colors": json.dumps(data.get("colors", {})),
            "fonts": json.dumps(data.get("fonts", {})),
            "nav": json.dumps(data.get("nav", [])),
            "footer": json.dumps(data.get("footer", {})),
            "logo_text": data.get("logo_text", data["name"]),
        })
        self.conn.commit()
        return data["slug"]

    def check_slug(self, slug):
        """Check if a slug is available, reserved, or taken."""
        from app.server import RESERVED_SLUGS
        if slug in RESERVED_SLUGS:
            return {"available": False, "reason": "reserved", "suggestion": None}

        row = self.conn.execute("SELECT slug, name, claimed FROM tenants WHERE slug=?", (slug,)).fetchone()
        if row:
            return {
                "available": False,
                "reason": "claimed" if row["claimed"] else "taken_free",
                "owner": row["name"],
                "suggestion": None,
            }

        # Also check YAML files (onboard may have created one without DB entry)
        yaml_path = os.path.join(BASE_DIR, "content", "tenants", f"{slug}.yaml")
        if os.path.exists(yaml_path):
            return {
                "available": False,
                "reason": "taken_free",
                "owner": slug,
                "suggestion": None,
            }

        return {"available": True, "reason": "open", "suggestion": None}

    def suggest_slug(self, base_name, city="", state=""):
        """Generate slug candidates for a name, with location fallbacks."""
        from app.onboard import slugify
        base = slugify(base_name)
        candidates = [base]
        if city:
            candidates.append(f"{base}-{slugify(city)}")
        if state:
            candidates.append(f"{base}-{slugify(state)}")
        if city and state:
            candidates.append(f"{base}-{slugify(city)}-{slugify(state)}")

        results = []
        for slug in candidates:
            check = self.check_slug(slug)
            results.append({"slug": slug, "available": check["available"], "reason": check["reason"]})
        return results

    def claim_slug(self, slug):
        """Lock a slug as paid/claimed. Returns True if successful."""
        row = self.conn.execute("SELECT slug, claimed FROM tenants WHERE slug=?", (slug,)).fetchone()
        if not row:
            return False
        if row["claimed"]:
            return False
        self.conn.execute(
            "UPDATE tenants SET claimed=1, claimed_at=datetime('now') WHERE slug=?", (slug,)
        )
        self.conn.commit()
        return True

    def get_tenant(self, slug):
        """Get a tenant as a dict with JSON fields parsed."""
        row = self.conn.execute("SELECT * FROM tenants WHERE slug=?", (slug,)).fetchone()
        if not row:
            return None
        d = dict(row)
        for k in ("colors", "fonts", "nav", "footer", "menu_recs"):
            if d.get(k):
                d[k] = json.loads(d[k])
        return d

    def list_tenants(self):
        """List all non-platform tenants."""
        rows = self.conn.execute(
            "SELECT slug, name, type, created_at FROM tenants WHERE type != 'platform' ORDER BY name"
        ).fetchall()
        return [dict(r) for r in rows]

    # --------------------------------------------------
    # INVENTORY CRUD
    # --------------------------------------------------
    def add_inventory_item(self, tenant_slug, item):
        """Add or update an inventory item."""
        self.conn.execute("""
            INSERT INTO inventory_items
                (tenant_slug, item_name, display_name, original_line,
                 vendor_price, package_size, package_unit, unit_price, unit_price_label,
                 usda_fdc_id, usda_description, match_source, category)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(tenant_slug, item_name) DO UPDATE SET
                vendor_price=excluded.vendor_price,
                package_size=excluded.package_size,
                usda_fdc_id=excluded.usda_fdc_id,
                usda_description=excluded.usda_description,
                match_source=excluded.match_source,
                category=excluded.category
        """, (
            tenant_slug,
            item.get("item_name", item.get("item", "")),
            item.get("display_name", ""),
            item.get("original_line", item.get("original", "")),
            item.get("vendor_price"),
            item.get("package_size"),
            item.get("package_unit", ""),
            item.get("unit_price"),
            item.get("unit_price_label", ""),
            item.get("usda_fdc_id"),
            item.get("usda_description", ""),
            item.get("match_source", item.get("_match_source", "")),
            item.get("category", ""),
        ))
        self.conn.commit()

    def get_inventory(self, tenant_slug):
        """Get all inventory items for a tenant."""
        rows = self.conn.execute(
            "SELECT * FROM inventory_items WHERE tenant_slug=? ORDER BY category, item_name",
            (tenant_slug,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_inventory_stats(self, tenant_slug):
        """Quick stats for a tenant's inventory."""
        row = self.conn.execute("""
            SELECT count(*) as total,
                   sum(CASE WHEN vendor_price IS NOT NULL THEN 1 ELSE 0 END) as priced,
                   sum(CASE WHEN usda_fdc_id IS NOT NULL THEN 1 ELSE 0 END) as usda_matched,
                   sum(vendor_price) as total_cost
            FROM inventory_items WHERE tenant_slug=?
        """, (tenant_slug,)).fetchone()
        return dict(row) if row else {}

    # --------------------------------------------------
    # DISH CRUD
    # --------------------------------------------------
    def add_dish(self, data, ingredients=None):
        """Add or update a dish with optional ingredients."""
        self.conn.execute("""
            INSERT INTO dishes
                (slug, tenant_slug, name, cuisine, type, food_cost, menu_price,
                 margin_pct, popularity, classification, steps, auto_generated, version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(slug) DO UPDATE SET
                name=excluded.name, food_cost=excluded.food_cost,
                menu_price=excluded.menu_price, margin_pct=excluded.margin_pct,
                popularity=excluded.popularity, classification=excluded.classification,
                steps=excluded.steps, updated_at=datetime('now')
        """, (
            data["slug"],
            data["tenant_slug"],
            data["name"],
            data.get("cuisine", ""),
            data.get("type", "auto_generated"),
            data.get("food_cost", 0),
            data.get("menu_price", 0),
            data.get("margin_pct", 0),
            data.get("popularity", 0),
            data.get("classification", ""),
            json.dumps(data.get("steps", [])),
            1 if data.get("auto_generated", True) else 0,
            data.get("version", "1.0"),
        ))

        if ingredients:
            for ing in ingredients:
                self.conn.execute("""
                    INSERT INTO dish_ingredients
                        (dish_slug, item_name, usage_amount, usage_unit, item_cost,
                         usda_fdc_id, display_name, match_source)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(dish_slug, item_name) DO UPDATE SET
                        usage_amount=excluded.usage_amount,
                        item_cost=excluded.item_cost,
                        usda_fdc_id=excluded.usda_fdc_id
                """, (
                    data["slug"],
                    ing.get("item_name", ing.get("item", "")),
                    ing.get("usage_amount"),
                    ing.get("usage_unit", ""),
                    ing.get("item_cost", 0),
                    ing.get("usda_fdc_id"),
                    ing.get("display_name", ""),
                    ing.get("match_source", ing.get("_match_source", "")),
                ))
        self.conn.commit()

    def get_dishes(self, tenant_slug):
        """Get all dishes for a tenant with their ingredients."""
        dishes = []
        rows = self.conn.execute(
            "SELECT * FROM dishes WHERE tenant_slug=? ORDER BY name", (tenant_slug,)
        ).fetchall()
        for row in rows:
            d = dict(row)
            d["steps"] = json.loads(d["steps"]) if d["steps"] else []
            ings = self.conn.execute(
                "SELECT * FROM dish_ingredients WHERE dish_slug=? ORDER BY item_name",
                (d["slug"],)
            ).fetchall()
            d["ingredients"] = [dict(i) for i in ings]
            dishes.append(d)
        return dishes

    # --------------------------------------------------
    # ORDERS & SALES (POS integration)
    # --------------------------------------------------
    def record_order(self, tenant_slug, items, order_data=None):
        """Record an order with line items.
        items: list of dicts with dish_slug, item_name, quantity, unit_price
        order_data: optional dict with order_ref, source, subtotal, tax, total, etc.
        Returns order_id.
        """
        od = order_data or {}
        subtotal = od.get("subtotal", sum(i.get("unit_price", 0) * i.get("quantity", 1) for i in items))
        cur = self.conn.execute("""
            INSERT INTO orders (tenant_slug, order_ref, source, subtotal, tax, total,
                                tip, payment_method, order_type, guest_count, ordered_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            tenant_slug,
            od.get("order_ref", ""),
            od.get("source", "manual"),
            subtotal,
            od.get("tax", 0),
            od.get("total", subtotal),
            od.get("tip", 0),
            od.get("payment_method", ""),
            od.get("order_type", "dine_in"),
            od.get("guest_count", 1),
            od.get("ordered_at", datetime.now().isoformat()),
        ))
        order_id = cur.lastrowid

        for item in items:
            self.conn.execute("""
                INSERT INTO order_items (order_id, dish_slug, item_name, quantity, unit_price, modifiers)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                order_id,
                item.get("dish_slug"),
                item.get("item_name", ""),
                item.get("quantity", 1),
                item.get("unit_price", 0),
                json.dumps(item.get("modifiers", [])),
            ))
        self.conn.commit()
        return order_id

    def rebuild_daily_sales(self, tenant_slug, start_date=None):
        """Aggregate order_items into daily_sales for menu engineering.
        Call after importing POS data or nightly.
        """
        where = "WHERE o.tenant_slug = ?"
        params = [tenant_slug]
        if start_date:
            where += " AND date(o.ordered_at) >= ?"
            params.append(start_date)

        # Clear existing aggregates for the range
        del_where = "WHERE tenant_slug = ?"
        del_params = [tenant_slug]
        if start_date:
            del_where += " AND sale_date >= ?"
            del_params.append(start_date)
        self.conn.execute(f"DELETE FROM daily_sales {del_where}", del_params)

        # Rebuild from orders
        self.conn.execute(f"""
            INSERT INTO daily_sales (tenant_slug, dish_slug, sale_date, qty_sold, revenue, food_cost_total)
            SELECT o.tenant_slug, oi.dish_slug, date(o.ordered_at) as sale_date,
                   sum(oi.quantity), sum(oi.quantity * oi.unit_price),
                   sum(oi.quantity * COALESCE(d.food_cost, 0))
            FROM orders o
            JOIN order_items oi ON o.id = oi.order_id
            LEFT JOIN dishes d ON oi.dish_slug = d.slug
            {where}
            AND oi.dish_slug IS NOT NULL
            GROUP BY o.tenant_slug, oi.dish_slug, date(o.ordered_at)
        """, params)
        self.conn.commit()

    def get_dish_popularity(self, tenant_slug, days=30):
        """Get real popularity scores from sales data for menu engineering.
        Returns dict of {dish_slug: {'qty': int, 'revenue': float, 'popularity': 0-100}}
        """
        rows = self.conn.execute("""
            SELECT dish_slug,
                   sum(qty_sold) as total_qty,
                   sum(revenue) as total_revenue,
                   count(DISTINCT sale_date) as days_sold
            FROM daily_sales
            WHERE tenant_slug = ?
              AND sale_date >= date('now', ?)
            GROUP BY dish_slug
            ORDER BY total_qty DESC
        """, (tenant_slug, f"-{days} days")).fetchall()

        if not rows:
            return {}

        results = {}
        max_qty = max(r["total_qty"] for r in rows) if rows else 1
        for r in rows:
            results[r["dish_slug"]] = {
                "qty": r["total_qty"],
                "revenue": r["total_revenue"],
                "days_sold": r["days_sold"],
                "popularity": round((r["total_qty"] / max_qty) * 100, 1) if max_qty > 0 else 0,
            }
        return results

    def get_sales_summary(self, tenant_slug, days=30):
        """High-level sales stats for a tenant."""
        row = self.conn.execute("""
            SELECT count(DISTINCT id) as order_count,
                   sum(total) as gross_revenue,
                   avg(total) as avg_ticket,
                   sum(tip) as total_tips,
                   count(DISTINCT date(ordered_at)) as active_days
            FROM orders
            WHERE tenant_slug = ?
              AND ordered_at >= datetime('now', ?)
        """, (tenant_slug, f"-{days} days")).fetchone()
        return dict(row) if row else {}

    # --------------------------------------------------
    # FLAVOR SCORES
    # --------------------------------------------------
    def save_flavor_scores(self, dish_slug, scores, source="merged"):
        """Save normalized flavor scores for a dish."""
        for dim, val in scores.items():
            self.conn.execute("""
                INSERT INTO flavor_scores (dish_slug, dimension, score, source)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(dish_slug, dimension) DO UPDATE SET
                    score=excluded.score, source=excluded.source
            """, (dish_slug, dim, round(val, 2), source))
        self.conn.commit()

    def get_flavor_scores(self, dish_slug):
        """Get flavor profile for a dish as a dict."""
        rows = self.conn.execute(
            "SELECT dimension, score, source FROM flavor_scores WHERE dish_slug=? ORDER BY score DESC",
            (dish_slug,)
        ).fetchall()
        return {r["dimension"]: {"score": r["score"], "source": r["source"]} for r in rows}

    # --------------------------------------------------
    # REVIEWS (versioned history)
    # --------------------------------------------------
    def save_review(self, tenant_slug, overall_score, grade, summary, checks, priority_actions):
        """Save a review snapshot. Returns the review ID."""
        cur = self.conn.execute("""
            INSERT INTO reviews (tenant_slug, overall_score, grade, summary, priority_actions)
            VALUES (?, ?, ?, ?, ?)
        """, (tenant_slug, overall_score, grade, summary, json.dumps(priority_actions)))
        review_id = cur.lastrowid

        for check_name, data in checks.items():
            self.conn.execute("""
                INSERT INTO review_checks
                    (review_id, check_name, score, grade, weight, issues, actions)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                review_id,
                check_name,
                data["score"],
                data["grade"],
                data.get("weight", 1.0),
                json.dumps(data.get("issues", [])),
                json.dumps(data.get("actions", [])),
            ))
        self.conn.commit()
        return review_id

    def get_review_history(self, tenant_slug, limit=10):
        """Get review history for a tenant, newest first."""
        rows = self.conn.execute("""
            SELECT id, overall_score, grade, summary, created_at
            FROM reviews WHERE tenant_slug=?
            ORDER BY created_at DESC LIMIT ?
        """, (tenant_slug, limit)).fetchall()
        return [dict(r) for r in rows]

    def get_review(self, review_id):
        """Get a full review with all checks."""
        row = self.conn.execute("SELECT * FROM reviews WHERE id=?", (review_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["priority_actions"] = json.loads(d["priority_actions"]) if d["priority_actions"] else []
        checks = self.conn.execute(
            "SELECT * FROM review_checks WHERE review_id=? ORDER BY check_name", (review_id,)
        ).fetchall()
        d["checks"] = {}
        for c in checks:
            cd = dict(c)
            cd["issues"] = json.loads(cd["issues"]) if cd["issues"] else []
            cd["actions"] = json.loads(cd["actions"]) if cd["actions"] else []
            d["checks"][cd["check_name"]] = cd
        return d

    def get_latest_review(self, tenant_slug):
        """Get the most recent review for a tenant."""
        row = self.conn.execute(
            "SELECT id FROM reviews WHERE tenant_slug=? ORDER BY created_at DESC LIMIT 1",
            (tenant_slug,)
        ).fetchone()
        if not row:
            return None
        return self.get_review(row["id"])

    # --------------------------------------------------
    # QUERIES
    # --------------------------------------------------
    def dishes_by_margin(self, tenant_slug=None, min_margin=0):
        """Find dishes by contribution margin."""
        sql = "SELECT * FROM dishes WHERE margin_pct >= ?"
        params = [min_margin]
        if tenant_slug:
            sql += " AND tenant_slug=?"
            params.append(tenant_slug)
        sql += " ORDER BY margin_pct DESC"
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def dishes_by_flavor(self, dimension, min_score=5.0):
        """Find dishes strong in a specific flavor dimension."""
        rows = self.conn.execute("""
            SELECT d.slug, d.name, d.tenant_slug, fs.score, fs.dimension
            FROM dishes d
            JOIN flavor_scores fs ON d.slug = fs.dish_slug
            WHERE fs.dimension=? AND fs.score >= ?
            ORDER BY fs.score DESC
        """, (dimension, min_score)).fetchall()
        return [dict(r) for r in rows]

    def unmatched_ingredients(self, tenant_slug=None):
        """Find inventory items that didn't match USDA."""
        sql = "SELECT * FROM inventory_items WHERE usda_fdc_id IS NULL"
        params = []
        if tenant_slug:
            sql += " AND tenant_slug=?"
            params.append(tenant_slug)
        sql += " ORDER BY item_name"
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def platform_stats(self):
        """Dashboard stats across the whole platform."""
        stats = {}
        stats["tenants"] = self.conn.execute("SELECT count(*) FROM tenants WHERE type != 'platform'").fetchone()[0]
        stats["inventory_items"] = self.conn.execute("SELECT count(*) FROM inventory_items").fetchone()[0]
        stats["dishes"] = self.conn.execute("SELECT count(*) FROM dishes").fetchone()[0]
        stats["reviews"] = self.conn.execute("SELECT count(*) FROM reviews").fetchone()[0]
        stats["orders"] = self.conn.execute("SELECT count(*) FROM orders").fetchone()[0]

        # USDA stats from attached db (if available)
        try:
            stats["usda_foods"] = self.conn.execute("SELECT count(*) FROM usda.food").fetchone()[0]
        except Exception:
            stats["usda_foods"] = 0

        avg = self.conn.execute("SELECT avg(overall_score) FROM reviews").fetchone()[0]
        stats["avg_review_score"] = round(avg, 1) if avg else 0

        # Sales stats
        sales = self.conn.execute("""
            SELECT count(*) as total_orders, COALESCE(sum(total), 0) as total_revenue
            FROM orders
        """).fetchone()
        stats["total_orders"] = sales["total_orders"]
        stats["total_revenue"] = round(sales["total_revenue"], 2)

        return stats

    # --------------------------------------------------
    # YAML SYNC -- populate DB from existing YAML files
    # --------------------------------------------------
    def sync_from_yaml(self):
        """Import tenants, inventory, and dishes from YAML files into htcah.db.
        This is the migration bridge: YAML = source of truth during dev,
        DB catches up and eventually becomes the primary store.
        """
        import yaml
        content_dir = os.path.join(BASE_DIR, "content")

        # 1. Sync tenants
        tenant_dir = os.path.join(content_dir, "tenants")
        tenant_count = 0
        if os.path.isdir(tenant_dir):
            for fname in os.listdir(tenant_dir):
                if not fname.endswith(".yaml"):
                    continue
                with open(os.path.join(tenant_dir, fname)) as f:
                    t = yaml.safe_load(f) or {}
                if not t.get("slug"):
                    t["slug"] = fname.replace(".yaml", "")
                if not t.get("name"):
                    t["name"] = t["slug"].replace("-", " ").title()
                self.upsert_tenant(t)
                tenant_count += 1

        # 2. Sync inventory
        inv_dir = os.path.join(content_dir, "inventories")
        inv_count = 0
        if os.path.isdir(inv_dir):
            for fname in os.listdir(inv_dir):
                if not fname.endswith(".yaml"):
                    continue
                tenant_slug = fname.replace(".yaml", "").replace("-inventory", "")
                with open(os.path.join(inv_dir, fname)) as f:
                    data = yaml.safe_load(f) or {}
                items = data.get("items") or data.get("inventory") or []
                if isinstance(data, list):
                    items = data
                for item in items:
                    if isinstance(item, dict):
                        self.add_inventory_item(tenant_slug, item)
                        inv_count += 1

        # 3. Sync dishes/recipes
        recipe_dir = os.path.join(content_dir, "recipes")
        dish_count = 0
        if os.path.isdir(recipe_dir):
            for fname in os.listdir(recipe_dir):
                if not fname.endswith(".yaml"):
                    continue
                with open(os.path.join(recipe_dir, fname)) as f:
                    r = yaml.safe_load(f) or {}
                if not r.get("slug"):
                    r["slug"] = fname.replace(".yaml", "")
                # Figure out tenant_slug from the recipe
                tenant_slug = r.get("tenant_slug", r.get("tenant", ""))
                if not tenant_slug:
                    # Derive from slug: everything before the last dish-name part
                    # e.g., "smash-stack-burgers-classic-smash-burger" -> tenant = "smash-stack-burgers"
                    # Fallback: check if any tenant slug is a prefix
                    for t_row in self.conn.execute("SELECT slug FROM tenants ORDER BY length(slug) DESC").fetchall():
                        if r["slug"].startswith(t_row["slug"] + "-"):
                            tenant_slug = t_row["slug"]
                            break
                if not tenant_slug:
                    continue  # can't place this dish

                dish_data = {
                    "slug": r["slug"],
                    "tenant_slug": tenant_slug,
                    "name": r.get("name", r.get("title", "")),
                    "cuisine": r.get("cuisine", ""),
                    "type": r.get("type", "auto_generated"),
                    "food_cost": r.get("food_cost", 0),
                    "menu_price": r.get("suggested_price", r.get("menu_price", 0)),
                    "margin_pct": r.get("margin_pct", 0),
                    "popularity": r.get("popularity", 50),
                    "classification": r.get("classification", ""),
                    "steps": r.get("steps", []),
                }
                ingredients = r.get("ingredients", [])
                self.add_dish(dish_data, ingredients)
                dish_count += 1

        return {"tenants": tenant_count, "inventory": inv_count, "dishes": dish_count}

    def close(self):
        self.conn.close()


    # --------------------------------------------------
    # SUBSCRIBERS (landing page capture)
    # --------------------------------------------------
    _EMAIL_RE = None
    @classmethod
    def _valid_email(cls, email):
        """Strict-enough email validation: local@domain.tld, TLD >= 2 chars,
        no whitespace, no control chars. Not RFC-complete, but rejects the
        obvious garbage (`a@b.c`, `foo`, `x y@z.com`)."""
        import re as _re
        if cls._EMAIL_RE is None:
            cls._EMAIL_RE = _re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
        if not email or len(email) > 254:
            return False
        return bool(cls._EMAIL_RE.match(email))

    def add_subscriber(self, email, bucket, goal="", weekly_saving=0, source=""):
        """Insert a subscriber (or update fields if they re-submit).

        Generates a stable unsubscribe_token on first insert. A re-subscribe
        (after unsubscribing) clears unsubscribed_at and keeps the same token.

        bucket: 'patron' (/tip-better) or 'experience' (/next-show).
        Returns dict with status.
        """
        import secrets as _secrets
        email = (email or "").strip().lower()
        bucket = (bucket or "").strip().lower()
        if not self._valid_email(email):
            return {"ok": False, "error": "invalid email"}
        if bucket not in ("patron", "experience"):
            return {"ok": False, "error": "invalid bucket"}
        token = _secrets.token_urlsafe(18)
        try:
            self.conn.execute("""
                INSERT INTO subscribers
                    (email, bucket, goal, weekly_saving, source, unsubscribe_token)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(email, bucket) DO UPDATE SET
                    goal=excluded.goal,
                    weekly_saving=excluded.weekly_saving,
                    source=excluded.source,
                    unsubscribed_at=NULL
            """, (email, bucket, goal, float(weekly_saving or 0), source, token))
            self.conn.commit()
            row = self.conn.execute(
                "SELECT unsubscribe_token FROM subscribers WHERE email=? AND bucket=?",
                (email, bucket)
            ).fetchone()
            return {
                "ok": True,
                "email": email,
                "bucket": bucket,
                "unsubscribe_token": row["unsubscribe_token"] if row else token,
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def get_active_subscribers(self, bucket=None):
        """Return active (not unsubscribed) subscribers as list of dicts."""
        if bucket:
            rows = self.conn.execute(
                "SELECT email, bucket, goal, weekly_saving, source, unsubscribe_token, created_at "
                "FROM subscribers WHERE unsubscribed_at IS NULL AND bucket=? "
                "ORDER BY created_at",
                (bucket,)
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT email, bucket, goal, weekly_saving, source, unsubscribe_token, created_at "
                "FROM subscribers WHERE unsubscribed_at IS NULL "
                "ORDER BY created_at"
            ).fetchall()
        return [dict(r) for r in rows]

    def unsubscribe_by_token(self, token):
        """Mark any subscriber rows with this token as unsubscribed.

        Same token can map to both buckets for the same person if they
        signed up for both; we unsubscribe them from all of them.
        """
        token = (token or "").strip()
        if not token or len(token) < 8:
            return {"ok": False, "error": "invalid token"}
        cur = self.conn.execute(
            "UPDATE subscribers SET unsubscribed_at=datetime('now') "
            "WHERE unsubscribe_token=? AND unsubscribed_at IS NULL",
            (token,)
        )
        self.conn.commit()
        if cur.rowcount == 0:
            # token unknown OR already unsubscribed — don't leak which
            return {"ok": True, "count": 0}
        return {"ok": True, "count": cur.rowcount}

    # --------------------------------------------------
    # TENANT CLAIM FLOW
    # A tenant's page exists (auto-generated from public info + YAML).
    # Owner claims ownership by receiving a token via email.
    # --------------------------------------------------
    def generate_claim_token(self, slug, email):
        """Start a claim on tenant <slug>. Store a hashed token in the tenants row;
        return the *unhashed* token so the caller can email it. Caller never sees
        the hash. Only the hash is stored.

        Returns: {ok, token} on success, {ok:false, error} on failure.
        """
        import secrets as _secrets, hashlib as _hashlib
        slug = (slug or "").strip().lower()
        email = (email or "").strip().lower()
        if not slug:
            return {"ok": False, "error": "missing slug"}
        if not self._valid_email(email):
            return {"ok": False, "error": "invalid email"}

        row = self.conn.execute(
            "SELECT slug, claimed FROM tenants WHERE slug=?", (slug,)
        ).fetchone()
        if not row:
            return {"ok": False, "error": "tenant not found"}
        if row["claimed"]:
            # Already claimed — don't leak that we already know
            return {"ok": False, "error": "already claimed"}

        token = _secrets.token_urlsafe(24)
        token_hash = _hashlib.sha256(token.encode()).hexdigest()
        email_hash = _hashlib.sha256(email.encode()).hexdigest()
        self.conn.execute(
            "UPDATE tenants SET token_hash=?, email_hash=?, updated_at=datetime('now') WHERE slug=?",
            (token_hash, email_hash, slug)
        )
        self.conn.commit()
        return {"ok": True, "token": token, "slug": slug}

    def verify_claim_token(self, slug, token):
        """Consume a claim token: mark tenant as claimed if token matches.
        Token can only be used once — after success, token_hash is cleared.
        """
        import hashlib as _hashlib
        slug = (slug or "").strip().lower()
        token = (token or "").strip()
        if not slug or not token or len(token) < 8:
            return {"ok": False, "error": "invalid request"}
        token_hash = _hashlib.sha256(token.encode()).hexdigest()
        row = self.conn.execute(
            "SELECT slug, claimed, token_hash FROM tenants WHERE slug=?", (slug,)
        ).fetchone()
        if not row:
            return {"ok": False, "error": "tenant not found"}
        if row["claimed"]:
            return {"ok": False, "error": "already claimed"}
        if not row["token_hash"] or row["token_hash"] != token_hash:
            return {"ok": False, "error": "invalid or expired token"}
        self.conn.execute(
            "UPDATE tenants SET claimed=1, claimed_at=datetime('now'), token_hash=NULL, updated_at=datetime('now') WHERE slug=?",
            (slug,)
        )
        self.conn.commit()
        return {"ok": True, "slug": slug}

    def is_tenant_claimed(self, slug):
        row = self.conn.execute(
            "SELECT claimed FROM tenants WHERE slug=?", ((slug or "").strip().lower(),)
        ).fetchone()
        return bool(row and row["claimed"])

    def delete_subscriber_by_token(self, token):
        """Permanently remove subscriber rows with this token (GDPR right-to-deletion).
        Unlike unsubscribe (which marks `unsubscribed_at`), this fully deletes the row.
        """
        token = (token or "").strip()
        if not token or len(token) < 8:
            return {"ok": False, "error": "invalid token"}
        cur = self.conn.execute(
            "DELETE FROM subscribers WHERE unsubscribe_token=?",
            (token,)
        )
        self.conn.commit()
        return {"ok": True, "count": cur.rowcount}

    def subscriber_counts(self):
        """Quick stats for eval/health dashboards. Only counts active subs."""
        rows = self.conn.execute(
            "SELECT bucket, COUNT(*) FROM subscribers "
            "WHERE unsubscribed_at IS NULL GROUP BY bucket"
        ).fetchall()
        return {b: c for b, c in rows}


# --------------------------------------------------
# CLI: quick test / sync
# --------------------------------------------------
if __name__ == "__main__":
    import sys
    db = DB()

    if len(sys.argv) > 1 and sys.argv[1] == "sync":
        print("\n  Syncing YAML -> htcah.db ...")
        result = db.sync_from_yaml()
        print(f"    Tenants:   {result['tenants']}")
        print(f"    Inventory: {result['inventory']}")
        print(f"    Dishes:    {result['dishes']}")
        print("  Done.\n")
    else:
        print("\n  HTCAH Database")
        print("  ==============\n")
        stats = db.platform_stats()
        for k, v in stats.items():
            print(f"    {k:20s}: {v}")
        print()
        print("  Run 'python -m app.db sync' to import from YAML files.\n")

    db.close()
