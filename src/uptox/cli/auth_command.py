"""
Daily auth token refresh — run via cron on AWS.

Usage:
    uptox-auth          # refresh token if today is trading day, then launch TUI
    uptox-auth --force  # skip trading day check
    uptox-auth --no-ui  # refresh token only, skip TUI launch
"""
import sys
import argparse
from datetime import date

from uptox.services.market_calendar import is_trading_day
from uptox.services.token_service import save_access_token
from uptox.api.auth import fetch_access_token
from uptox.config.settings import settings


def main() -> None:
    parser = argparse.ArgumentParser(description="Uptox daily token refresh")
    parser.add_argument("--force", action="store_true", help="Skip trading day check")
    parser.add_argument("--no-ui", action="store_true", help="Skip launching TUI after refresh")
    args = parser.parse_args()

    today = date.today()
    if not args.force and not is_trading_day(today):
        print(f"{today} is not a trading day. Skipping.")
        sys.exit(0)

    print(f"{today} is a trading day. Fetching access token...")
    token = fetch_access_token()
    save_access_token(token)
    settings.access_token = token
    print("Access token saved.")

    if not args.no_ui:
        from uptox.ui.app import UptoxApp
        UptoxApp().run()


if __name__ == "__main__":
    main()
