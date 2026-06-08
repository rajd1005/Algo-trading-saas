"""
Dhan "Login with Dhan" (App / Consent) authentication.

Because SEBI requires API tokens to expire within 24 hours, we cannot store a
permanent token. Instead we use Dhan's app-consent login so the user signs in on
Dhan's own page and we receive the access token automatically:

  1. generate_consent()  -> consentId  (using your App ID + App Secret)
  2. send the user to login_url(consentId) -> they log in on Dhan
  3. Dhan redirects back with a tokenId -> consume_consent() -> access token

We can also renew_token() to extend a live token by another 24h (auto-renew),
so in practice you log in once and the system keeps the session alive.

Docs: https://dhanhq.co/docs/v2/authentication/
"""
import requests

import config

AUTH_BASE = "https://auth.dhan.co"


def generate_consent(app_id: str, app_secret: str, client_id: str) -> str:
    """Step 1 — start a login consent. Returns a consentAppId."""
    url = f"{AUTH_BASE}/app/generate-consent?client_id={client_id}"
    headers = {"app_id": app_id, "app_secret": app_secret}
    r = requests.post(url, headers=headers, timeout=10)
    r.raise_for_status()
    d = r.json()
    return d.get("consentAppId") or d.get("consentId") or ""


def login_url(consent_app_id: str) -> str:
    """Step 2 — the Dhan page the user logs in on."""
    return f"{AUTH_BASE}/login/consentApp-login?consentAppId={consent_app_id}"


def consume_consent(app_id: str, app_secret: str, token_id: str):
    """Step 3 — exchange the tokenId (from the redirect) for an access token.
    Returns (access_token, client_id, raw_response)."""
    url = f"{AUTH_BASE}/app/consumeApp-consent?tokenId={token_id}"
    headers = {"app_id": app_id, "app_secret": app_secret}
    r = requests.post(url, headers=headers, timeout=10)
    r.raise_for_status()
    d = r.json()
    return d.get("accessToken", ""), d.get("dhanClientId", ""), d


def renew_token(access_token: str, client_id: str) -> str:
    """Extend a live token by another 24h. Returns the new access token (or '')."""
    url = f"{config.DHAN_API_BASE}/RenewToken"
    headers = {"access-token": access_token, "dhanClientId": client_id}
    try:
        r = requests.get(url, headers=headers, timeout=10)
        r.raise_for_status()
        return r.json().get("accessToken", "")
    except Exception:
        return ""
