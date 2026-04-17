"""
SMTP mail sender — thin abstraction so the rest of the codebase never imports smtplib.

Parameterized by env vars so the implementation can be swapped without
touching callers. Design goal: if SMTP deliverability tanks, flip env
vars or replace this module's send() with a Resend/Postmark/SES call.

Env vars:
    SMTP_HOST         (required) e.g. smtp.mailserver.example
    SMTP_PORT         (default 587)
    SMTP_USER         (required unless SMTP_DRY_RUN)
    SMTP_PASS         (required unless SMTP_DRY_RUN)
    SMTP_FROM         (required) "HowToCookAtHome <yap@howtocookathome.com>"
    SMTP_STARTTLS     (default "1" — set "0" for plain, unusual)
    SMTP_DRY_RUN      (default "0") if "1", log instead of sending
    SMTP_LOG_DIR      (default data/mail_log/) where dry-run .eml files land

Usage:
    from app.mail import send
    send(
        to="you@example.com",
        subject="Yap — 2026 W16",
        html="<h1>Hi</h1>",
        text="Hi",
        attachments=[("yap-2026-16.pdf", pdf_bytes, "application/pdf")],
    )
"""

import os
import ssl
import smtplib
import mimetypes
from email.message import EmailMessage
from email.utils import make_msgid, formatdate
from pathlib import Path
import datetime as _dt


BASE_DIR = Path(__file__).resolve().parent.parent
LOG_DIR = Path(os.environ.get("SMTP_LOG_DIR", BASE_DIR / "data" / "mail_log"))


class MailError(Exception):
    pass


def _truthy(v, default="0"):
    return str(v if v is not None else default).strip().lower() in ("1", "true", "yes", "on")


def _config():
    cfg = {
        "host":     os.environ.get("SMTP_HOST", ""),
        "port":     int(os.environ.get("SMTP_PORT", "587")),
        "user":     os.environ.get("SMTP_USER", ""),
        "password": os.environ.get("SMTP_PASS", ""),
        "sender":   os.environ.get("SMTP_FROM", ""),
        "starttls": _truthy(os.environ.get("SMTP_STARTTLS"), "1"),
        "dry_run":  _truthy(os.environ.get("SMTP_DRY_RUN"), "0"),
    }
    return cfg


def _build_message(to, subject, html="", text="", sender=None, attachments=None, headers=None):
    """Build an EmailMessage with multipart/alternative body + attachments."""
    msg = EmailMessage()
    msg["From"] = sender or _config()["sender"]
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="howtocookathome.com")
    msg["MIME-Version"] = "1.0"
    msg["Auto-Submitted"] = "auto-generated"
    for k, v in (headers or {}).items():
        msg[k] = v

    # Body: text fallback + HTML alternative
    if not text and not html:
        raise MailError("send() requires either text or html")
    if text and html:
        msg.set_content(text)
        msg.add_alternative(html, subtype="html")
    elif html:
        # Still set a minimal plain fallback for spam filters
        msg.set_content("This is an HTML email — view in an HTML-capable client.")
        msg.add_alternative(html, subtype="html")
    else:
        msg.set_content(text)

    for att in (attachments or []):
        if len(att) == 3:
            filename, data, ctype = att
        else:
            filename, data = att
            ctype, _ = mimetypes.guess_type(filename)
            ctype = ctype or "application/octet-stream"
        maintype, subtype = ctype.split("/", 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)

    return msg


def _write_log(msg):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_to = (msg["To"] or "unknown").replace("@", "_at_").replace(",", "_")
    path = LOG_DIR / f"{ts}-{safe_to}.eml"
    with open(path, "wb") as f:
        f.write(bytes(msg))
    return path


def send(to, subject, html="", text="", sender=None, attachments=None,
         headers=None, config=None):
    """Send one email. Returns dict(ok=bool, to=str, log=path_or_None, error=str).

    In dry-run mode (SMTP_DRY_RUN=1) the message is written to data/mail_log/
    as an .eml file and nothing leaves the machine. This is how we test
    before touching real credentials.
    """
    cfg = {**_config(), **(config or {})}
    msg = _build_message(to, subject, html=html, text=text, sender=sender or cfg["sender"],
                         attachments=attachments, headers=headers)

    if cfg["dry_run"]:
        path = _write_log(msg)
        return {"ok": True, "to": to, "log": str(path), "error": None, "dry_run": True}

    if not cfg["host"]:
        return {"ok": False, "to": to, "log": None, "error": "SMTP_HOST not set"}
    if not cfg["sender"]:
        return {"ok": False, "to": to, "log": None, "error": "SMTP_FROM not set"}

    try:
        if cfg["port"] == 465:
            # Implicit TLS (SMTPS)
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(cfg["host"], cfg["port"], context=ctx, timeout=30) as s:
                if cfg["user"]:
                    s.login(cfg["user"], cfg["password"])
                s.send_message(msg)
        else:
            with smtplib.SMTP(cfg["host"], cfg["port"], timeout=30) as s:
                s.ehlo()
                if cfg["starttls"]:
                    ctx = ssl.create_default_context()
                    s.starttls(context=ctx)
                    s.ehlo()
                if cfg["user"]:
                    s.login(cfg["user"], cfg["password"])
                s.send_message(msg)
        return {"ok": True, "to": to, "log": None, "error": None}
    except Exception as e:
        return {"ok": False, "to": to, "log": None, "error": f"{type(e).__name__}: {e}"}


def send_bulk(recipients, subject, html_for, text_for=None, sender=None,
              attachment_for=None, headers=None, throttle_sec=0.0):
    """Send individualized copies to many recipients.

    recipients: list of dicts. Each must have 'email'; may have any other keys
                used by html_for/text_for/attachment_for to personalize.
    html_for(r) -> str        : returns HTML body for recipient r
    text_for(r) -> str | None : returns plain-text body (optional)
    attachment_for(r) -> list | None : returns attachments for recipient r
    throttle_sec : sleep between sends to avoid tripping provider limits

    Returns list of per-recipient results.
    """
    import time
    results = []
    for r in recipients:
        to = r.get("email")
        if not to:
            results.append({"ok": False, "to": None, "error": "missing email"})
            continue
        html = html_for(r) if html_for else ""
        text = text_for(r) if text_for else ""
        atts = attachment_for(r) if attachment_for else None
        res = send(to=to, subject=subject, html=html, text=text,
                   sender=sender, attachments=atts, headers=headers)
        res["recipient"] = to
        results.append(res)
        if throttle_sec:
            time.sleep(throttle_sec)
    return results


if __name__ == "__main__":
    # Tiny self-test: send one dry-run message.
    os.environ.setdefault("SMTP_DRY_RUN", "1")
    os.environ.setdefault("SMTP_FROM", "HowToCookAtHome <yap@howtocookathome.com>")
    r = send(
        to="you@example.com",
        subject="Mail self-test",
        text="Plain body",
        html="<p>HTML body</p>",
    )
    print(r)
