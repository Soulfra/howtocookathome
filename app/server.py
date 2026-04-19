#!/usr/bin/env python3
"""
SERVER.PY — Serves the published HTCAH site.
Local dev: python3 -m app.server
Render:    gunicorn app.server:app

Routes:
  /                → output/web/index.html
  /api/health      → output/api/health.json
  /api/recipes     → output/api/recipes.json
  /api/episodes    → output/api/episodes.json
  /api/brand       → output/api/brand.json
  /<page>.html     → output/web/<page>.html
  /r/<slug>        → output/web/r_<slug>.html  (enriched recipes)
  /recipe/<slug>   → output/web/recipe_<slug>.html (S1 episodes)

Tenant routes (subdomain or /t/<slug>/):
  /cookbook.pdf     → generates and serves tenant cookbook PDF on demand
"""
import os, json, mimetypes, tempfile
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, unquote
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import sys; sys.path.insert(0, BASE_DIR)
from app.onboard import process_onboard
from app.cookbook import build_cookbook
from app.checkout import (
    create_library_checkout, create_tier_checkout,
    handle_webhook, verify_webhook_signature,
    verify_download_token, get_checkout_status,
)
WEB_DIR = os.path.join(BASE_DIR, "output", "web")
STATIC_WEB_DIR = os.path.join(BASE_DIR, "static", "web")
API_DIR = os.path.join(BASE_DIR, "output", "api")
TEMPLATE_DIR = os.path.join(BASE_DIR, "templates")
STATIC_DIR = os.path.join(BASE_DIR, "static")


def _find_web_file(filename):
    """Look for a file in output/web/ first, then fall back to static/web/."""
    primary = os.path.join(WEB_DIR, filename)
    if os.path.isfile(primary):
        return primary
    fallback = os.path.join(STATIC_WEB_DIR, filename)
    if os.path.isfile(fallback):
        return fallback
    return None
PORT = int(os.environ.get("PORT", 3050))

# Domains that host the main platform (not tenant subdomains)
# Requests to these go to the platform site. Everything else is a tenant subdomain.
PLATFORM_HOSTS = {
    "howtocookathome.com", "www.howtocookathome.com",
    "death2data.com", "www.death2data.com",
    "localhost", "127.0.0.1",
}

# Reserved subdomains — these can NEVER be claimed as tenant slugs.
# If someone visits api.howtocookathome.com it should 404, not serve a tenant.
RESERVED_SLUGS = {
    "www", "api", "app", "admin", "blog", "shop", "mail", "smtp", "imap",
    "ftp", "cdn", "static", "assets", "docs", "help", "support", "status",
    "billing", "pay", "dashboard", "login", "auth", "oauth", "sso",
    "menu", "menus", "recipes", "episodes", "about", "onboard", "signup",
    "dev", "staging", "test", "demo", "beta", "preview", "sandbox",
    "ns1", "ns2", "mx", "spf", "dkim", "dmarc", "autoconfig", "autodiscover",
    "platform", "htcah", "howtocookathome",
}


def _claim_page_shell(inner_html):
    """Shared HTML shell for /claim/* pages — same black/red aesthetic as V5/V6 landings."""
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Claim your spot | HowToCookAtHome</title>
<meta name="theme-color" content="#000000">
<style>
html,body{{background:#000;color:#fff;margin:0;padding:0;min-height:100vh;
font-family:-apple-system,system-ui,"Segoe UI",Roboto,Arial,sans-serif;
display:flex;align-items:center;justify-content:center;}}
.card{{max-width:560px;padding:3rem 2rem;text-align:left;width:100%;box-sizing:border-box;}}
h1{{font-weight:900;letter-spacing:-0.02em;text-transform:uppercase;
font-size:clamp(1.85rem,5vw,2.75rem);line-height:1;margin:0 0 1rem;}}
h1 .r{{color:#D91E18;}}
p{{font-size:1.02rem;line-height:1.5;opacity:0.88;margin:0 0 1rem;}}
a{{color:#D91E18;text-decoration:none;font-weight:700;}}
a:hover{{text-decoration:underline;}}
form{{display:flex;flex-direction:column;gap:0.6rem;margin-top:1.5rem;}}
input[type=email]{{background:#0A0A0A;border:2px solid #1F1F1F;color:#fff;
font:inherit;font-size:1rem;padding:0.95rem 1rem;min-height:52px;
transition:border-color 0.15s;}}
input[type=email]:focus{{outline:none;border-color:#D91E18;}}
input[type=email]::placeholder{{color:#8A8A8A;}}
button{{background:#D91E18;color:#fff;border:0;font:inherit;font-weight:900;
font-size:1.05rem;text-transform:uppercase;letter-spacing:0.06em;
padding:1rem;cursor:pointer;min-height:56px;transition:background 0.15s;}}
button:hover{{background:#B31812;}}
button:disabled{{opacity:0.55;cursor:wait;}}
.msg{{margin-top:0.85rem;font-weight:700;font-size:0.95rem;min-height:1.2em;}}
.msg.ok{{color:#2DD18A;}}
.msg.err{{color:#FF6B64;}}
.tag{{margin-top:2rem;font-size:0.8rem;color:#8A8A8A;line-height:1.5;}}
.tag a{{color:#8A8A8A;text-decoration:underline;}}
</style></head><body>
<div class="card">{inner_html}</div></body></html>"""


def _claim_form_html(slug, tenant_name):
    return _claim_page_shell(f"""
<h1>Claim <span class="r">{tenant_name}</span></h1>
<p>Is this your business? Drop your email at the business's domain (or one you can prove you own) and we'll send you a one-time link to confirm.</p>
<p>After you verify, you'll control the page — edit the description, add hours, show off your menu.</p>
<form id="f">
    <input type="email" name="email" placeholder="owner@{slug}.com" required
           autocomplete="email" inputmode="email" spellcheck="false">
    <button type="submit">Send verification link</button>
    <div class="msg" id="msg" aria-live="polite"></div>
</form>
<p class="tag">
  Not the owner of {tenant_name}? <a href="/">Back to HowToCookAtHome.</a><br>
  Already claimed before? Your original verification email still works.
</p>
<script>
document.getElementById('f').addEventListener('submit', async (e) => {{
    e.preventDefault();
    const btn = e.target.querySelector('button');
    const email = e.target.email.value.trim();
    const msg = document.getElementById('msg');
    msg.className = 'msg'; msg.textContent = '';
    btn.disabled = true;
    try {{
        const r = await fetch('/api/claim/{slug}', {{
            method: 'POST',
            headers: {{'Content-Type':'application/json'}},
            body: JSON.stringify({{email}})
        }});
        const d = await r.json();
        if (r.ok && d.ok) {{
            msg.className = 'msg ok';
            msg.textContent = d.message || 'Check your inbox.';
            btn.textContent = 'Sent ✓';
        }} else {{
            msg.className = 'msg err';
            msg.textContent = d.error || 'Something went sideways.';
            btn.disabled = false;
        }}
    }} catch (_) {{
        msg.className = 'msg err';
        msg.textContent = 'Network hiccup.';
        btn.disabled = false;
    }}
}});
</script>""")


def _claimed_already_html(tenant_name):
    return _claim_page_shell(f"""
<h1><span class="r">{tenant_name}</span> is claimed.</h1>
<p>This page already has an owner. If that's you, check your email for your edit link — or reach out for a fresh one.</p>
<p class="tag"><a href="/">Back to HowToCookAtHome.</a></p>""")


def _claim_verify_html(slug, result):
    if result.get("ok"):
        return _claim_page_shell(f"""
<h1>Verified. <span class="r">You own it.</span></h1>
<p>You've claimed the page for <strong>{slug}</strong>. We'll be in touch about next steps (editing, pricing).</p>
<p class="tag"><a href="/t/{slug}">View your page.</a> · <a href="/">Back home.</a></p>""")
    err = result.get("error", "Something went wrong.")
    return _claim_page_shell(f"""
<h1>That link didn't work.</h1>
<p>{err}</p>
<p class="tag"><a href="/claim/{slug}">Try again</a> or <a href="/">head home</a>.</p>""")


def _unsubscribe_html(result):
    """Tiny branded HTML page confirming unsubscribe. result is dict from
    DB.unsubscribe_by_token(). We don't expose whether the token was valid
    vs already-used — that's a small privacy property."""
    count = int(result.get("count", 0)) if result.get("ok") else 0
    if count > 0:
        msg_h = "You're unsubscribed."
        msg_p = ("We pulled your email from the weekly drop. "
                 "No more yap. No more emails.")
    else:
        msg_h = "All set."
        msg_p = ("That link has already been used, or never was active. "
                 "Either way — you're not on the list.")
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Unsubscribed | HowToCookAtHome</title>
<style>
html,body{{background:#000;color:#fff;margin:0;padding:0;min-height:100vh;
font-family:-apple-system,system-ui,Segoe UI,Roboto,Arial,sans-serif;
display:flex;align-items:center;justify-content:center;}}
.card{{max-width:520px;padding:3rem 2rem;text-align:left;}}
h1{{font-weight:900;letter-spacing:-0.02em;text-transform:uppercase;
font-size:clamp(2rem,6vw,3.25rem);line-height:1;margin:0 0 1rem;}}
h1 .r{{color:#D91E18;}}
p{{font-size:1.05rem;line-height:1.5;opacity:0.88;}}
a{{color:#D91E18;text-decoration:none;font-weight:700;}}
a:hover{{text-decoration:underline;}}
.tag{{margin-top:2rem;font-size:0.85rem;color:#8A8A8A;}}
</style></head><body>
<div class="card">
<h1>{msg_h.replace("unsubscribed.", "<span class='r'>unsubscribed.</span>")}</h1>
<p>{msg_p}</p>
<p class="tag">Changed your mind? <a href="/tip-better">Tip better</a> or <a href="/next-show">fund your next show</a>.</p>
</div></body></html>"""


def _extract_tenant_from_host(host_header):
    """Pull the tenant slug from a subdomain like bigreds.howtocookathome.com.

    Returns None if it's the bare domain (platform site) or localhost.
    """
    if not host_header:
        return None
    # Strip port (localhost:8080, etc.)
    hostname = host_header.split(":")[0].lower().strip()

    # If it's a known platform host, no tenant
    if hostname in PLATFORM_HOSTS:
        return None

    # Check for subdomain pattern: <slug>.howtocookathome.com or <slug>.death2data.com
    for apex in ("howtocookathome.com", "death2data.com"):
        if hostname.endswith(f".{apex}"):
            slug = hostname[: -(len(apex) + 1)]  # everything before .domain.com
            if slug and slug not in RESERVED_SLUGS:
                return slug

    # On Render: <slug>.onrender.com during dev/preview
    if hostname.endswith(".onrender.com"):
        # Don't treat the app's own subdomain as a tenant
        # e.g. howtocookathome.onrender.com is platform, not a tenant
        return None

    return None


# --- Rate limiter (in-memory, per-IP, sliding window) ---
# Prevents someone from POSTing /api/subscribe in a loop with fake emails.
# Resets when the process restarts (fine for a single-worker starter tier).
_RATE_STATE = {}  # ip -> [timestamp1, timestamp2, ...]
_RATE_WINDOW_SEC = 60
_RATE_MAX_REQUESTS = 5

def _rate_limit_check(ip):
    """Returns True if request is allowed, False if rate-limited."""
    import time as _time
    now = _time.time()
    cutoff = now - _RATE_WINDOW_SEC
    history = [t for t in _RATE_STATE.get(ip, []) if t > cutoff]
    if len(history) >= _RATE_MAX_REQUESTS:
        _RATE_STATE[ip] = history  # don't add this one
        return False
    history.append(now)
    _RATE_STATE[ip] = history
    # Periodic cleanup so the dict doesn't grow forever
    if len(_RATE_STATE) > 5000:
        for k in list(_RATE_STATE.keys()):
            if not _RATE_STATE[k] or _RATE_STATE[k][-1] < cutoff:
                del _RATE_STATE[k]
    return True

def _client_ip(environ):
    """Pull the client IP from CF/Render forwarding headers, falling back to
    REMOTE_ADDR. Render/Cloudflare always set one of these."""
    for key in ("HTTP_CF_CONNECTING_IP", "HTTP_X_FORWARDED_FOR", "HTTP_X_REAL_IP"):
        v = environ.get(key, "")
        if v:
            # XFF may be a comma-separated list; first entry is original client
            return v.split(",")[0].strip()
    return environ.get("REMOTE_ADDR", "unknown")


# Security headers applied to every response
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "X-XSS-Protection": "1; mode=block",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Content-Security-Policy": "default-src 'self' 'unsafe-inline' https://fdc.nal.usda.gov; img-src 'self' data:; font-src 'self'",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "Permissions-Policy": "camera=(self), microphone=(), geolocation=()",
}


class HTCAHHandler(SimpleHTTPRequestHandler):
    """Clean URL routing for the published site."""

    def do_GET(self):
        path = unquote(urlparse(self.path).path).rstrip("/") or "/"

        # --- Subdomain routing: bigreds.howtocookathome.com → tenant ---
        host = self.headers.get("Host", "")
        tenant_from_subdomain = _extract_tenant_from_host(host)
        if tenant_from_subdomain:
            self._serve_tenant(tenant_from_subdomain, path.lstrip("/"))
            return

        # --- Tenant sites: /t/<slug>/ (path-based fallback) ---
        if path.startswith("/t/"):
            parts = path[3:].split("/", 1)
            tenant_slug = parts[0]
            tenant_path = parts[1] if len(parts) > 1 else ""
            self._serve_tenant(tenant_slug, tenant_path)
            return

        # --- Unsubscribe: GET /unsubscribe/<token> ---
        if path.startswith("/unsubscribe/") and len(path) > len("/unsubscribe/"):
            token = path[len("/unsubscribe/"):]
            from app.db import DB
            db = DB()
            res = db.unsubscribe_by_token(token)
            db.close()
            body = _unsubscribe_html(res).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            for k, v in SECURITY_HEADERS.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)
            return

        # --- Live API: slug availability check ---
        if path == "/api/check-slug":
            from urllib.parse import parse_qs
            qs = parse_qs(urlparse(self.path).query)
            name = qs.get("name", [""])[0]
            city = qs.get("city", [""])[0]
            state = qs.get("state", [""])[0]
            if not name:
                self._json_error(400, "name parameter required")
                return
            try:
                from app.db import DB
                from app.onboard import slugify
                db = DB()
                suggestions = db.suggest_slug(name, city, state)
                response = json.dumps({"name": name, "suggestions": suggestions}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                for k, v in SECURITY_HEADERS.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(response)
            except Exception as e:
                self._json_error(500, str(e))
            return

        # --- Checkout routes (dev server) ---
        if path == "/api/checkout":
            from urllib.parse import parse_qs
            qs = parse_qs(urlparse(self.path).query)
            product = qs.get("product", [""])[0]
            email = qs.get("email", [""])[0] or None
            if product == "library":
                result = create_library_checkout(customer_email=email)
            elif product in ("sprout", "harvest"):
                result = create_tier_checkout(product, customer_email=email)
            else:
                self._json_error(400, "product must be library, sprout, or harvest")
                return
            if "error" in result:
                self._json_error(400, result["error"])
                return
            self.send_response(303)
            self.send_header("Location", result["url"])
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())
            return

        if path == "/api/checkout/status":
            result = get_checkout_status()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())
            return

        if path.startswith("/dl/"):
            token = path[4:]
            valid, info = verify_download_token(token)
            if not valid:
                self._json_error(403, info.get("error", "Invalid token"))
                return
            product = info.get("product", "library")
            library_file = os.path.join(BASE_DIR, "downloads", f"{product}.zip")
            if not os.path.isfile(library_file):
                library_file = os.path.join(BASE_DIR, "docs", f"{product}.zip")
            if not os.path.isfile(library_file):
                self._json_error(404, "Download file not found. Contact support.")
                return
            self._serve_file(library_file, "application/zip")
            return

        if path in ("/checkout/success", "/checkout/cancel"):
            msg = "Thank you! Check your email for the download link." if "success" in path else "Checkout cancelled. You weren't charged."
            body = f"<html><body style='font-family:Arial;max-width:600px;margin:80px auto;text-align:center'><h1>{msg}</h1><p><a href='/'>Back to HowToCookAtHome</a></p></body></html>".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)
            return

        # --- Platform pages (served from templates/, not pre-rendered) ---
        # These are interactive/dynamic pages that should NOT go through publish.py.
        # Content pages (recipes, episodes, tenant sites) are pre-rendered to output/.
        STATIC_PAGES = {
            "/scan": "scanner.html",
            "/scanner": "scanner.html",
            "/terms": "terms.html",
            "/guide": "guide.html",
            "/onboard": "onboard.html",
            "/about": "about.html",
        }
        if path in STATIC_PAGES:
            page_file = os.path.join(TEMPLATE_DIR, STATIC_PAGES[path])
            if os.path.isfile(page_file):
                self._serve_file(page_file, "text/html")
            else:
                self._serve_404()
            return

        # --- Downloadable files (kitchen terminal docx, etc.) ---
        if path == "/the-kitchen-terminal.docx":
            docx_path = os.path.join(BASE_DIR, "the-kitchen-terminal.docx")
            if os.path.isfile(docx_path):
                self._serve_file(docx_path, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
            else:
                self._serve_404()
            return

        # --- UPC Barcode Scan API ---
        if path.startswith("/api/scan/"):
            upc = path.split("/api/scan/")[1].strip()
            if not upc:
                self._json_error(400, "UPC required")
                return
            try:
                from app.branded import lookup_upc
                result = lookup_upc(upc)
                if result:
                    response = json.dumps(result, default=str).encode()
                    self.send_response(200)
                else:
                    response = json.dumps({"error": "not_found", "upc": upc}).encode()
                    self.send_response(404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                for k, v in SECURITY_HEADERS.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(response)
            except Exception as e:
                self._json_error(500, str(e))
            return

        # --- Branded food search API ---
        if path == "/api/search":
            from urllib.parse import parse_qs
            qs = parse_qs(urlparse(self.path).query)
            q = qs.get("q", [""])[0]
            cat = qs.get("category", [""])[0]
            if not q:
                self._json_error(400, "q parameter required")
                return
            try:
                from app.branded import search_branded
                results = search_branded(q, category=cat or None, limit=20)
                response = json.dumps({"query": q, "results": results}, default=str).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                for k, v in SECURITY_HEADERS.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(response)
            except Exception as e:
                self._json_error(500, str(e))
            return

        # --- API routes (static JSON files) ---
        if path.startswith("/api/"):
            endpoint = path.split("/api/")[1].split("?")[0]
            json_file = os.path.join(API_DIR, f"{endpoint}.json")
            if os.path.isfile(json_file):
                self._serve_file(json_file, "application/json")
            else:
                self._json_error(404, f"No API endpoint: {endpoint}")
            return

        # --- Clean URL routing for recipes ---
        if path.startswith("/r/"):
            slug = path[3:]
            target = os.path.join(WEB_DIR, f"r_{slug}.html")
            if os.path.isfile(target):
                self._serve_file(target, "text/html")
                return

        if path.startswith("/recipe/"):
            slug = path[8:]
            target = os.path.join(WEB_DIR, f"recipe_{slug}.html")
            if os.path.isfile(target):
                self._serve_file(target, "text/html")
                return

        # --- Static files: /static/style.css, /static/img/*, etc. ---
        if path.startswith("/static/"):
            static_file = os.path.join(STATIC_DIR, path[8:])
            if os.path.isfile(static_file):
                ctype, _ = mimetypes.guess_type(static_file)
                self._serve_file(static_file, ctype or "application/octet-stream")
                return

        # --- Named pages ---
        if path == "/":
            target = os.path.join(WEB_DIR, "index.html")
        elif path.endswith(".html"):
            target = os.path.join(WEB_DIR, os.path.basename(path))
        else:
            target = os.path.join(WEB_DIR, f"{os.path.basename(path)}.html")

        if os.path.isfile(target):
            self._serve_file(target, "text/html")
        else:
            self._serve_404()

    def do_POST(self):
        path = unquote(urlparse(self.path).path)

        if path == "/api/webhook":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            sig = self.headers.get("Stripe-Signature", "")
            if not verify_webhook_signature(body.decode(), sig):
                self._json_error(401, "Invalid signature")
                return
            try:
                event = json.loads(body)
                result = handle_webhook(event)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(result).encode())
            except Exception as e:
                self._json_error(500, str(e))
            return

        if path == "/api/onboard":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            try:
                data = json.loads(body)
                result = process_onboard(data)
                response = json.dumps(result).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                # NO Set-Cookie header — privacy first
                for k, v in SECURITY_HEADERS.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(response)
            except Exception as e:
                self._json_error(500, str(e))
            return

        if path == "/api/subscribe":
            content_length = int(self.headers.get("Content-Length", 0) or 0)
            body = self.rfile.read(content_length) if content_length else b"{}"
            try:
                ctype_in = self.headers.get("Content-Type", "")
                if "application/json" in ctype_in:
                    data = json.loads(body.decode("utf-8") or "{}")
                else:
                    from urllib.parse import parse_qs
                    parsed = parse_qs(body.decode("utf-8"))
                    data = {k: v[0] for k, v in parsed.items()}
                from app.db import DB
                db = DB()
                result = db.add_subscriber(
                    email=data.get("email", ""),
                    bucket=data.get("bucket", ""),
                    goal=data.get("goal", ""),
                    weekly_saving=data.get("weekly_saving", 0),
                    source=data.get("source", ""),
                )
                db.close()
                status = 200 if result.get("ok") else 400
                response = json.dumps(result).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                for k, v in SECURITY_HEADERS.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(response)
            except Exception as e:
                self._json_error(500, str(e))
            return

        self._json_error(404, "Not found")

    def _serve_tenant(self, tenant_slug, sub_path=""):
        """Serve a tenant page — works for both subdomain and /t/<slug>/ routing."""
        tenant_dir = os.path.join(BASE_DIR, "output", "tenants", tenant_slug)
        sub_path = sub_path.strip("/")

        # --- Cookbook PDF — generate on demand ---
        if sub_path == "cookbook.pdf":
            try:
                pdf_path = os.path.join(tenant_dir, f"{tenant_slug}-cookbook.pdf")
                build_cookbook(tenant_slug, output_path=pdf_path)
                self._serve_file(pdf_path, "application/pdf")
            except Exception as e:
                self._json_error(500, f"Cookbook generation failed: {e}")
            return

        if not sub_path:
            target = os.path.join(tenant_dir, "index.html")
        elif sub_path.startswith("r/"):
            recipe_slug = sub_path[2:]
            target = os.path.join(tenant_dir, f"r_{recipe_slug}.html")
        elif sub_path.startswith("api/"):
            # Tenant-scoped API: bigreds.howtocookathome.com/api/menu
            api_file = os.path.join(tenant_dir, "api", f"{sub_path[4:]}.json")
            if os.path.isfile(api_file):
                self._serve_file(api_file, "application/json")
            else:
                self._json_error(404, f"No tenant API: {sub_path}")
            return
        elif sub_path.endswith(".html"):
            target = os.path.join(tenant_dir, sub_path)
        else:
            target = os.path.join(tenant_dir, f"{sub_path}.html")

        if os.path.isfile(target):
            self._serve_file(target, "text/html")
        else:
            self._serve_404()

    def _serve_file(self, filepath, content_type):
        try:
            with open(filepath, "rb") as f:
                content = f.read()
            self.send_response(200)
            self.send_header("Content-Type", f"{content_type}; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            for k, v in SECURITY_HEADERS.items():
                self.send_header(k, v)
            # CORS for API
            if content_type == "application/json":
                self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(content)
        except Exception as e:
            self._json_error(500, str(e))

    def _serve_404(self):
        body = b"<h1>404</h1><p>Page not found.</p><p><a href='/'>Back to home</a></p>"
        self.send_response(404)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json_error(self, code, msg):
        body = json.dumps({"error": msg}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        print(f"  {args[0]}" if args else "")


# --- WSGI wrapper for Render/gunicorn ---
def app(environ, start_response):
    """Minimal WSGI app so gunicorn can serve this."""
    path = environ.get("PATH_INFO", "/").rstrip("/") or "/"
    host = environ.get("HTTP_HOST", "")
    headers = [(k, v) for k, v in SECURITY_HEADERS.items()]

    def serve(filepath, ctype):
        with open(filepath, "rb") as f:
            content = f.read()
        h = list(headers)
        h.append(("Content-Type", f"{ctype}; charset=utf-8"))
        h.append(("Content-Length", str(len(content))))
        if ctype == "application/json":
            h.append(("Access-Control-Allow-Origin", "*"))
        start_response("200 OK", h)
        return [content]

    def not_found():
        body = b"<h1>404</h1><p>Not found.</p>"
        h = list(headers)
        h.append(("Content-Type", "text/html; charset=utf-8"))
        start_response("404 Not Found", h)
        return [body]

    def serve_tenant_cookbook(slug):
        """Generate and serve a tenant cookbook PDF."""
        tenant_dir = os.path.join(BASE_DIR, "output", "tenants", slug)
        os.makedirs(tenant_dir, exist_ok=True)
        pdf_path = os.path.join(tenant_dir, f"{slug}-cookbook.pdf")
        try:
            build_cookbook(slug, output_path=pdf_path)
            with open(pdf_path, "rb") as f:
                content = f.read()
            h = list(headers)
            h.append(("Content-Type", "application/pdf"))
            h.append(("Content-Length", str(len(content))))
            h.append(("Content-Disposition", f'inline; filename="{slug}-cookbook.pdf"'))
            start_response("200 OK", h)
            return [content]
        except Exception as e:
            h = list(headers)
            h.append(("Content-Type", "application/json"))
            start_response("500 Internal Server Error", h)
            return [json.dumps({"error": f"Cookbook generation failed: {str(e)}"}).encode()]

    def resolve_tenant(slug, sub_path):
        """Resolve a file inside a tenant's output directory."""
        tenant_dir = os.path.join(BASE_DIR, "output", "tenants", slug)
        sub_path = sub_path.strip("/")
        if sub_path == "cookbook.pdf":
            return ("__cookbook__", slug)  # special marker
        if not sub_path:
            f = os.path.join(tenant_dir, "index.html")
        elif sub_path.startswith("r/"):
            f = os.path.join(tenant_dir, f"r_{sub_path[2:]}.html")
        elif sub_path.startswith("api/"):
            f = os.path.join(tenant_dir, "api", f"{sub_path[4:]}.json")
            return (f, "application/json") if os.path.isfile(f) else (None, None)
        elif sub_path.endswith(".html"):
            f = os.path.join(tenant_dir, sub_path)
        else:
            f = os.path.join(tenant_dir, f"{sub_path}.html")
        return (f, "text/html") if os.path.isfile(f) else (None, None)

    # --- CORS preflight (OPTIONS) for cross-origin POSTs from landing pages ---
    # The landing pages live on howtocookathome.com (Cloudflare Pages) and POST
    # to this API on howtocookathome.onrender.com. Browsers send an OPTIONS
    # preflight for POST with Content-Type: application/json.
    method = environ.get("REQUEST_METHOD", "GET")
    if method == "OPTIONS":
        h = list(headers)
        h.append(("Access-Control-Allow-Origin", "*"))
        h.append(("Access-Control-Allow-Methods", "GET, POST, OPTIONS"))
        h.append(("Access-Control-Allow-Headers", "Content-Type"))
        h.append(("Access-Control-Max-Age", "86400"))
        h.append(("Content-Length", "0"))
        start_response("204 No Content", h)
        return [b""]

    # --- Subdomain routing (production) ---
    tenant_from_subdomain = _extract_tenant_from_host(host)
    if tenant_from_subdomain:
        filepath, ctype = resolve_tenant(tenant_from_subdomain, path)
        if filepath == "__cookbook__":
            return serve_tenant_cookbook(ctype)  # ctype holds slug here
        return serve(filepath, ctype) if filepath else not_found()

    # --- Path-based tenant routing: /t/<slug>/ ---
    if path.startswith("/t/"):
        parts = path[3:].split("/", 1)
        slug = parts[0]
        sub_path = parts[1] if len(parts) > 1 else ""
        filepath, ctype = resolve_tenant(slug, sub_path)
        if filepath == "__cookbook__":
            return serve_tenant_cookbook(ctype)  # ctype holds slug here
        return serve(filepath, ctype) if filepath else not_found()

    # --- Live API: slug check (WSGI) ---
    query_string = environ.get("QUERY_STRING", "")
    if path == "/api/check-slug":
        from urllib.parse import parse_qs
        qs = parse_qs(query_string)
        name = qs.get("name", [""])[0]
        city = qs.get("city", [""])[0]
        state = qs.get("state", [""])[0]
        if not name:
            h = list(headers)
            h.append(("Content-Type", "application/json"))
            start_response("400 Bad Request", h)
            return [json.dumps({"error": "name parameter required"}).encode()]
        try:
            from app.db import DB
            db = DB()
            suggestions = db.suggest_slug(name, city, state)
            response = json.dumps({"name": name, "suggestions": suggestions}).encode()
            h = list(headers)
            h.append(("Content-Type", "application/json"))
            h.append(("Access-Control-Allow-Origin", "*"))
            start_response("200 OK", h)
            return [response]
        except Exception as e:
            h = list(headers)
            h.append(("Content-Type", "application/json"))
            start_response("500 Internal Server Error", h)
            return [json.dumps({"error": str(e)}).encode()]

    # --- Checkout routes ---
    query_string = query_string or environ.get("QUERY_STRING", "")

    # GET /api/checkout?product=library|sprout|harvest[&email=...]
    if method == "GET" and path == "/api/checkout":
        from urllib.parse import parse_qs
        qs = parse_qs(query_string)
        product = qs.get("product", [""])[0]
        email = qs.get("email", [""])[0] or None

        if product == "library":
            result = create_library_checkout(customer_email=email)
        elif product in ("sprout", "harvest"):
            result = create_tier_checkout(product, customer_email=email)
        else:
            h = list(headers)
            h.append(("Content-Type", "application/json"))
            start_response("400 Bad Request", h)
            return [json.dumps({"error": "product must be library, sprout, or harvest"}).encode()]

        if "error" in result:
            h = list(headers)
            h.append(("Content-Type", "application/json"))
            start_response("400 Bad Request", h)
            return [json.dumps(result).encode()]

        # Redirect to Stripe Checkout
        h = list(headers)
        h.append(("Location", result["url"]))
        h.append(("Content-Type", "application/json"))
        start_response("303 See Other", h)
        return [json.dumps(result).encode()]

    # GET /api/checkout/status — system health
    if method == "GET" and path == "/api/checkout/status":
        result = get_checkout_status()
        h = list(headers)
        h.append(("Content-Type", "application/json"))
        start_response("200 OK", h)
        return [json.dumps(result).encode()]

    # POST /api/webhook — Stripe webhook receiver
    if method == "POST" and path == "/api/webhook":
        try:
            content_length = int(environ.get("CONTENT_LENGTH", 0))
            body = environ["wsgi.input"].read(content_length)
            sig = environ.get("HTTP_STRIPE_SIGNATURE", "")

            # Verify signature
            if not verify_webhook_signature(body.decode(), sig):
                h = list(headers)
                h.append(("Content-Type", "application/json"))
                start_response("401 Unauthorized", h)
                return [json.dumps({"error": "Invalid signature"}).encode()]

            event = json.loads(body)
            result = handle_webhook(event)
            h = list(headers)
            h.append(("Content-Type", "application/json"))
            start_response("200 OK", h)
            return [json.dumps(result).encode()]
        except Exception as e:
            h = list(headers)
            h.append(("Content-Type", "application/json"))
            start_response("500 Internal Server Error", h)
            return [json.dumps({"error": str(e)}).encode()]

    # GET /dl/<token> — Token-based download
    if method == "GET" and path.startswith("/dl/"):
        token = path[4:]
        valid, info = verify_download_token(token)
        if not valid:
            h = list(headers)
            h.append(("Content-Type", "application/json"))
            start_response("403 Forbidden", h)
            return [json.dumps(info).encode()]

        # Serve the library ZIP/PDF based on product
        product = info.get("product", "library")
        # Look for library files in docs/ or a dedicated downloads/ dir
        library_dir = os.path.join(BASE_DIR, "downloads")
        os.makedirs(library_dir, exist_ok=True)
        library_file = os.path.join(library_dir, f"{product}.zip")
        if not os.path.isfile(library_file):
            # Fallback: check docs/
            library_file = os.path.join(BASE_DIR, "docs", f"{product}.zip")
        if not os.path.isfile(library_file):
            h = list(headers)
            h.append(("Content-Type", "application/json"))
            start_response("404 Not Found", h)
            return [json.dumps({"error": f"Download file not found. Contact support.", "uses_remaining": info.get("uses_remaining")}).encode()]

        with open(library_file, "rb") as f:
            content = f.read()
        h = list(headers)
        h.append(("Content-Type", "application/zip"))
        h.append(("Content-Length", str(len(content))))
        h.append(("Content-Disposition", f'attachment; filename="howtocookathome-{product}.zip"'))
        start_response("200 OK", h)
        return [content]

    # GET /checkout/success — Thank you page
    if method == "GET" and path == "/checkout/success":
        body = b"""<!DOCTYPE html>
<html><head><title>Thank You | HowToCookAtHome</title>
<style>body{font-family:Arial,sans-serif;max-width:600px;margin:80px auto;text-align:center;color:#333}
h1{color:#2D6A4F}a{color:#C4975A}</style></head>
<body><h1>Thank you!</h1>
<p>Your purchase is confirmed. Check your email for the download link.</p>
<p>If you don't see it within a few minutes, check your spam folder.</p>
<p><a href="/">Back to HowToCookAtHome</a></p>
</body></html>"""
        h = list(headers)
        h.append(("Content-Type", "text/html; charset=utf-8"))
        start_response("200 OK", h)
        return [body]

    # GET /checkout/cancel — Cancelled page
    if method == "GET" and path == "/checkout/cancel":
        body = b"""<!DOCTYPE html>
<html><head><title>Checkout Cancelled | HowToCookAtHome</title>
<style>body{font-family:Arial,sans-serif;max-width:600px;margin:80px auto;text-align:center;color:#333}
a{color:#C4975A}</style></head>
<body><h1>Checkout cancelled</h1>
<p>No worries. You weren't charged.</p>
<p><a href="/">Back to HowToCookAtHome</a></p>
</body></html>"""
        h = list(headers)
        h.append(("Content-Type", "text/html; charset=utf-8"))
        start_response("200 OK", h)
        return [body]

    # --- GET /unsubscribe/<token> ---
    if method == "GET" and path.startswith("/unsubscribe/") and len(path) > len("/unsubscribe/"):
        token = path[len("/unsubscribe/"):]
        from app.db import DB
        db = DB()
        res = db.unsubscribe_by_token(token)
        db.close()
        body = _unsubscribe_html(res).encode("utf-8")
        h = list(headers)
        h.append(("Content-Type", "text/html; charset=utf-8"))
        start_response("200 OK", h)
        return [body]

    # --- GET /claim/<slug> — claim form for a tenant page ---
    # Shown on unclaimed pages. Renders a small form that POSTs /api/claim/<slug>.
    if method == "GET" and path.startswith("/claim/") and len(path) > len("/claim/") and "/" not in path[len("/claim/"):]:
        slug = path[len("/claim/"):]
        from app.db import DB
        db = DB()
        tenant_row = db.conn.execute("SELECT slug, name, claimed FROM tenants WHERE slug=?", (slug,)).fetchone()
        # Lazy-sync: if the tenant YAML exists in the repo but the row isn't in
        # the DB yet (common on fresh deploys — publish.py builds HTML but
        # doesn't populate the tenants table), upsert from YAML on demand.
        if not tenant_row:
            yaml_path = os.path.join(BASE_DIR, "content", "tenants", f"{slug}.yaml")
            if os.path.isfile(yaml_path):
                try:
                    import yaml as _yaml
                    data = _yaml.safe_load(open(yaml_path)) or {}
                    if data.get("slug"):
                        db.upsert_tenant(data)
                        tenant_row = db.conn.execute(
                            "SELECT slug, name, claimed FROM tenants WHERE slug=?", (slug,)
                        ).fetchone()
                except Exception:
                    pass
        db.close()
        if not tenant_row:
            return not_found()
        if tenant_row["claimed"]:
            body = _claimed_already_html(tenant_row["name"]).encode("utf-8")
        else:
            body = _claim_form_html(tenant_row["slug"], tenant_row["name"]).encode("utf-8")
        h = list(headers)
        h.append(("Content-Type", "text/html; charset=utf-8"))
        start_response("200 OK", h)
        return [body]

    # --- POST /api/claim/<slug> — email a magic link to the owner ---
    if method == "POST" and path.startswith("/api/claim/") and len(path) > len("/api/claim/"):
        slug = path[len("/api/claim/"):]
        # rate limit — same bucket as subscribe; a hostile actor can't spray claim requests
        ip = _client_ip(environ)
        if not _rate_limit_check(ip):
            h = list(headers)
            h.append(("Content-Type", "application/json"))
            h.append(("Retry-After", str(_RATE_WINDOW_SEC)))
            start_response("429 Too Many Requests", h)
            return [json.dumps({"ok": False, "error": "slow down"}).encode()]
        try:
            content_length = int(environ.get("CONTENT_LENGTH", 0) or 0)
            raw = environ["wsgi.input"].read(content_length) if content_length else b"{}"
            ctype_in = environ.get("CONTENT_TYPE", "")
            if "application/json" in ctype_in:
                data = json.loads(raw.decode("utf-8") or "{}")
            else:
                from urllib.parse import parse_qs
                parsed = parse_qs(raw.decode("utf-8"))
                data = {k: v[0] for k, v in parsed.items()}
            email = (data.get("email") or "").strip().lower()

            from app.db import DB
            db = DB()
            # Lazy-sync from YAML if the tenant row isn't in DB yet
            row = db.conn.execute("SELECT name FROM tenants WHERE slug=?", (slug,)).fetchone()
            if not row:
                yaml_path = os.path.join(BASE_DIR, "content", "tenants", f"{slug}.yaml")
                if os.path.isfile(yaml_path):
                    try:
                        import yaml as _yaml
                        data = _yaml.safe_load(open(yaml_path)) or {}
                        if data.get("slug"):
                            db.upsert_tenant(data)
                    except Exception:
                        pass
            result = db.generate_claim_token(slug=slug, email=email)
            row = db.conn.execute("SELECT name FROM tenants WHERE slug=?", (slug,)).fetchone()
            tenant_name = row["name"] if row else slug
            db.close()

            # Send the magic-link email even if generate_claim_token failed, to avoid
            # leaking which slugs are claimable — but only if basic email validation
            # passed. Otherwise return 400.
            if not result.get("ok") and result.get("error") == "invalid email":
                h = list(headers)
                h.append(("Content-Type", "application/json"))
                start_response("400 Bad Request", h)
                return [json.dumps(result).encode()]

            if result.get("ok"):
                try:
                    from app import mail as _mail
                    verify_url = f"https://howtocookathome.com/claim/verify/{slug}/{result['token']}"
                    subj = f"Claim {tenant_name} on HowToCookAtHome"
                    text = (
                        f"Someone (hopefully you) asked to claim the page for {tenant_name} "
                        f"on HowToCookAtHome.\n\n"
                        f"Click this link to confirm you're the owner:\n{verify_url}\n\n"
                        f"If this wasn't you, ignore this email — nothing happens.\n\n"
                        f"— HowToCookAtHome\n"
                    )
                    html = (
                        f"<p>Someone (hopefully you) asked to claim the page for "
                        f"<strong>{tenant_name}</strong> on HowToCookAtHome.</p>"
                        f'<p><a href="{verify_url}" style="background:#D91E18;color:#fff;padding:0.9rem 1.5rem;'
                        f'text-decoration:none;font-weight:900;text-transform:uppercase;letter-spacing:0.06em;">'
                        f"Confirm ownership</a></p>"
                        f"<p style=\"color:#8A8A8A;font-size:0.85rem\">Or copy this link: {verify_url}</p>"
                        f"<p style=\"color:#8A8A8A;font-size:0.85rem\">If this wasn't you, ignore this email.</p>"
                    )
                    _mail.send(to=email, subject=subj, html=html, text=text)
                except Exception as _e:
                    # Don't fail the request if mail blows up; tell the caller a
                    # generic ok so they don't see internals (and we still have
                    # the token in the DB for re-send if needed).
                    pass

            # Always return a generic success to the client — don't leak whether
            # the tenant existed, was already claimed, or the email bounced.
            h = list(headers)
            h.append(("Content-Type", "application/json"))
            h.append(("Access-Control-Allow-Origin", "*"))
            start_response("200 OK", h)
            return [json.dumps({"ok": True, "message": "If the email matches, a verification link is on its way."}).encode()]
        except Exception as e:
            h = list(headers)
            h.append(("Content-Type", "application/json"))
            start_response("500 Internal Server Error", h)
            return [json.dumps({"ok": False, "error": str(e)}).encode()]

    # --- GET /claim/verify/<slug>/<token> — owner clicks the email link ---
    if method == "GET" and path.startswith("/claim/verify/"):
        parts = path[len("/claim/verify/"):].split("/", 1)
        if len(parts) != 2:
            return not_found()
        slug, token = parts
        from app.db import DB
        db = DB()
        result = db.verify_claim_token(slug=slug, token=token)
        db.close()
        body = _claim_verify_html(slug, result).encode("utf-8")
        h = list(headers)
        h.append(("Content-Type", "text/html; charset=utf-8"))
        start_response("200 OK" if result.get("ok") else "400 Bad Request", h)
        return [body]

    # --- DELETE /api/subscribers/<token> — GDPR right-to-deletion ---
    # Unsubscribe marks the row; this fully removes it. Subscriber clicks
    # a link in their own email to trigger this.
    if method in ("DELETE", "POST") and path.startswith("/api/subscribers/") and len(path) > len("/api/subscribers/"):
        token = path[len("/api/subscribers/"):]
        from app.db import DB
        db = DB()
        res = db.delete_subscriber_by_token(token)
        db.close()
        h = list(headers)
        h.append(("Content-Type", "application/json"))
        h.append(("Access-Control-Allow-Origin", "*"))
        start_response("200 OK" if res.get("ok") else "400 Bad Request", h)
        return [json.dumps(res).encode()]

    # --- POST /api/subscribe ---
    # Captures email signups from /tip-better and /next-show landing pages.
    # Body: {"email": "...", "bucket": "patron"|"experience", "goal": "...", "weekly_saving": 47}
    if method == "POST" and path == "/api/subscribe":
        # --- Rate limit first, before even parsing the body ---
        ip = _client_ip(environ)
        if not _rate_limit_check(ip):
            h = list(headers)
            h.append(("Content-Type", "application/json"))
            h.append(("Access-Control-Allow-Origin", "*"))
            h.append(("Retry-After", str(_RATE_WINDOW_SEC)))
            start_response("429 Too Many Requests", h)
            return [json.dumps({"ok": False, "error": "slow down — try again in a minute"}).encode()]
        try:
            content_length = int(environ.get("CONTENT_LENGTH", 0) or 0)
            raw = environ["wsgi.input"].read(content_length) if content_length else b"{}"
            ctype_in = environ.get("CONTENT_TYPE", "")
            if "application/json" in ctype_in:
                data = json.loads(raw.decode("utf-8") or "{}")
            else:
                from urllib.parse import parse_qs
                parsed = parse_qs(raw.decode("utf-8"))
                data = {k: v[0] for k, v in parsed.items()}
            from app.db import DB
            db = DB()
            result = db.add_subscriber(
                email=data.get("email", ""),
                bucket=data.get("bucket", ""),
                goal=data.get("goal", ""),
                weekly_saving=data.get("weekly_saving", 0),
                source=data.get("source", ""),
            )
            db.close()
            h = list(headers)
            h.append(("Content-Type", "application/json"))
            h.append(("Access-Control-Allow-Origin", "*"))
            status = "200 OK" if result.get("ok") else "400 Bad Request"
            start_response(status, h)
            return [json.dumps(result).encode()]
        except Exception as e:
            h = list(headers)
            h.append(("Content-Type", "application/json"))
            start_response("500 Internal Server Error", h)
            return [json.dumps({"ok": False, "error": str(e)}).encode()]

    # --- POST /api/onboard ---
    if method == "POST" and path == "/api/onboard":
        try:
            content_length = int(environ.get("CONTENT_LENGTH", 0))
            body = environ["wsgi.input"].read(content_length)
            data = json.loads(body)
            result = process_onboard(data)
            response = json.dumps(result).encode()
            h = list(headers)
            h.append(("Content-Type", "application/json"))
            start_response("200 OK", h)
            return [response]
        except Exception as e:
            h = list(headers)
            h.append(("Content-Type", "application/json"))
            start_response("500 Internal Server Error", h)
            return [json.dumps({"error": str(e)}).encode()]

    # --- Static files ---
    if path.startswith("/static/"):
        static_file = os.path.join(STATIC_DIR, path[8:])
        if os.path.isfile(static_file):
            ctype, _ = mimetypes.guess_type(static_file)
            return serve(static_file, ctype or "application/octet-stream")

    # --- Platform pages (served from templates/, not pre-rendered) ---
    WSGI_STATIC_PAGES = {
        "/scan": "scanner.html",
        "/scanner": "scanner.html",
        "/terms": "terms.html",
        "/guide": "guide.html",
        "/onboard": "onboard.html",
        "/about": "about.html",
    }
    if path in WSGI_STATIC_PAGES:
        page_file = os.path.join(TEMPLATE_DIR, WSGI_STATIC_PAGES[path])
        if os.path.isfile(page_file):
            return serve(page_file, "text/html")
        return not_found()

    # --- Downloadable files ---
    if path == "/the-kitchen-terminal.docx":
        docx_path = os.path.join(BASE_DIR, "the-kitchen-terminal.docx")
        if os.path.isfile(docx_path):
            return serve(docx_path, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        return not_found()

    # /api/debug-paths removed — it exposed filesystem layout to any caller.
    # Was useful during the deploy debug session (commit 27d9e29). Now gone.

    # --- Platform routes ---
    def resolve_platform(path):
        if path.startswith("/api/"):
            endpoint = path.split("/api/")[1].split("?")[0]
            f = os.path.join(API_DIR, f"{endpoint}.json")
            return (f, "application/json") if os.path.isfile(f) else (None, None)
        if path.startswith("/r/"):
            f = _find_web_file(f"r_{path[3:]}.html")
            return (f, "text/html") if f else (None, None)
        if path.startswith("/recipe/"):
            f = _find_web_file(f"recipe_{path[8:]}.html")
            return (f, "text/html") if f else (None, None)
        if path == "/":
            f = _find_web_file("index.html")
        elif path.endswith(".html"):
            f = _find_web_file(os.path.basename(path))
        else:
            f = _find_web_file(f"{os.path.basename(path)}.html")
        return (f, "text/html") if f else (None, None)

    filepath, ctype = resolve_platform(path)
    return serve(filepath, ctype) if filepath else not_found()


def _load_identity():
    """Load identity.yaml — the mise en place for the whole platform."""
    import yaml
    identity_path = os.path.join(BASE_DIR, "content", "identity.yaml")
    if os.path.isfile(identity_path):
        with open(identity_path) as f:
            return yaml.safe_load(f)
    return {}


def _cli_version(identity):
    v = identity.get("version", "unknown")
    codename = identity.get("codename", "")
    dba = identity.get("entity", {}).get("dba", "HowToCookAtHome")
    owner = identity.get("entity", {}).get("owner", "")
    entity = identity.get("entity", {}).get("name", "")
    print(f"\n  {dba} v{v} ({codename})")
    print(f"  {owner} — {entity}")
    print(f"  Type --help for commands, --license for terms, --credits for attribution.\n")


def _cli_license(identity):
    lics = identity.get("licenses", {})
    print(f"\n  Licenses")
    print(f"  ========")
    for key in ("code", "content", "service", "data"):
        lic = lics.get(key, {})
        if not lic:
            continue
        print(f"  {key.title():10s} {lic.get('type', 'unknown'):25s} ({lic.get('spdx', '')})")
        applies = lic.get("applies_to", "")
        if applies:
            print(f"             Applies to: {applies}")
        summary = lic.get("summary", "").strip().split("\n")[0]
        print(f"             {summary}")
    print()


def _cli_credits(identity):
    print(f"\n  Credits")
    print(f"  =======")
    founder = identity.get("credits", {}).get("founder", {})
    if founder:
        print(f"  Founder:  {founder.get('name', '')} ({founder.get('role', '')})")
    for collab in identity.get("credits", {}).get("collaborators", []):
        status = f" [{collab.get('status', '')}]" if collab.get("status") else ""
        print(f"  Collab:   {collab.get('name', '')}{status}")
    for ai in identity.get("credits", {}).get("ai_tools", []):
        print(f"  AI:       {ai.get('name', '')} — {ai.get('role', '')}")
    for ds in identity.get("credits", {}).get("data_sources", []):
        print(f"  Data:     {ds.get('name', '')} ({ds.get('license', '')})")
    print()


def _cli_help(identity):
    dba = identity.get("entity", {}).get("dba", "HowToCookAtHome")
    v = identity.get("version", "?")
    print(f"\n  {dba} v{v}")
    print(f"  {'='*40}")
    print(f"  Usage: python3 -m app.<module> [options]\n")
    commands = identity.get("cli", {}).get("commands", {})
    for name, cmd in commands.items():
        desc = cmd.get("description", "")
        usage = cmd.get("usage", "")
        print(f"  {name:12s} {desc}")
        if usage:
            print(f"  {'':12s} $ {usage}")
    contact = identity.get("contact", {})
    print(f"\n  Website:  {contact.get('website', '')}")
    print(f"  GitHub:   {contact.get('github', '')}")
    print(f"  Support:  {contact.get('support', '')}")
    open_qs = identity.get("open_questions", [])
    if open_qs:
        print(f"\n  Open questions ({len(open_qs)}):")
        for q in open_qs:
            print(f"    ? {q.get('question', '')}  [{q.get('status', '')}]")
    print()


if __name__ == "__main__":
    identity = _load_identity()

    # --- CLI decision tree (mise en place) ---
    if "--version" in sys.argv or "-V" in sys.argv:
        _cli_version(identity)
        exit(0)
    if "--license" in sys.argv:
        _cli_license(identity)
        exit(0)
    if "--credits" in sys.argv:
        _cli_credits(identity)
        exit(0)
    if "--help" in sys.argv or "-h" in sys.argv:
        _cli_help(identity)
        exit(0)
    if "--identity" in sys.argv:
        # Dump the full mise en place
        _cli_version(identity)
        _cli_license(identity)
        _cli_credits(identity)
        _cli_help(identity)
        exit(0)

    if not os.path.isdir(WEB_DIR):
        print(f"\n  ERROR: output/web/ not found. Run publish.py first.\n")
        exit(1)

    # Print the banner (like python3 interactive mode)
    _cli_version(identity)

    api_health = os.path.join(API_DIR, "health.json")
    if os.path.isfile(api_health):
        h = json.load(open(api_health))
        usda_foods = h.get('usda', {}).get('foods', 0)
        usda_str = f"{usda_foods:,}" if isinstance(usda_foods, (int, float)) else str(usda_foods)
        print(f"  Recipes:    {h.get('recipes', 0)} (USDA-enriched)")
        print(f"  USDA:       {usda_str} foods")
        print(f"  Tenants:    {h.get('tenants', 0)} total")
        print(f"  Demo sites: {h.get('demo_sites', 0)}")
        print(f"  Live sites: {h.get('live_sites', 0)}")

    pages = len([f for f in os.listdir(WEB_DIR) if f.endswith(".html")])
    print(f"\n  Serving {pages} pages on http://localhost:{PORT}")
    print(f"  API:     http://localhost:{PORT}/api/health")
    print(f"  Ctrl+C to stop.\n")

    server = HTTPServer(("0.0.0.0", PORT), HTCAHHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.\n")
        server.server_close()
