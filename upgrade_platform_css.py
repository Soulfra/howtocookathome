#!/usr/bin/env python3
"""
One-time script: upgrade all platform pages in output/web/ to the new design system.
Injects Google Fonts, design tokens, and improved typography into existing HTML files.
Does NOT touch tenant pages (those already have the new styling).
"""
import os, glob, re

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output", "web")

GOOGLE_FONTS = '<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin><link href="https://fonts.googleapis.com/css2?family=DM+Sans:ital,opsz,wght@0,9..40,300;0,9..40,400;0,9..40,500;0,9..40,600;0,9..40,700;1,9..40,400&family=Lora:ital,wght@0,400;0,500;0,600;0,700;1,400&display=swap" rel="stylesheet">'

# CSS overrides that layer on top of existing styles
DESIGN_UPGRADES = """
<style>
/* === Design system upgrade === */
:root {
  --color-primary: #1A1A2E;
  --color-accent: #C4975A;
  --color-bg: #FFFBF5;
  --color-surface: #FFFFFF;
  --color-text: #1C1917;
  --color-text-light: #57534E;
  --color-success: #2D6A4F;
  --font-heading: 'Lora', Georgia, 'Times New Roman', serif;
  --font-body: 'DM Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  --radius: 16px;
  --shadow-sm: 0 1px 2px rgba(0,0,0,0.04), 0 1px 4px rgba(0,0,0,0.03);
  --shadow-md: 0 4px 12px rgba(0,0,0,0.06), 0 1px 3px rgba(0,0,0,0.04);
  --shadow-lg: 0 8px 24px rgba(0,0,0,0.08), 0 2px 6px rgba(0,0,0,0.04);
  --transition: 0.2s cubic-bezier(0.4, 0, 0.2, 1);
}
body {
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
  font-size: 15px;
  line-height: 1.7;
  background: var(--color-bg);
}
h1,h2,h3,h4 { letter-spacing: -0.02em; line-height: 1.25; }
a { transition: color var(--transition); }
::selection { background: var(--color-accent); color: white; }

/* Upgraded nav */
nav {
  position: sticky;
  top: 0;
  z-index: 100;
  box-shadow: 0 1px 0 rgba(255,255,255,0.06);
  backdrop-filter: blur(12px);
}
nav .links a {
  transition: all var(--transition);
  padding-bottom: 2px;
  border-bottom: 2px solid transparent;
}
nav .links a:hover {
  opacity: 1;
  color: var(--color-accent);
  border-bottom-color: var(--color-accent);
}

/* Upgraded cards */
.episode-card {
  border-radius: var(--radius);
  border: 1px solid rgba(0,0,0,0.04);
  box-shadow: var(--shadow-sm);
  transition: transform var(--transition), box-shadow var(--transition);
}
.episode-card:hover {
  transform: translateY(-3px);
  box-shadow: var(--shadow-lg);
  border-color: rgba(0,0,0,0.04);
}

/* Upgraded hero */
.hero { padding: 5rem 2rem 4rem; }
.hero h1 { font-size: 2.6rem; letter-spacing: -0.03em; font-weight: 700; }
.hero .cta {
  border-radius: 8px;
  transition: opacity var(--transition);
}
.hero .cta:hover { opacity: 0.9; }

/* Upgraded footer */
footer {
  padding: 2.5rem 2rem;
  font-size: 0.82rem;
  letter-spacing: 0.01em;
}

/* Recipe page upgrades */
.recipe-header { padding: 4rem 2rem; }
.recipe-header h1 { font-size: 2.2rem; letter-spacing: -0.03em; }
.meta-item {
  border-radius: var(--radius);
  border: 1px solid rgba(0,0,0,0.06);
  box-shadow: var(--shadow-sm);
}

/* Subscribe block */
.subscribe { padding: 4rem 2rem; }
.subscribe button { border-radius: 6px; transition: opacity var(--transition); }
.subscribe button:hover { opacity: 0.9; }
.subscribe input[type="email"] { border-radius: 6px 0 0 6px; }

/* Responsive */
@media (max-width: 600px) {
  .hero h1 { font-size: 1.8rem; }
  .hero { padding: 3rem 1.5rem 2.5rem; }
  nav { padding: 0.75rem 1rem; }
  nav .links { gap: 0.8rem; }
  nav .links a { font-size: 0.75rem; }
}
</style>
"""

def upgrade_file(filepath):
    with open(filepath) as f:
        html = f.read()

    # Skip if already upgraded
    if "Design system upgrade" in html:
        return False

    # Skip tenant pages (restaurants.html is platform-level, keep it)
    basename = os.path.basename(filepath)

    # 1. Inject Google Fonts link before </head>
    if "fonts.googleapis.com" not in html:
        html = html.replace("</head>", f"{GOOGLE_FONTS}\n</head>")

    # 2. Inject design token overrides before </head>
    html = html.replace("</head>", f"{DESIGN_UPGRADES}\n</head>")

    with open(filepath, "w") as f:
        f.write(html)
    return True


if __name__ == "__main__":
    files = glob.glob(os.path.join(WEB_DIR, "*.html"))
    upgraded = 0
    for f in sorted(files):
        # Skip restaurants.html — it already has the new design system
        if os.path.basename(f) == "restaurants.html":
            continue
        if upgrade_file(f):
            print(f"  Upgraded: {os.path.basename(f)}")
            upgraded += 1
        else:
            print(f"  Skipped:  {os.path.basename(f)} (already upgraded)")
    print(f"\n  Done. {upgraded} files upgraded.")
