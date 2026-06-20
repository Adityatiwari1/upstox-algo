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


def _token_valid(token: str) -> bool:
    """Ping profile endpoint — returns False on 401/403."""
    try:
        r = httpx.get(
            _PROFILE_URL,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=8,
        )
        return r.status_code == 200
    except Exception as e:
        log.warning("Token validation request failed: %s", e)
        return False


def _ensure_auth() -> None:
    if settings.access_token and _token_valid(settings.access_token):
        log.info("Existing token valid")
        return
    if settings.access_token:
        log.warning("Stored token is expired or invalid — re-authenticating")
    else:
        log.info("No access token found — starting login")
    token = fetch_access_token()
    save_access_token(token)
    settings.access_token = token
    log.info("Login successful")


def main() -> None:
    from uptox.services.market_calendar import is_trading_day, market_status
    if not is_trading_day():
        log.info("Non-trading day — skipping auth and feed. %s", market_status())
        # Still launch UI so user can view trade history / settings
        app = UptoxApp()
        app.run()
        return
    _ensure_auth()
    log.info("Starting Uptox Trading")
    app = UptoxApp()
    app.run()


if __name__ == "__main__":
    main()
