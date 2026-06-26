from uptox.config.logging_config import setup as _setup_logging
_setup_logging()

import logging
import httpx
from uptox.config.settings import settings
from uptox.api.auth import fetch_access_token
from uptox.services.token_service import save_access_token
from uptox.ui.app import UptoxApp

log = logging.getLogger(__name__)

_PROFILE_URL = "https://api.upstox.com/v2/user/profile"


def _token_valid(token: str) -> bool | None:
    """
    Ping profile endpoint.
    Returns True=valid, False=expired (401/403), None=network error (assume valid).
    """
    try:
        r = httpx.get(
            _PROFILE_URL,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=8,
        )
        if r.status_code == 200:
            return True
        if r.status_code in (401, 403):
            return False
        log.warning("Token check returned %s — assuming valid", r.status_code)
        return None
    except httpx.TimeoutException:
        log.warning("Token validation timed out — assuming token still valid")
        return None
    except Exception as e:
        log.warning("Token validation error: %s — assuming valid", e)
        return None


def _ensure_auth() -> None:
    if settings.access_token:
        result = _token_valid(settings.access_token)
        if result is True:
            log.info("Existing token valid")
            return
        if result is None:
            log.info("Token validation skipped (network unreachable) — using stored token")
            return
        log.warning("Stored token expired (401/403) — re-authenticating")
    else:
        log.info("No access token found — starting login")
    token = fetch_access_token()
    save_access_token(token)
    settings.access_token = token
    log.info("Login successful")


def main() -> None:
    from uptox.services.market_calendar import is_trading_day, market_status
    _ensure_auth()
    if not is_trading_day():
        log.info("Non-trading day — skipping feed. %s", market_status())
    else:
        log.info("Starting Uptox Trading")
    app = UptoxApp()
    app.run()


if __name__ == "__main__":
    main()
