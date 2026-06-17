from uptox.config.settings import settings
from uptox.api.auth import fetch_access_token
from uptox.services.token_service import save_access_token
from uptox.ui.app import UptoxApp


def _ensure_auth() -> None:
    if not settings.access_token:
        print("No access token found. Starting login...")
        token = fetch_access_token()
        save_access_token(token)
        settings.access_token = token
        print("Login successful.")


def main() -> None:
    # _ensure_auth()  # TODO: re-enable after Upstox segments activated
    app = UptoxApp()
    app.run()


if __name__ == "__main__":
    main()
