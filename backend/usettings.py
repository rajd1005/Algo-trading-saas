"""Per-user key/value settings (kill switch, providers, daily limits, md_status…).

Global settings (SMTP, registration toggle, how-to doc, email templates) stay in
the `Setting` table; these are strictly scoped to one tenant via user_id.
"""
from models import UserSetting


def uget(db, user_id, key, default=""):
    row = (db.query(UserSetting)
           .filter(UserSetting.user_id == user_id, UserSetting.key == key).first())
    return row.value if row else default


def uset(db, user_id, key, value):
    row = (db.query(UserSetting)
           .filter(UserSetting.user_id == user_id, UserSetting.key == key).first())
    if row:
        if row.value != value:
            row.value = value
    else:
        db.add(UserSetting(user_id=user_id, key=key, value=str(value)))
        db.flush()        # visible to later reads within the same uncommitted tick


def unum(db, user_id, key):
    try:
        v = uget(db, user_id, key, "")
        return float(v) if v not in ("", None) else 0.0
    except Exception:
        return 0.0
