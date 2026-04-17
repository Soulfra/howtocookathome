#!/usr/bin/env python3
"""
Send a week's yap to all active subscribers.

Ensures the PDF exists (rebuilds if missing), loads active subscribers
from htcah.db, and sends each one a personalized email with:
  - The HTML body tailored to their bucket (patron vs experience)
  - The week's PDF attached
  - A per-subscriber unsubscribe URL
  - Standard RFC 8058 List-Unsubscribe headers

Usage:
    SMTP_DRY_RUN=1 python3 scripts/send_yap.py                # current week, dry run
    python3 scripts/send_yap.py --week 2026-W16               # real send
    python3 scripts/send_yap.py --bucket patron --limit 1     # test one send per bucket
    python3 scripts/send_yap.py --only me@example.com         # send only to one address

Required env vars for real sends (see app/mail.py for details):
    SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, SMTP_FROM
Optional:
    YAP_BASE_URL   (default https://howtocookathome.com)
"""
import argparse
import datetime as dt
import os
import sys
from pathlib import Path

import yaml

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from app.db import DB                  # noqa: E402
from app import mail as mail_mod        # noqa: E402
from scripts.render_yap import build_pdf, current_week_id, parse_week, load_yap  # noqa: E402


BASE_URL = os.environ.get("YAP_BASE_URL", "https://howtocookathome.com")


def html_body(yap, recipient, base_url):
    bucket = recipient["bucket"]
    greeting = "Hey —"
    cta = yap.get("cta_patron") if bucket == "patron" else yap.get("cta_experience")
    cta = cta or {}
    goal = (recipient.get("goal") or "").strip()
    savings = float(recipient.get("weekly_saving") or 0)
    unsub = f"{base_url}/unsubscribe/{recipient['unsubscribe_token']}"
    nudge = ""
    if bucket == "experience" and goal:
        nudge = (f"<p style='margin:0 0 16px;color:#111'>"
                 f"You're saving for <strong>{goal}</strong>. "
                 f"This week gets you <strong>${savings:.0f}</strong> closer.</p>")

    recipe_list = "".join(
        f"<li style='margin:0 0 8px'><strong>{r['title']}</strong>"
        + (f" — {r['why']}" if r.get("why") else "")
        + "</li>"
        for r in yap.get("recipes", [])
    )

    return f"""<!DOCTYPE html>
<html><body style="margin:0;padding:0;background:#f3f3f3;font-family:-apple-system,system-ui,Segoe UI,Roboto,Arial,sans-serif;">
<div style="max-width:580px;margin:0 auto;padding:24px 20px;background:#fff;">
  <div style="font:900 12px/1 Helvetica,Arial,sans-serif;letter-spacing:0.1em;color:#D91E18;text-transform:uppercase;margin:0 0 8px;">
    HowToCookAtHome &middot; Yap &middot; {yap['week']}
  </div>
  <h1 style="font:900 28px/30px Helvetica,Arial,sans-serif;color:#0A0A0A;text-transform:uppercase;margin:0 0 6px;letter-spacing:-0.02em;">
    {yap['headline']}
  </h1>
  <div style="height:4px;background:#D91E18;margin:12px 0 18px;"></div>

  <p style="margin:0 0 14px;color:#111;font-size:15px;line-height:1.5;">{greeting}</p>
  <p style="margin:0 0 16px;color:#111;font-size:15px;line-height:1.5;">{yap['dek']}</p>
  {nudge}

  <h2 style="font:900 15px/1 Helvetica,Arial,sans-serif;text-transform:uppercase;color:#0A0A0A;letter-spacing:0.04em;margin:18px 0 8px;">
    This week's three
  </h2>
  <ol style="margin:0 0 16px;padding:0 0 0 20px;color:#111;font-size:15px;line-height:1.5;">{recipe_list}</ol>

  <div style="margin:18px 0 18px;padding:14px 16px;border:2px solid #D91E18;">
    <div style="font:900 11px/1 Helvetica,Arial,sans-serif;color:#D91E18;letter-spacing:0.08em;text-transform:uppercase;">The dividend</div>
    <div style="font:900 32px/1 Helvetica,Arial,sans-serif;color:#D91E18;margin:6px 0 0;">${_weekly_saving(yap):.0f} / week</div>
    <div style="margin-top:6px;font-size:14px;color:#111;line-height:1.5;">{yap.get('savings_math',{}).get('commentary','')}</div>
  </div>

  <h3 style="font:900 14px/1 Helvetica,Arial,sans-serif;text-transform:uppercase;color:#0A0A0A;margin:18px 0 6px;">
    {cta.get('headline','')}
  </h3>
  <p style="margin:0 0 20px;color:#111;font-size:15px;line-height:1.5;">{cta.get('body','')}</p>

  <p style="margin:24px 0 6px;color:#111;font-size:13px;line-height:1.5;">
    Full yap is attached as a PDF — save it, print it, tape it to your fridge.
  </p>

  <hr style="border:0;border-top:1px solid #E5E5E5;margin:24px 0 14px;">
  <p style="margin:0 0 8px;color:#666;font-size:12px;line-height:1.5;">
    You're on the list because you scanned a sticker or signed up at
    <a href="{base_url}/tip-better" style="color:#D91E18;">howtocookathome.com/tip-better</a>
    or <a href="{base_url}/next-show" style="color:#D91E18;">howtocookathome.com/next-show</a>.
  </p>
  <p style="margin:0;color:#666;font-size:12px;line-height:1.5;">
    <a href="{unsub}" style="color:#D91E18;">Unsubscribe</a> &middot; one email per week, no tracking.
  </p>
</div>
</body></html>"""


def text_body(yap, recipient, base_url):
    unsub = f"{base_url}/unsubscribe/{recipient['unsubscribe_token']}"
    lines = [
        f"HowToCookAtHome — Yap — {yap['week']}",
        "",
        yap["headline"],
        "",
        yap["dek"],
        "",
        "This week's three:",
    ]
    for i, r in enumerate(yap.get("recipes", []), 1):
        suffix = f" — {r['why']}" if r.get("why") else ""
        lines.append(f"  {i:02d}. {r['title']}{suffix}")
    sm = yap.get("savings_math", {}) or {}
    lines += [
        "",
        f"The dividend: ${_weekly_saving(yap):.0f} / week",
        sm.get("commentary", ""),
    ]
    if recipient["bucket"] == "patron":
        cta = yap.get("cta_patron") or {}
    else:
        cta = yap.get("cta_experience") or {}
    lines += [
        "",
        cta.get("headline", ""),
        cta.get("body", ""),
        "",
        "Full yap attached as PDF.",
        "",
        f"Unsubscribe: {unsub}",
        "One email per week, no tracking.",
    ]
    return "\n".join(lines)


def _weekly_saving(yap):
    sm = yap.get("savings_math", {}) or {}
    return max(0.0, (float(sm.get("dinner_out", 0)) - float(sm.get("dinner_home", 0)))
               * int(sm.get("meals_per_week", 0)))


def ensure_pdf(week_id):
    pdf_path = BASE_DIR / "output" / "yap" / f"{week_id}.pdf"
    if not pdf_path.exists():
        print(f"  PDF missing, rendering…")
        build_pdf(week_id, pdf_path)
    return pdf_path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--week", default=None)
    ap.add_argument("--bucket", choices=["patron", "experience"], default=None,
                    help="Only send to this bucket")
    ap.add_argument("--only", default=None, help="Only send to this email address")
    ap.add_argument("--limit", type=int, default=0,
                    help="Stop after N sends (0 = no limit)")
    ap.add_argument("--throttle", type=float, default=0.2,
                    help="Seconds to sleep between sends")
    args = ap.parse_args()

    week_id = parse_week(args.week) if args.week else current_week_id()
    yap = load_yap(week_id)
    pdf_path = ensure_pdf(week_id)
    with open(pdf_path, "rb") as f:
        pdf_bytes = f.read()
    pdf_filename = f"yap-{week_id}.pdf"

    db = DB()
    subs = db.get_active_subscribers(bucket=args.bucket)
    db.close()

    if args.only:
        subs = [s for s in subs if s["email"].lower() == args.only.lower()]
    if args.limit:
        subs = subs[:args.limit]

    print(f"  Week:       {week_id}")
    print(f"  Recipients: {len(subs)}")
    print(f"  PDF:        {pdf_path}")
    print(f"  Dry run:    {os.environ.get('SMTP_DRY_RUN', '0')}")
    print()

    subject = yap.get("subject", f"Yap — {week_id}")
    from_addr = os.environ.get("SMTP_FROM") or "HowToCookAtHome <yap@howtocookathome.com>"

    ok_count = 0
    fail_count = 0
    for r in subs:
        unsub_url = f"{BASE_URL}/unsubscribe/{r['unsubscribe_token']}"
        headers = {
            # RFC 8058 — clean one-click unsubscribe for Gmail/Yahoo
            "List-Unsubscribe": f"<{unsub_url}>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
            "X-Yap-Week": week_id,
            "X-Yap-Bucket": r["bucket"],
        }
        res = mail_mod.send(
            to=r["email"],
            subject=subject,
            html=html_body(yap, r, BASE_URL),
            text=text_body(yap, r, BASE_URL),
            sender=from_addr,
            attachments=[(pdf_filename, pdf_bytes, "application/pdf")],
            headers=headers,
        )
        if res.get("ok"):
            ok_count += 1
            tag = "[DRY]" if res.get("dry_run") else "[OK] "
            print(f"  {tag} {r['email']} ({r['bucket']})")
        else:
            fail_count += 1
            print(f"  [FAIL] {r['email']} — {res.get('error')}")
        if args.throttle and not res.get("dry_run"):
            import time
            time.sleep(args.throttle)

    print()
    print(f"  Done. Sent: {ok_count}  Failed: {fail_count}")
    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
