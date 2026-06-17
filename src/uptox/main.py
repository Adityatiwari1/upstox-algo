from uptox.config.logging_config import setup as _setup_logging
_setup_logging()

import logging
from uptox.config.settings import settings
from uptox.api.auth import fetch_access_token
from uptox.services.token_service import save_access_token
from uptox.ui.app import UptoxApp

log = logging.getLogger(__name__)


def _ensure_auth() -> None:
    if not settings.access_token:
        log.info("No access token found — starting login")
        token = fetch_access_token()
        save_access_token(token)
        settings.access_token = token
        log.info("Login successful")


def main() -> None:
    _ensure_auth()
    log.info("Starting Uptox Trading")
    app = UptoxApp()
    app.run()


if __name__ == "__main__":
    main()
