#!/usr/bin/env python3
"""
COOKBOOK_GEN.PY — Generate a real cookbook PDF from tenant data.

Design system: consistent typography hierarchy, uniform spacing grid,
page numbers, branded headers/footers, professional table styling.
Every recipe gets a facing notes page. 50/50 — half content, half yours.
"""
import sqlite3, json, os, sys, textwrap
from datetime import datetime
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.lib.colors import HexColor, black, white, Color
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, PageBreak, Table, TableStyle,
    KeepTogether, HRFlowable, Frame, PageTemplate, BaseDocTemplate
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.pdfgen import canvas as pdfcanvas

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "data", "htcah.db")  # app data
USDA_PATH = os.path.join(BASE_DIR, "data", "usda", "foundation.db")  # USDA reference

# =========================================================
# DESIGN SYSTEM — one source of truth for the entire book
# =========================================================
# Palette
INK = HexColor("#1A1A2E")          # primary text, headings
GOLD = HexColor("#C4975A")         # accent, rules, numbering
CREAM = HexColor("#FAF8F4")        # page tint / alternating rows
SAGE = HexColor("#2D6A4F")         # success / cost callouts
SLATE = HexColor("#555555")        # body text
MUTED = HexColor("#999999")        # captions, footnotes
RULE = HexColor("#D8D4CC")         # horizontal rules, table borders
GHOST = HexColor("#E8E4DC")        # notes lines, faint UI
PAGE_BG = HexColor("#FFFFFF")      # page background

# Spacing grid (pts)
GRID = 6  # base unit — everything snaps to multiples of 6pt

# Page geometry
PAGE_W, PAGE_H = letter
MARGIN_TOP = 0.9 * inch
MARGIN_BOT = 0.8 * inch
MARGIN_L = 1.0 * inch
MARGIN_R = 1.0 * inch
CONTENT_W = PAGE_W - MARGIN_L - MARGIN_R

# Typography scale
FONT_SERIF = "Times-Roman"
FONT_SERIF_BOLD = "Times-Bold"
FONT_SERIF_ITALIC = "Times-Italic"
FONT_SANS = "Helvetica"
FONT_SANS_BOLD = "Helvetica-Bold"
FONT_SANS_OBLIQUE = "Helvetica-Oblique"


def _build_styles():
    """Build the complete paragraph style sheet."""
    base = getSampleStyleSheet()
    S = {}

    # Cover
    S["cover_title"] = ParagraphStyle(
        "cover_title", parent=base["Title"],
        fontName=FONT_SERIF_BOLD, fontSize=38, leading=44,
        textColor=INK, alignment=TA_CENTER, spaceAfter=GRID*2,
    )
    S["cover_sub"] = ParagraphStyle(
        "cover_sub", parent=base["Normal"],
        fontName=FONT_SERIF_ITALIC, fontSize=13, leading=18,
        textColor=GOLD, alignment=TA_CENTER, spaceAfter=GRID,
    )
    S["cover_detail"] = ParagraphStyle(
        "cover_detail", parent=base["Normal"],
        fontName=FONT_SANS, fontSize=9, leading=13,
        textColor=MUTED, alignment=TA_CENTER, spaceAfter=GRID,
    )

    # Headings
    S["h1"] = ParagraphStyle(
        "h1", parent=base["Heading1"],
        fontName=FONT_SERIF_BOLD, fontSize=22, leading=26,
        textColor=INK, spaceBefore=0, spaceAfter=GRID*2,
    )
    S["h2"] = ParagraphStyle(
        "h2", parent=base["Heading2"],
        fontName=FONT_SANS_BOLD, fontSize=9, leading=12,
        textColor=GOLD, spaceBefore=GRID*3, spaceAfter=GRID,
        tracking=1.2,  # letter-spacing effect via tracking
    )

    # Body
    S["body"] = ParagraphStyle(
        "body", parent=base["Normal"],
        fontName=FONT_SANS, fontSize=9.5, leading=14,
        textColor=SLATE, spaceAfter=GRID,
    )
    S["body_small"] = ParagraphStyle(
        "body_small", parent=base["Normal"],
        fontName=FONT_SANS, fontSize=8, leading=11,
        textColor=MUTED, spaceAfter=2,
    )
    S["body_bold"] = ParagraphStyle(
        "body_bold", parent=base["Normal"],
        fontName=FONT_SANS_BOLD, fontSize=9.5, leading=14,
        textColor=SLATE, spaceAfter=GRID,
    )

    # Step numbering
    S["step_num"] = ParagraphStyle(
        "step_num", parent=base["Normal"],
        fontName=FONT_SANS_BOLD, fontSize=9.5, leading=14,
        textColor=GOLD,
    )

    # Cost callout
    S["callout"] = ParagraphStyle(
        "callout", parent=base["Normal"],
        fontName=FONT_SANS_BOLD, fontSize=10, leading=14,
        textColor=SAGE, alignment=TA_CENTER,
        spaceBefore=GRID*2, spaceAfter=GRID*2,
    )

    # Table headers (not directly used as ParagraphStyle on tables,
    # but useful for header cell content)
    S["th"] = ParagraphStyle(
        "th", parent=base["Normal"],
        fontName=FONT_SANS_BOLD, fontSize=7.5, leading=10,
        textColor=white,
    )
    S["td"] = ParagraphStyle(
        "td", parent=base["Normal"],
        fontName=FONT_SANS, fontSize=8, leading=11,
        textColor=SLATE,
    )
    S["td_accent"] = ParagraphStyle(
        "td_accent", parent=base["Normal"],
        fontName=FONT_SANS_BOLD, fontSize=8, leading=11,
        textColor=INK,
    )

    # Notes pages
    S["notes_head"] = ParagraphStyle(
        "notes_head", parent=base["Heading1"],
        fontName=FONT_SERIF_ITALIC, fontSize=14, leading=18,
        textColor=GHOST, spaceBefore=0, spaceAfter=GRID,
    )
    S["notes_prompt"] = ParagraphStyle(
        "notes_prompt", parent=base["Normal"],
        fontName=FONT_SANS_OBLIQUE, fontSize=8.5, leading=12,
        textColor=MUTED, spaceAfter=GRID*2,
    )

    # TOC
    S["toc_num"] = ParagraphStyle(
        "toc_num", parent=base["Normal"],
        fontName=FONT_SANS_BOLD, fontSize=9, leading=13,
        textColor=GOLD,
    )
    S["toc_name"] = ParagraphStyle(
        "toc_name", parent=base["Normal"],
        fontName=FONT_SANS, fontSize=9, leading=13,
        textColor=INK,
    )
    S["toc_detail"] = ParagraphStyle(
        "toc_detail", parent=base["Normal"],
        fontName=FONT_SANS, fontSize=8, leading=11,
        textColor=MUTED, alignment=TA_RIGHT,
    )

    # Belongs-to page
    S["belongs_label"] = ParagraphStyle(
        "belongs_label", parent=base["Normal"],
        fontName=FONT_SERIF_ITALIC, fontSize=13, leading=18,
        textColor=RULE, spaceBefore=0, spaceAfter=GRID,
    )

    # Flavor
    S["flavor"] = ParagraphStyle(
        "flavor", parent=base["Normal"],
        fontName=FONT_SANS, fontSize=8, leading=11,
        textColor=SLATE, spaceAfter=2,
    )

    return S


# =========================================================
# PAGE TEMPLATE — headers, footers, page numbers
# =========================================================
class CookbookCanvasHelper:
    """Draws on every page: footer rule + page number, optional header."""

    def __init__(self, tenant_name):
        self.tenant_name = tenant_name
        self.page_count = 0

    def on_page(self, canvas, doc):
        self.page_count += 1
        canvas.saveState()

        # Footer rule
        y = MARGIN_BOT - 18
        canvas.setStrokeColor(RULE)
        canvas.setLineWidth(0.5)
        canvas.line(MARGIN_L, y, PAGE_W - MARGIN_R, y)

        # Page number — centered
        canvas.setFont(FONT_SANS, 7)
        canvas.setFillColor(MUTED)
        canvas.drawCentredString(PAGE_W / 2, y - 12, str(self.page_count))

        # Tenant name on right side of footer
        canvas.setFont(FONT_SANS_OBLIQUE, 6.5)
        canvas.drawRightString(PAGE_W - MARGIN_R, y - 12, self.tenant_name)

        canvas.restoreState()

    def on_cover(self, canvas, doc):
        """Cover page — no header/footer."""
        self.page_count += 1


# =========================================================
# DATA LAYER
# =========================================================
def get_tenant_data(slug):
    """Pull everything we know about a tenant from the database."""
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row

    tenant = dict(db.execute("SELECT * FROM tenants WHERE slug=?", (slug,)).fetchone() or {})
    if not tenant:
        raise ValueError(f"No tenant: {slug}")

    dishes = []
    for d in db.execute("SELECT * FROM dishes WHERE tenant_slug=? ORDER BY food_cost DESC", (slug,)).fetchall():
        dish = dict(d)
        dish["ingredients"] = [dict(i) for i in db.execute(
            "SELECT * FROM dish_ingredients WHERE dish_slug=?", (dish["slug"],)).fetchall()]
        dish["flavors"] = {r["dimension"]: r["score"] for r in db.execute(
            "SELECT dimension, score FROM flavor_scores WHERE dish_slug=?", (dish["slug"],)).fetchall()}
        dish["nutrition"] = [dict(n) for n in db.execute(
            "SELECT * FROM nutrition WHERE dish_slug=?", (dish["slug"],)).fetchall()]
        for ing in dish["ingredients"]:
            if ing["usda_fdc_id"]:
                usda = db.execute("SELECT description FROM food WHERE fdc_id=?", (ing["usda_fdc_id"],)).fetchone()
                if usda:
                    ing["usda_description"] = usda["description"]
        dishes.append(dish)

    inventory = [dict(i) for i in db.execute(
        "SELECT * FROM inventory_items WHERE tenant_slug=? ORDER BY vendor_price DESC", (slug,)).fetchall()]

    db.close()
    return tenant, dishes, inventory


def get_all_platform_recipes():
    """Get recipes from the platform tenant — shared library anyone can drag in."""
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    dishes = []
    for d in db.execute("SELECT * FROM dishes WHERE tenant_slug='platform'").fetchall():
        dish = dict(d)
        dish["ingredients"] = [dict(i) for i in db.execute(
            "SELECT * FROM dish_ingredients WHERE dish_slug=?", (dish["slug"],)).fetchall()]
        dish["flavors"] = {r["dimension"]: r["score"] for r in db.execute(
            "SELECT dimension, score FROM flavor_scores WHERE dish_slug=?", (dish["slug"],)).fetchall()}
        for ing in dish["ingredients"]:
            if ing["usda_fdc_id"]:
                usda = db.execute("SELECT description FROM food WHERE fdc_id=?", (ing["usda_fdc_id"],)).fetchone()
                if usda:
                    ing["usda_description"] = usda["description"]
        dishes.append(dish)
    db.close()
    return dishes


# =========================================================
# TABLE BUILDER — one consistent style for all tables
# =========================================================
def _uniform_table(header_row, data_rows, col_widths, styles):
    """Build a table with the standard cookbook styling."""
    all_rows = []

    # Header
    h_cells = [Paragraph(h, styles["th"]) for h in header_row]
    all_rows.append(h_cells)

    # Data
    for row in data_rows:
        all_rows.append(row)  # Already Paragraph objects

    t = Table(all_rows, colWidths=col_widths, repeatRows=1)
    t.setStyle(TableStyle([
        # Header row
        ("BACKGROUND", (0, 0), (-1, 0), INK),
        ("TEXTCOLOR", (0, 0), (-1, 0), white),
        ("FONTNAME", (0, 0), (-1, 0), FONT_SANS_BOLD),
        ("FONTSIZE", (0, 0), (-1, 0), 7.5),
        ("TOPPADDING", (0, 0), (-1, 0), 5),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),

        # Data rows — alternating
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [PAGE_BG, CREAM]),

        # Alignment
        ("VALIGN", (0, 0), (-1, -1), "TOP"),

        # Grid — thin rules only
        ("LINEBELOW", (0, 0), (-1, 0), 0.75, GOLD),       # under header
        ("LINEBELOW", (0, 1), (-1, -2), 0.25, RULE),       # between rows
        ("LINEBELOW", (0, -1), (-1, -1), 0.75, RULE),      # bottom border
        ("LINEAFTER", (0, 0), (-2, -1), 0.25, RULE),       # column dividers
    ]))
    return t


# =========================================================
# CONTENT BUILDERS
# =========================================================
def _build_cover(story, tenant, dishes, inventory, S):
    """Cover page."""
    story.append(Spacer(1, 2.2 * inch))

    # Decorative rule above title
    story.append(HRFlowable(width="20%", thickness=1.5, color=GOLD,
                            spaceAfter=GRID*3, hAlign="CENTER"))
    story.append(Paragraph(tenant["name"], S["cover_title"]))

    # Decorative rule below title
    story.append(HRFlowable(width="30%", thickness=1.5, color=GOLD,
                            spaceBefore=GRID, spaceAfter=GRID*4, hAlign="CENTER"))

    story.append(Paragraph("Recipes  /  Costs  /  Nutrition", S["cover_sub"]))
    story.append(Spacer(1, GRID * 3))

    # Stats line
    stats_parts = [f"{len(dishes)} dishes", f"{len(inventory)} ingredients", "USDA-matched"]
    story.append(Paragraph("   \u2022   ".join(stats_parts), S["cover_detail"]))

    total_cost = sum(i["vendor_price"] or 0 for i in inventory)
    if total_cost > 0:
        story.append(Paragraph(f"Total inventory value: ${total_cost:,.2f}", S["cover_detail"]))

    story.append(Spacer(1, 2 * inch))

    # Bottom attribution
    story.append(Paragraph("Generated from live platform data", S["cover_detail"]))
    story.append(Paragraph("Powered by HowToCookAtHome + USDA FoodData Central", S["cover_detail"]))
    story.append(Paragraph(datetime.now().strftime("%B %Y"), S["cover_detail"]))
    story.append(PageBreak())


def _build_belongs_page(story, S):
    """This book belongs to..."""
    story.append(Spacer(1, 2.5 * inch))

    story.append(Paragraph("This cookbook belongs to:", S["belongs_label"]))
    story.append(Spacer(1, GRID))
    story.append(HRFlowable(width="55%", thickness=0.75, color=RULE))
    story.append(Spacer(1, GRID * 6))

    story.append(Paragraph("Date started:", S["belongs_label"]))
    story.append(Spacer(1, GRID))
    story.append(HRFlowable(width="35%", thickness=0.75, color=RULE))
    story.append(Spacer(1, GRID * 6))

    story.append(Paragraph("Kitchen:", S["belongs_label"]))
    story.append(Spacer(1, GRID))
    story.append(HRFlowable(width="45%", thickness=0.75, color=RULE))
    story.append(PageBreak())


def _build_toc(story, dishes, inventory, S):
    """Table of contents + inventory summary."""
    story.append(Paragraph("Contents", S["h1"]))
    story.append(HRFlowable(width="100%", thickness=1, color=RULE, spaceAfter=GRID*2))

    # TOC table — no heavy grid, just clean rows
    toc_rows = []
    for i, dish in enumerate(dishes, 1):
        cost_str = f"${dish['food_cost']:.2f}" if dish.get("food_cost") else "\u2014"
        price_str = f"${dish['menu_price']:.2f}" if dish.get("menu_price") else "\u2014"
        toc_rows.append([
            Paragraph(f"{i:02d}", S["toc_num"]),
            Paragraph(dish["name"], S["toc_name"]),
            Paragraph(f"Cost {cost_str}  \u00b7  Menu {price_str}", S["toc_detail"]),
        ])

    if toc_rows:
        toc = Table(toc_rows, colWidths=[0.4 * inch, 3.6 * inch, 2.0 * inch])
        toc.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LINEBELOW", (0, 0), (-1, -1), 0.25, GHOST),
        ]))
        story.append(toc)

    # Inventory overview
    if inventory:
        story.append(Spacer(1, GRID * 4))
        story.append(Paragraph("PANTRY OVERVIEW", S["h2"]))

        inv_data = []
        for item in inventory[:14]:  # keep to one page
            inv_data.append([
                Paragraph(item["display_name"] or item["item_name"], S["td_accent"]),
                Paragraph(f"{item['package_size'] or ''} {item['package_unit'] or ''}".strip(), S["td"]),
                Paragraph(f"${item['vendor_price']:.2f}" if item["vendor_price"] else "\u2014", S["td"]),
                Paragraph(item["unit_price_label"] or "\u2014", S["td"]),
                Paragraph("\u2713" if item["usda_fdc_id"] else "\u2014", S["td"]),
            ])

        inv_table = _uniform_table(
            ["Item", "Package", "Price", "Unit Cost", "USDA"],
            inv_data,
            [2.1 * inch, 1.1 * inch, 0.7 * inch, 1.0 * inch, 0.5 * inch],
            S,
        )
        story.append(inv_table)

    story.append(PageBreak())


def _build_recipe_page(story, dish, index, S):
    """One recipe page."""
    # Recipe title
    story.append(Paragraph(dish["name"], S["h1"]))
    story.append(HRFlowable(width="100%", thickness=1.5, color=GOLD, spaceAfter=GRID))

    # Cost callout bar
    if dish.get("food_cost") and dish.get("menu_price"):
        margin_pct = ((dish["menu_price"] - dish["food_cost"]) / dish["menu_price"] * 100) if dish["menu_price"] > 0 else 0
        story.append(Paragraph(
            f"Food Cost ${dish['food_cost']:.2f}   \u00b7   "
            f"Menu ${dish['menu_price']:.2f}   \u00b7   "
            f"Margin {margin_pct:.0f}%",
            S["callout"],
        ))
    elif dish.get("food_cost"):
        story.append(Paragraph(f"Food Cost: ${dish['food_cost']:.2f}", S["callout"]))

    # --- INGREDIENTS TABLE ---
    story.append(Paragraph("INGREDIENTS", S["h2"]))

    ing_rows = []
    for ing in dish["ingredients"]:
        usda_note = ing.get("usda_description", "")
        if len(usda_note) > 35:
            usda_note = usda_note[:32] + "..."
        amt = f"{ing['usage_amount'] or ''} {ing['usage_unit'] or ''}".strip()
        cost = f"${ing['item_cost']:.2f}" if ing.get("item_cost") else "\u2014"
        ing_rows.append([
            Paragraph(ing["display_name"] or ing["item_name"], S["td_accent"]),
            Paragraph(amt or "\u2014", S["td"]),
            Paragraph(cost, S["td"]),
            Paragraph(usda_note, S["td"]),
        ])

    if ing_rows:
        ing_table = _uniform_table(
            ["Ingredient", "Amount", "Cost", "USDA Source"],
            ing_rows,
            [1.8 * inch, 0.9 * inch, 0.65 * inch, 2.2 * inch],
            S,
        )
        story.append(ing_table)

    # --- METHOD ---
    if dish.get("steps"):
        try:
            steps = json.loads(dish["steps"]) if isinstance(dish["steps"], str) else dish["steps"]
        except Exception:
            steps = [dish["steps"]]

        story.append(Paragraph("METHOD", S["h2"]))
        for i, step in enumerate(steps, 1):
            step_text = step if isinstance(step, str) else str(step)
            # Two-column layout: number | instruction
            step_table = Table(
                [[Paragraph(f"{i}.", S["step_num"]), Paragraph(step_text, S["body"])]],
                colWidths=[0.35 * inch, CONTENT_W - 0.35 * inch],
            )
            step_table.setStyle(TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                ("LEFTPADDING", (0, 0), (0, 0), 0),
                ("RIGHTPADDING", (0, 0), (0, 0), 4),
                ("LEFTPADDING", (1, 0), (1, 0), 0),
            ]))
            story.append(step_table)

    # --- FLAVOR PROFILE (compact bar chart in text) ---
    if dish.get("flavors"):
        story.append(Paragraph("FLAVOR PROFILE", S["h2"]))
        top_flavors = sorted(dish["flavors"].items(), key=lambda x: -x[1])[:5]
        for dim, score in top_flavors:
            bar_filled = int(round(score))
            bar_empty = 10 - bar_filled
            bar_str = "\u2588" * bar_filled + "\u2591" * bar_empty
            story.append(Paragraph(
                f'<font name="{FONT_SANS_BOLD}" color="#{INK.hexval()[2:]}">'
                f'{dim.title():12s}</font>  '
                f'<font name="Courier" size="7" color="#{GOLD.hexval()[2:]}">{bar_str}</font>  '
                f'<font name="{FONT_SANS}" size="7" color="#{MUTED.hexval()[2:]}">{score:.1f}</font>',
                S["flavor"],
            ))

    story.append(PageBreak())


def _build_notes_page(story, dish, S):
    """Facing notes page for a recipe."""
    story.append(Paragraph(f"Notes \u2014 {dish['name']}", S["notes_head"]))
    story.append(HRFlowable(width="100%", thickness=0.5, color=GHOST, spaceAfter=GRID))

    # Contextual prompts
    prompts = _get_notes_prompts(dish)
    for p in prompts:
        story.append(Paragraph(p, S["notes_prompt"]))

    # Ruled lines
    story.append(Spacer(1, GRID * 2))
    for _ in range(22):
        story.append(HRFlowable(width="100%", thickness=0.25, color=GHOST))
        story.append(Spacer(1, 20))


def _build_blank_notes(story, S, count=3):
    """Extra blank notes pages at the back."""
    for i in range(count):
        story.append(PageBreak())
        story.append(Paragraph("Notes", S["notes_head"]))
        story.append(HRFlowable(width="100%", thickness=0.5, color=GHOST, spaceAfter=GRID * 2))
        for _ in range(25):
            story.append(HRFlowable(width="100%", thickness=0.25, color=GHOST))
            story.append(Spacer(1, 20))


def _build_back_cover(story, tenant, S):
    """Back cover page."""
    story.append(PageBreak())
    story.append(Spacer(1, 2.5 * inch))

    story.append(HRFlowable(width="15%", thickness=1.5, color=GOLD,
                            spaceAfter=GRID*3, hAlign="CENTER"))
    story.append(Paragraph(tenant["name"], S["cover_title"]))
    story.append(HRFlowable(width="15%", thickness=1.5, color=GOLD,
                            spaceBefore=GRID*2, spaceAfter=GRID*4, hAlign="CENTER"))

    lines = [
        "Generated automatically from live restaurant data.",
        "Every ingredient matched against USDA FoodData Central.",
        "Every cost calculated from real vendor invoices.",
        "Every flavor profile derived from actual ingredient composition.",
    ]
    for line in lines:
        story.append(Paragraph(line, S["cover_sub"]))

    story.append(Spacer(1, GRID * 6))
    story.append(Paragraph("howtocookathome.com", S["cover_detail"]))


def _get_notes_prompts(dish):
    """Generate contextual prompts for the notes page."""
    prompts = ["What did you change from the recipe?"]

    if dish.get("food_cost") and dish["food_cost"] > 0:
        prompts.append(
            f"Your cost per plate is ${dish['food_cost']:.2f}. "
            f"Can you get it lower? What would you swap?"
        )

    if dish.get("flavors"):
        top = max(dish["flavors"].items(), key=lambda x: x[1])
        low = min(dish["flavors"].items(), key=lambda x: x[1])
        prompts.append(
            f"Dominant flavor: {top[0]}. Weakest: {low[0]}. Would you rebalance?"
        )

    prompts.append("Who did you cook this for? What did they think?")
    prompts.append("Next time I'll try:")

    return prompts


# =========================================================
# MAIN BUILD
# =========================================================
def build_cookbook(slug, output_path=None, selected_slugs=None, include_platform=False):
    """Generate the cookbook PDF.

    Args:
        slug: Tenant slug
        output_path: Where to save (defaults to output/tenants/<slug>/<slug>-cookbook.pdf)
        selected_slugs: List of dish slugs to include (None = all)
        include_platform: Also include platform-level recipes (shared library)

    Returns:
        Path to the generated PDF.
    """
    tenant, dishes, inventory = get_tenant_data(slug)

    if include_platform:
        platform_recipes = get_all_platform_recipes()
        dishes = dishes + platform_recipes

    if selected_slugs:
        dishes = [d for d in dishes if d["slug"] in selected_slugs]

    if not output_path:
        output_path = os.path.join(BASE_DIR, "output", "tenants", slug, f"{slug}-cookbook.pdf")
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    S = _build_styles()
    helper = CookbookCanvasHelper(tenant["name"])

    # Build document with two page templates:
    # "cover" — no header/footer
    # "body" — header/footer with page numbers
    frame = Frame(MARGIN_L, MARGIN_BOT, CONTENT_W, PAGE_H - MARGIN_TOP - MARGIN_BOT,
                  id="main")

    doc = BaseDocTemplate(
        output_path, pagesize=letter,
        topMargin=MARGIN_TOP, bottomMargin=MARGIN_BOT,
        leftMargin=MARGIN_L, rightMargin=MARGIN_R,
        title=f"{tenant['name']} Cookbook",
        author="HowToCookAtHome",
    )
    doc.addPageTemplates([
        PageTemplate(id="cover", frames=[frame], onPage=helper.on_cover),
        PageTemplate(id="body", frames=[frame], onPage=helper.on_page),
    ])

    story = []

    # --- COVER (uses "cover" template — no page number) ---
    _build_cover(story, tenant, dishes, inventory, S)

    # Switch to body template for all remaining pages
    from reportlab.platypus import NextPageTemplate
    story.insert(-1, NextPageTemplate("body"))  # insert before the PageBreak

    # --- BELONGS TO ---
    _build_belongs_page(story, S)

    # --- TABLE OF CONTENTS ---
    _build_toc(story, dishes, inventory, S)

    # --- RECIPE + NOTES SPREADS ---
    for idx, dish in enumerate(dishes):
        _build_recipe_page(story, dish, idx + 1, S)
        _build_notes_page(story, dish, S)
        if idx < len(dishes) - 1:
            story.append(PageBreak())

    # --- EXTRA BLANK NOTES ---
    _build_blank_notes(story, S, count=3)

    # --- BACK COVER ---
    _build_back_cover(story, tenant, S)

    # Build
    doc.build(story)
    return output_path


if __name__ == "__main__":
    slug = sys.argv[1] if len(sys.argv) > 1 else "big-reds-bbq"
    include_platform = "--all" in sys.argv
    selected = None
    for arg in sys.argv[2:]:
        if arg.startswith("--"):
            continue
        if selected is None:
            selected = []
        selected.append(arg)

    path = build_cookbook(slug, selected_slugs=selected, include_platform=include_platform)
    print(f"Cookbook generated: {path}")
