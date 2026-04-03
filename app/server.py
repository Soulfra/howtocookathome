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
WEB_DIR = os.path.join(BASE_DIR, "output", "web")
API_DIR = os.path.join(BASE_DIR, "output", "api")
TEMPLATE_DIR = os.path.join(BASE_DIR, "templates")
STATIC_DIR = os.path.join(BASE_DIR, "static")
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
        else:
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

    # --- POST /api/onboard ---
    method = environ.get("REQUEST_METHOD", "GET")
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

    # --- Platform routes ---
    def resolve_platform(path):
        if path.startswith("/api/"):
            endpoint = path.split("/api/")[1].split("?")[0]
            f = os.path.join(API_DIR, f"{endpoint}.json")
            return (f, "application/json") if os.path.isfile(f) else (None, None)
        if path.startswith("/r/"):
            f = os.path.join(WEB_DIR, f"r_{path[3:]}.html")
            return (f, "text/html") if os.path.isfile(f) else (None, None)
        if path.startswith("/recipe/"):
            f = os.path.join(WEB_DIR, f"recipe_{path[8:]}.html")
            return (f, "text/html") if os.path.isfile(f) else (None, None)
        if path == "/":
            f = os.path.join(WEB_DIR, "index.html")
        elif path.endswith(".html"):
            f = os.path.join(WEB_DIR, os.path.basename(path))
        else:
            f = os.path.join(WEB_DIR, f"{os.path.basename(path)}.html")
        return (f, "text/html") if os.path.isfile(f) else (None, None)

    filepath, ctype = resolve_platform(path)
    return serve(filepath, ctype) if filepath else not_found()


if __name__ == "__main__":
    if not os.path.isdir(WEB_DIR):
        print(f"\n  ERROR: output/web/ not found. Run publish.py first.\n")
        exit(1)

    api_health = os.path.join(API_DIR, "health.json")
    if os.path.isfile(api_health):
        h = json.load(open(api_health))
        print(f"\n  HTCAH Server")
        print(f"  ============")
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
