#!/usr/bin/env python3
"""
CHECKOUT.PY — Stripe integration for HTCAH.

Three products, two flows:
  1. Library purchase (one-time) → Checkout Session mode='payment'
  2. Sprout tier ($9/mo)         → Checkout Session mode='subscription'
  3. Harvest tier ($29/mo)       → Checkout Session mode='subscription'

No auth system. Identity = Stripe customer email + token.
Download delivery = tokenized URL (expires after N downloads or 7 days).
Tenant access = check Stripe subscription status.

Stripe best practices followed:
  - Checkout Sessions API (not raw PaymentIntents)
  - Webhook signature verification
  - Restricted API keys (not secret keys)
  - No keys in source code — read from env vars
  - Dynamic payment methods (no hardcoded payment_method_types)

Usage:
    from app.checkout import (
        create_library_checkout,
        create_tier_checkout,
        handle_webhook,
        verify_download_token,
        get_customer_tier,
    )
"""
import os
import json
import hmac
import hashlib
import secrets
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

# Stripe SDK is optional — we use raw HTTP via requests
# so we don't add a heavy dependency. But if stripe is installed, use it.
try:
    import stripe
    HAS_STRIPE_SDK = True
except ImportError:
    HAS_STRIPE_SDK = False
    import requests

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "data", "htcah.db")

# ──────────────────────────────────────────────
# CONFIG — All from environment, never hardcoded
# ──────────────────────────────────────────────
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")        # sk_... or rk_...
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")  # whsec_...
STRIPE_PUBLISHABLE_KEY = os.environ.get("STRIPE_PUBLISHABLE_KEY", "")  # pk_...

# Product price IDs — create these in Stripe Dashboard first
PRICE_LIBRARY = os.environ.get("STRIPE_PRICE_LIBRARY", "")         # one-time
PRICE_SPROUT = os.environ.get("STRIPE_PRICE_SPROUT", "")           # recurring $9/mo
PRICE_HARVEST = os.environ.get("STRIPE_PRICE_HARVEST", "")         # recurring $29/mo

# Download settings
DOWNLOAD_EXPIRY_DAYS = 7
DOWNLOAD_MAX_USES = 5

# Base URL for redirects
BASE_URL = os.environ.get("BASE_URL", "http://localhost:3050")


def _check_config():
    """Verify Stripe is configured. Returns (ok, missing_keys)."""
    required = {
        "STRIPE_SECRET_KEY": STRIPE_SECRET_KEY,
        "STRIPE_WEBHOOK_SECRET": STRIPE_WEBHOOK_SECRET,
    }
    missing = [k for k, v in required.items() if not v]
    return len(missing) == 0, missing


def _stripe_api(method, endpoint, **kwargs):
    """Call Stripe API. Uses SDK if available, otherwise raw requests."""
    if HAS_STRIPE_SDK:
        stripe.api_key = STRIPE_SECRET_KEY
        # Map endpoint to SDK call
        # This is a thin wrapper — most calls go through create_checkout_session
        raise NotImplementedError("SDK path — use raw requests for now")

    url = f"https://api.stripe.com/v1/{endpoint}"
    headers = {"Authorization": f"Bearer {STRIPE_SECRET_KEY}"}

    if method == "POST":
        resp = requests.post(url, headers=headers, data=kwargs.get("data", {}))
    elif method == "GET":
        resp = requests.get(url, headers=headers, params=kwargs.get("params", {}))
    else:
        raise ValueError(f"Unsupported method: {method}")

    if resp.status_code >= 400:
        return {"error": resp.json().get("error", {}).get("message", resp.text)}
    return resp.json()


# ──────────────────────────────────────────────
# DATABASE — Download tokens + customer records
# ──────────────────────────────────────────────
def _get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # Ensure checkout tables exist
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS customers (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            email           TEXT UNIQUE NOT NULL,
            stripe_id       TEXT,
            tier            TEXT DEFAULT 'free',
            subscription_id TEXT,
            library_purchased INTEGER DEFAULT 0,
            created_at      TEXT DEFAULT (datetime('now')),
            updated_at      TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS download_tokens (
            token           TEXT PRIMARY KEY,
            email           TEXT NOT NULL,
            product         TEXT NOT NULL,
            uses_remaining  INTEGER DEFAULT 5,
            expires_at      TEXT NOT NULL,
            created_at      TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS checkout_events (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type      TEXT NOT NULL,
            stripe_event_id TEXT UNIQUE,
            customer_email  TEXT,
            product         TEXT,
            amount_cents    INTEGER,
            raw_event       TEXT,
            created_at      TEXT DEFAULT (datetime('now'))
        );
    """)
    return conn


# ──────────────────────────────────────────────
# CHECKOUT SESSION CREATION
# ──────────────────────────────────────────────
def create_library_checkout(customer_email=None):
    """Create a Stripe Checkout Session for the library (one-time purchase).

    Returns: {"url": "https://checkout.stripe.com/...", "session_id": "cs_..."}
    """
    ok, missing = _check_config()
    if not ok:
        return {"error": f"Stripe not configured. Missing: {', '.join(missing)}"}

    if not PRICE_LIBRARY:
        return {"error": "STRIPE_PRICE_LIBRARY not set. Create a Price in Stripe Dashboard first."}

    data = {
        "mode": "payment",
        "line_items[0][price]": PRICE_LIBRARY,
        "line_items[0][quantity]": "1",
        "success_url": f"{BASE_URL}/checkout/success?session_id={{CHECKOUT_SESSION_ID}}",
        "cancel_url": f"{BASE_URL}/checkout/cancel",
    }
    if customer_email:
        data["customer_email"] = customer_email

    result = _stripe_api("POST", "checkout/sessions", data=data)
    if "error" in result:
        return result

    return {"url": result.get("url"), "session_id": result.get("id")}


def create_tier_checkout(tier, customer_email=None):
    """Create a Stripe Checkout Session for a tier subscription.

    tier: 'sprout' or 'harvest'
    Returns: {"url": "https://checkout.stripe.com/...", "session_id": "cs_..."}
    """
    ok, missing = _check_config()
    if not ok:
        return {"error": f"Stripe not configured. Missing: {', '.join(missing)}"}

    price_map = {"sprout": PRICE_SPROUT, "harvest": PRICE_HARVEST}
    price_id = price_map.get(tier)
    if not price_id:
        return {"error": f"Unknown tier '{tier}'. Use 'sprout' or 'harvest'."}

    data = {
        "mode": "subscription",
        "line_items[0][price]": price_id,
        "line_items[0][quantity]": "1",
        "success_url": f"{BASE_URL}/checkout/success?session_id={{CHECKOUT_SESSION_ID}}",
        "cancel_url": f"{BASE_URL}/checkout/cancel",
    }
    if customer_email:
        data["customer_email"] = customer_email

    result = _stripe_api("POST", "checkout/sessions", data=data)
    if "error" in result:
        return result

    return {"url": result.get("url"), "session_id": result.get("id")}


# ──────────────────────────────────────────────
# WEBHOOK HANDLING
# ──────────────────────────────────────────────
def verify_webhook_signature(payload_body, sig_header):
    """Verify Stripe webhook signature. Returns True/False."""
    if not STRIPE_WEBHOOK_SECRET:
        return False

    # Stripe signature format: t=timestamp,v1=signature
    try:
        parts = dict(item.split("=", 1) for item in sig_header.split(","))
        timestamp = parts.get("t", "")
        signature = parts.get("v1", "")

        signed_payload = f"{timestamp}.{payload_body}"
        expected = hmac.new(
            STRIPE_WEBHOOK_SECRET.encode(),
            signed_payload.encode(),
            hashlib.sha256
        ).hexdigest()

        return hmac.compare_digest(signature, expected)
    except Exception:
        return False


def handle_webhook(event):
    """Process a verified Stripe webhook event.

    Handles:
      - checkout.session.completed → record purchase, generate download token
      - customer.subscription.updated → update tier
      - customer.subscription.deleted → downgrade to free

    Returns: {"status": "ok", ...} or {"error": ...}
    """
    event_type = event.get("type", "")
    data = event.get("data", {}).get("object", {})

    db = _get_db()

    # Log every event
    db.execute(
        "INSERT OR IGNORE INTO checkout_events (event_type, stripe_event_id, customer_email, raw_event, created_at) VALUES (?, ?, ?, ?, ?)",
        (event_type, event.get("id"), data.get("customer_email", ""), json.dumps(event), datetime.utcnow().isoformat())
    )
    db.commit()

    if event_type == "checkout.session.completed":
        return _handle_checkout_completed(db, data)
    elif event_type == "customer.subscription.updated":
        return _handle_subscription_updated(db, data)
    elif event_type == "customer.subscription.deleted":
        return _handle_subscription_deleted(db, data)
    else:
        return {"status": "ignored", "event_type": event_type}


def _handle_checkout_completed(db, session):
    """Process completed checkout. Generate download token for library purchases."""
    email = session.get("customer_email") or session.get("customer_details", {}).get("email", "")
    mode = session.get("mode", "")
    stripe_customer_id = session.get("customer", "")

    if not email:
        return {"error": "No customer email in session"}

    # Upsert customer
    db.execute("""
        INSERT INTO customers (email, stripe_id, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(email) DO UPDATE SET
            stripe_id = COALESCE(excluded.stripe_id, customers.stripe_id),
            updated_at = excluded.updated_at
    """, (email, stripe_customer_id, datetime.utcnow().isoformat()))

    if mode == "payment":
        # Library purchase — generate download token
        db.execute("UPDATE customers SET library_purchased = 1, updated_at = ? WHERE email = ?",
                   (datetime.utcnow().isoformat(), email))

        token = _generate_download_token(db, email, "library")
        db.commit()
        return {
            "status": "ok",
            "action": "library_purchased",
            "email": email,
            "download_url": f"{BASE_URL}/dl/{token}",
            "token": token,
        }

    elif mode == "subscription":
        # Tier subscription — update customer tier
        subscription_id = session.get("subscription", "")
        # Determine tier from amount
        amount = session.get("amount_total", 0)  # in cents
        tier = "sprout" if amount <= 1000 else "harvest"

        db.execute("""
            UPDATE customers SET tier = ?, subscription_id = ?, updated_at = ?
            WHERE email = ?
        """, (tier, subscription_id, datetime.utcnow().isoformat(), email))
        db.commit()
        return {
            "status": "ok",
            "action": "subscription_started",
            "email": email,
            "tier": tier,
        }

    db.commit()
    return {"status": "ok", "action": "unknown_mode", "mode": mode}


def _handle_subscription_updated(db, subscription):
    """Handle subscription changes (upgrades/downgrades)."""
    stripe_customer_id = subscription.get("customer", "")
    row = db.execute("SELECT email FROM customers WHERE stripe_id = ?", (stripe_customer_id,)).fetchone()
    if not row:
        return {"status": "ignored", "reason": "unknown_customer"}

    # Check plan amount to determine tier
    items = subscription.get("items", {}).get("data", [])
    if items:
        amount = items[0].get("price", {}).get("unit_amount", 0)
        tier = "sprout" if amount <= 1000 else "harvest"
    else:
        tier = "free"

    status = subscription.get("status", "")
    if status in ("canceled", "unpaid", "past_due"):
        tier = "free"

    db.execute("UPDATE customers SET tier = ?, updated_at = ? WHERE email = ?",
               (tier, datetime.utcnow().isoformat(), row["email"]))
    db.commit()
    return {"status": "ok", "action": "tier_updated", "tier": tier}


def _handle_subscription_deleted(db, subscription):
    """Handle subscription cancellation — downgrade to free."""
    stripe_customer_id = subscription.get("customer", "")
    db.execute("""
        UPDATE customers SET tier = 'free', subscription_id = NULL, updated_at = ?
        WHERE stripe_id = ?
    """, (datetime.utcnow().isoformat(), stripe_customer_id))
    db.commit()
    return {"status": "ok", "action": "downgraded_to_free"}


# ──────────────────────────────────────────────
# DOWNLOAD TOKEN SYSTEM
# ──────────────────────────────────────────────
def _generate_download_token(db, email, product):
    """Generate a secure, time-limited download token."""
    token = secrets.token_urlsafe(32)
    expires = (datetime.utcnow() + timedelta(days=DOWNLOAD_EXPIRY_DAYS)).isoformat()

    db.execute(
        "INSERT INTO download_tokens (token, email, product, uses_remaining, expires_at) VALUES (?, ?, ?, ?, ?)",
        (token, email, product, DOWNLOAD_MAX_USES, expires)
    )
    return token


def verify_download_token(token):
    """Check if a download token is valid. Returns (valid, info_dict).

    Decrements uses_remaining on success.
    """
    db = _get_db()
    row = db.execute("SELECT * FROM download_tokens WHERE token = ?", (token,)).fetchone()

    if not row:
        return False, {"error": "Invalid token"}

    if row["uses_remaining"] <= 0:
        return False, {"error": "Download limit reached"}

    if datetime.fromisoformat(row["expires_at"]) < datetime.utcnow():
        return False, {"error": "Token expired"}

    # Decrement uses
    db.execute("UPDATE download_tokens SET uses_remaining = uses_remaining - 1 WHERE token = ?", (token,))
    db.commit()

    return True, {
        "email": row["email"],
        "product": row["product"],
        "uses_remaining": row["uses_remaining"] - 1,
    }


# ──────────────────────────────────────────────
# CUSTOMER QUERIES
# ──────────────────────────────────────────────
def get_customer_tier(email):
    """Get a customer's current tier. Returns 'free' if not found."""
    db = _get_db()
    row = db.execute("SELECT tier, library_purchased FROM customers WHERE email = ?", (email,)).fetchone()
    if not row:
        return {"tier": "free", "library": False}
    return {"tier": row["tier"], "library": bool(row["library_purchased"])}


def get_checkout_status():
    """Return checkout system status for /api/health."""
    ok, missing = _check_config()
    db = _get_db()
    customers = db.execute("SELECT COUNT(*) as c FROM customers").fetchone()["c"]
    tokens = db.execute("SELECT COUNT(*) as c FROM download_tokens WHERE uses_remaining > 0 AND expires_at > ?",
                        (datetime.utcnow().isoformat(),)).fetchone()["c"]
    return {
        "stripe_configured": ok,
        "missing_keys": missing if not ok else [],
        "customers": customers,
        "active_tokens": tokens,
        "products": {
            "library": bool(PRICE_LIBRARY),
            "sprout": bool(PRICE_SPROUT),
            "harvest": bool(PRICE_HARVEST),
        }
    }
