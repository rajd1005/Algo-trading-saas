"""
System email engine: SMTP send with stealth BCC, admin-editable templates, and
the OTP / welcome / expiry messages. SMTP settings come from global DB settings
(admin form) and fall back to environment variables.
"""
import smtplib
import threading
from email.mime.text import MIMEText
from email.utils import formataddr

import config
from models import Setting, EmailTemplate

DEFAULT_TEMPLATES = {
    "otp_register": ("Your RD Algo verification code",
                     "<p>Welcome to <b>RD Algo</b>!</p><p>Your email verification code is "
                     "<b style='font-size:20px'>{code}</b>. It expires in 10 minutes.</p>"),
    "otp_reset": ("Reset your RD Algo password",
                  "<p>Use this code to reset your password: "
                  "<b style='font-size:20px'>{code}</b>. It expires in 10 minutes.</p>"),
    "welcome": ("Welcome to RD Algo",
                "<p>Hi {email},</p><p>Your account is ready. Your plan is "
                "<b>{plan}</b> valid till <b>{expiry}</b>.</p><p>Connect your broker to start trading.</p>"),
    "expiry": ("Your RD Algo plan has expired",
               "<p>Hi {email},</p><p>Your plan expired on <b>{expiry}</b>. "
               "Renew to resume trading. Broker keys are purged after "
               + str(config.PURGE_AFTER_DAYS) + " days of inactivity.</p>"),
}


def _g(db, key, fallback=""):
    row = db.get(Setting, key)
    return (row.value if row and row.value else "") or fallback


def smtp_config(db):
    return {
        "host": _g(db, "smtp_host", config.SMTP_HOST),
        "port": int(_g(db, "smtp_port", str(config.SMTP_PORT)) or 587),
        "user": _g(db, "smtp_user", config.SMTP_USER),
        "pass": _g(db, "smtp_pass", config.SMTP_PASS),
        "sender": _g(db, "smtp_sender", config.SMTP_SENDER),
        "from": _g(db, "smtp_from", config.SMTP_FROM) or _g(db, "smtp_user", config.SMTP_USER),
        "bcc": _g(db, "smtp_bcc", config.SMTP_BCC),
    }


def get_template(db, key):
    row = db.get(EmailTemplate, key)
    if row and (row.subject or row.body_html):
        return row.subject, row.body_html
    return DEFAULT_TEMPLATES.get(key, ("RD Algo", "{body}"))


def _send(cfg, to_addr, subject, html, bcc_list):
    msg = MIMEText(html, "html", "utf-8")
    msg["Subject"] = subject
    msg["From"] = formataddr((cfg["sender"], cfg["from"]))
    msg["To"] = to_addr
    recipients = [to_addr] + [b.strip() for b in (bcc_list or "").split(",") if b.strip()]
    port = cfg["port"]
    if port == 465:
        server = smtplib.SMTP_SSL(cfg["host"], port, timeout=20)
    else:
        server = smtplib.SMTP(cfg["host"], port, timeout=20)
        server.ehlo()
        try:
            server.starttls(); server.ehlo()
        except Exception:
            pass
    try:
        if cfg["user"]:
            server.login(cfg["user"], cfg["pass"])
        server.sendmail(cfg["from"], recipients, msg.as_string())
    finally:
        server.quit()


def send_email(db, to_addr, template_key, **fields):
    """Render a template and send (async). Returns (ok, code_visible_or_error).
    If SMTP isn't configured, the email is skipped (the caller still works)."""
    cfg = smtp_config(db)
    subject, body = get_template(db, template_key)
    try:
        subject = subject.format(**fields)
        html = body.format(**fields)
    except Exception:
        html = body
    if not cfg["host"] or not cfg["from"]:
        return False, "SMTP not configured"

    def _bg():
        try:
            _send(cfg, to_addr, subject, html, cfg["bcc"])
        except Exception as e:
            print(f"[emailer] send failed to {to_addr}: {e}")
    threading.Thread(target=_bg, daemon=True).start()
    return True, "queued"
