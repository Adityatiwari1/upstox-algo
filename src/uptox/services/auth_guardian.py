"""
Proactive token health check — runs independently of the market feed's own
reactive re-auth (feed_client._reauth, triggered only on a 401 from the WSS
authorize call).

Why this exists: a long-running process only calls _ensure_auth() once, at
startup (see main.py). If the token expires while the process stays up
across a weekend, nothing re-validates it until the feed itself hits a 401 —
and if that reactive path stalls (e.g. a Playwright/headless-login hiccup,
or the app was sitting in market-closed sleep with no WSS call to trigger
the check at all), the token can stay dead for hours with no self-heal and
no alert. Found 2026-07-06→Monday: the app didn't reconnect on its own and
needed a manual restart. This worker checks token validity on a fixed
interval regardless of feed state, re-authenticates proactively, and pages
the user if it can't recover after repeated tries.
"""
from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)

_CHECK_INTERVAL = 600     # seconds — 10 min; cheap profile-ping, fine at this cadence
_MAX_FAILURES_BEFORE_ALERT = 2


async def run_forever() -> None:
    from uptox.config.settings import settings
    from uptox.services.token_service import is_token_valid, save_access_token
    from uptox.api.auth import fetch_access_token

    consecutive_failures = 0
    alerted = False

    while True:
        await asyncio.sleep(_CHECK_INTERVAL)
        token = settings.access_token
        valid = is_token_valid(token) if token else False

        if valid is True:
            if consecutive_failures:
                log.info("auth_guardian: token healthy again")
            consecutive_failures = 0
            alerted = False
            continue
        if valid is None:
            continue   # network blip — don't force a re-login on a timeout

        log.warning("auth_guardian: token invalid — attempting re-login")
        try:
            loop = asyncio.get_event_loop()
            new_token = await loop.run_in_executor(None, fetch_access_token)
            save_access_token(new_token)
            settings.access_token = new_token
            consecutive_failures = 0
            alerted = False
            log.info("auth_guardian: re-login successful")

            # Re-prime services that fetch once at startup — if the token was
            # already dead when they first ran (e.g. right after a weekend),
            # their cached state (option chain, SMAs, prev-day OHLC) never
            # recovers on its own once the token comes back.
            try:
                from uptox.services import option_chain_service, sma_service, prev_day_service
                from uptox.api.feed_client import DEFAULT_INSTRUMENTS
                await option_chain_service.fetch_and_prime(new_token)
                await asyncio.gather(*(
                    sma_service.fetch_and_prime(key, new_token) for key in DEFAULT_INSTRUMENTS
                ))
                await prev_day_service.fetch_and_prime(new_token)
                log.info("auth_guardian: re-primed option chain / SMAs / prev-day after reauth")
            except Exception as e:
                log.error("auth_guardian: re-prime after reauth failed: %s", e)

        except Exception as e:
            consecutive_failures += 1
            log.error("auth_guardian: re-login attempt failed (%d in a row): %s",
                      consecutive_failures, e)
            if consecutive_failures >= _MAX_FAILURES_BEFORE_ALERT and not alerted:
                from uptox.services.telegram_service import notify_auth_issue
                notify_auth_issue(
                    f"Token expired and {consecutive_failures} re-login attempts failed.\n"
                    f"Last error: {e}"
                )
                alerted = True
