#!/usr/bin/env python3
"""
Render a week's yap YAML into a branded PDF.

Output: output/yap/<week>.pdf

Design notes:
- White background (prints cleanly, friendly in email preview panes).
- Heavy black headlines + red accents — echoes the sticker without the
  ink-eating full-black background.
- No Unicode sub/superscripts (reportlab's built-in fonts can't render
  them; see skills/pdf/SKILL.md).

Usage:
    python3 scripts/render_yap.py                  # this week
    python3 scripts/render_yap.py --week 2026-W16
"""
import argparse
import datetime as dt
import sys
from pathlib import Path

import yaml
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.units import inch
from reportlab.lib.colors import HexColor, black, white
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_LEFT
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    PageBreak, KeepTogether, Flowable,
)

BASE_DIR = Path(__file__).resolve().parent.parent
YAP_DIR = BASE_DIR / "content" / "yap"
OUT_DIR = BASE_DIR / "output" / "yap"

RED = HexColor("#D91E18")
INK = HexColor("#0A0A0A")
MUTED = HexColor("#6A6A6A")
LINE = HexColor("#E5E5E5")


def current_week_id():
    iso = dt.date.today().isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def parse_week(s):
    s = s.strip().upper().replace("W", "-").replace("--", "-")
    parts = s.split("-")
    y, w = int(parts[0]), int(parts[1])
    return f"{y}-W{w:02d}"


class RedBar(Flowable):
    """A thick red bar used as a section divider."""
    def __init__(self, width, height=6, color=RED):
        super().__init__()
        self.width = width
        self.height = height
        self.color = color

    def draw(self):
        self.canv.setFillColor(self.color)
        self.canv.rect(0, 0, self.width, self.height, fill=1, stroke=0)


def build_styles():
    base = getSampleStyleSheet()
    return {
        "eyebrow": ParagraphStyle(
            "eyebrow", parent=base["Normal"],
            fontName="Helvetica-Bold", fontSize=9, leading=12,
            textColor=RED, spaceAfter=4,
        ),
        "hero": ParagraphStyle(
            "hero", parent=base["Title"],
            fontName="Helvetica-Bold", fontSize=42, leading=44,
            textColor=INK, spaceAfter=10, alignment=TA_LEFT,
        ),
        "dek": ParagraphStyle(
            "dek", parent=base["Normal"],
            fontName="Helvetica", fontSize=13, leading=19,
            textColor=INK, spaceAfter=16,
        ),
        "h2": ParagraphStyle(
            "h2", parent=base["Heading2"],
            fontName="Helvetica-Bold", fontSize=18, leading=22,
            textColor=INK, spaceBefore=12, spaceAfter=8,
        ),
        "h3": ParagraphStyle(
            "h3", parent=base["Heading3"],
            fontName="Helvetica-Bold", fontSize=13, leading=16,
            textColor=INK, spaceBefore=6, spaceAfter=4,
        ),
        "body": ParagraphStyle(
            "body", parent=base["Normal"],
            fontName="Helvetica", fontSize=11, leading=16,
            textColor=INK, spaceAfter=8,
        ),
        "muted": ParagraphStyle(
            "muted", parent=base["Normal"],
            fontName="Helvetica", fontSize=9, leading=13,
            textColor=MUTED,
        ),
        "big_red": ParagraphStyle(
            "big_red", parent=base["Normal"],
            fontName="Helvetica-Bold", fontSize=36, leading=40,
            textColor=RED, spaceAfter=6,
        ),
    }


def load_yap(week_id):
    path = YAP_DIR / f"{week_id}.yaml"
    if not path.exists():
        raise SystemExit(f"  Missing draft: {path}\n  Run: python3 scripts/build_yap.py --week {week_id}")
    with open(path) as f:
        return yaml.safe_load(f)


def build_story(yap, styles, page_width):
    story = []

    # Eyebrow + hero
    story.append(Paragraph(f"HOWTOCOOKATHOME &#183; YAP &#183; {yap['week']}", styles["eyebrow"]))
    story.append(Paragraph(yap["headline"].upper(), styles["hero"]))
    story.append(RedBar(page_width, 5))
    story.append(Spacer(1, 14))
    story.append(Paragraph(yap["dek"], styles["dek"]))

    # Recipes
    story.append(Paragraph("THIS WEEK'S THREE", styles["eyebrow"]))
    story.append(Spacer(1, 4))
    for i, r in enumerate(yap.get("recipes", []), 1):
        story.append(Paragraph(f"{i:02d} &middot; {r['title']}", styles["h3"]))
        if r.get("why"):
            story.append(Paragraph(r["why"], styles["body"]))
        else:
            story.append(Paragraph(
                "<i>Matt hasn't written the why yet — the editor missed Saturday.</i>",
                styles["muted"]
            ))
        story.append(Spacer(1, 6))

    story.append(Spacer(1, 12))
    story.append(RedBar(page_width, 2, MUTED))
    story.append(Spacer(1, 12))

    # Savings math
    sm = yap.get("savings_math", {}) or {}
    out = float(sm.get("dinner_out", 0))
    home = float(sm.get("dinner_home", 0))
    meals = int(sm.get("meals_per_week", 0))
    weekly = max(0.0, (out - home) * meals)
    story.append(Paragraph("THE DIVIDEND", styles["eyebrow"]))
    story.append(Paragraph(f"${weekly:.0f} / week", styles["big_red"]))
    math_line = f"(${out:.0f} out &minus; ${home:.0f} home) &times; {meals} meals = <b>${weekly:.0f}</b> redirected."
    story.append(Paragraph(math_line, styles["body"]))
    if sm.get("commentary"):
        story.append(Paragraph(sm["commentary"], styles["body"]))

    story.append(Spacer(1, 14))

    # Two CTAs side by side
    cta_p = yap.get("cta_patron") or {}
    cta_e = yap.get("cta_experience") or {}
    patron_cell = [
        Paragraph(cta_p.get("headline", ""), styles["h3"]),
        Paragraph(cta_p.get("body", ""), styles["body"]),
    ]
    exp_cell = [
        Paragraph(cta_e.get("headline", ""), styles["h3"]),
        Paragraph(cta_e.get("body", ""), styles["body"]),
    ]
    col_w = (page_width - 20) / 2
    cta_tbl = Table([[patron_cell, exp_cell]], colWidths=[col_w, col_w])
    cta_tbl.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("LINEABOVE", (0, 0), (-1, 0), 3, RED),
        ("TOPPADDING", (0, 0), (-1, -1), 10),
    ]))
    story.append(KeepTogether(cta_tbl))

    story.append(Spacer(1, 18))
    if yap.get("closing"):
        story.append(Paragraph(yap["closing"], styles["body"]))

    return story


def build_pdf(week_id, out_path=None):
    yap = load_yap(week_id)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = Path(out_path) if out_path else OUT_DIR / f"{week_id}.pdf"

    margin_x, margin_y = 0.75 * inch, 0.75 * inch
    page_w = LETTER[0] - 2 * margin_x
    styles = build_styles()

    doc = SimpleDocTemplate(
        str(out_path), pagesize=LETTER,
        leftMargin=margin_x, rightMargin=margin_x,
        topMargin=margin_y, bottomMargin=margin_y,
        title=yap.get("subject", f"Yap {week_id}"),
        author="HowToCookAtHome",
    )

    def on_page(canv, doc_):
        # Footer
        canv.saveState()
        canv.setFillColor(MUTED)
        canv.setFont("Helvetica", 8)
        canv.drawString(margin_x, 0.5 * inch,
                        f"howtocookathome.com  \u00B7  yap {week_id}")
        canv.drawRightString(LETTER[0] - margin_x, 0.5 * inch,
                             f"page {doc_.page}")
        # Red corner mark
        canv.setFillColor(RED)
        canv.rect(LETTER[0] - margin_x - 18, LETTER[1] - margin_y - 4, 18, 4,
                  fill=1, stroke=0)
        canv.restoreState()

    story = build_story(yap, styles, page_w)
    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
    return out_path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--week", default=None)
    ap.add_argument("-o", "--output", default=None)
    args = ap.parse_args()
    week_id = parse_week(args.week) if args.week else current_week_id()
    path = build_pdf(week_id, args.output)
    print(f"  Rendered: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
