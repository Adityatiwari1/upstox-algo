"""Upstox login — two modes:
  - TOTP_SECRET set: fully headless (AWS/cron)
  - TOTP_SECRET not set: browser OAuth + local callback server (local dev)
"""
import pyotp
import httpx
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from uptox.config.settings import settings

AUTH_DIALOG_URL = "https://api.upstox.com/v2/login/authorization/dialog"
TOKEN_URL       = "https://api.upstox.com/v2/login/authorization/token"


# ---------- browser OAuth flow ----------

class _CallbackHandler(BaseHTTPRequestHandler):
    auth_code: str | None = None

    def do_GET(self):
        params = parse_qs(urlparse(self.path).query)
        codes = params.get("code")
        if codes:
            _CallbackHandler.auth_code = codes[0]
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"<h2>Login successful. You can close this tab.</h2>")
        else:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"<h2>No auth code received.</h2>")

    def log_message(self, *args):
        pass  # suppress request logs


def _browser_oauth_flow() -> str:
    parsed = urlparse(settings.redirect_url)
    port = parsed.port or 8000

    server = HTTPServer(("0.0.0.0", port), _CallbackHandler)
    server.timeout = 120  # wait up to 2 min for user to login

    auth_url = (
        f"{AUTH_DIALOG_URL}?response_type=code"
        f"&client_id={settings.api_key}"
        f"&redirect_uri={settings.redirect_url}"
    )

    print(f"\nOpening browser for Upstox login...\nIf browser doesn't open, visit:\n{auth_url}\n")
    webbrowser.open(auth_url)

    # handle one request then stop
    server.handle_request()

    if not _CallbackHandler.auth_code:
        raise RuntimeError("Auth code not received within timeout.")
    return _CallbackHandler.auth_code


# ---------- headless Playwright TOTP flow ----------

def _headless_totp_flow() -> str:
    """
    Playwright fills mobile + TOTP (+ PIN if shown) in headless Chromium.
    Local HTTP server captures the auth code from Upstox's redirect.
    """
    from playwright.sync_api import sync_playwright
    import threading

    parsed = urlparse(settings.redirect_url)
    port   = parsed.port or 8000

    # Start callback server in background thread
    _CallbackHandler.auth_code = None
    server = HTTPServer(("0.0.0.0", port), _CallbackHandler)
    server.timeout = 90
    t = threading.Thread(target=server.handle_request, daemon=True)
    t.start()

    auth_url = (
        f"{AUTH_DIALOG_URL}?response_type=code"
        f"&client_id={settings.api_key}"
        f"&redirect_uri={settings.redirect_url}"
    )

    print("Headless login: opening Upstox login page...")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page    = browser.new_page()

        try:
            page.goto(auth_url, wait_until="networkidle")

            # ── Step 1: mobile ────────────────────────────────────────────
            page.wait_for_selector("input#mobileNum", timeout=15_000)
            page.fill("input#mobileNum", settings.mobile)
            page.keyboard.press("Enter")
            page.wait_for_timeout(3_000)

            # ── Step 2: TOTP ──────────────────────────────────────────────
            page.wait_for_selector("input#otpNum", timeout=15_000)
            totp = pyotp.TOTP(settings.totp_secret).now()
            page.fill("input#otpNum", totp)
            page.get_by_role("button", name="Continue").click()
            page.wait_for_timeout(3_000)

            # ── Step 3: PIN (shown for some accounts) ─────────────────────
            try:
                page.wait_for_selector("input#pinCode", timeout=5_000)
                page.fill("input#pinCode", settings.password)
                page.wait_for_timeout(500)
                page.get_by_role("button", name="Continue").click()
                page.wait_for_timeout(3_000)
            except Exception:
                pass

            # ── Wait for redirect ──────────────────────────────────────────
            print(f"Headless login: waiting for callback on port {port}...")
            try:
                page.wait_for_url(f"**{parsed.path}**", timeout=15_000)
            except Exception:
                pass  # server may have already captured it

        except Exception as exc:
            print(f"Headless login error: {exc}")

        t.join(timeout=30)
        browser.close()

    if not _CallbackHandler.auth_code:
        raise RuntimeError(
            "Headless login failed: auth code not captured. "
            "Check MOBILE / TOTP_SECRET / PASSWORD in .env. "
            "Screenshots saved to logs/debug_step*.png"
        )

    print("Headless login: success.")
    return _CallbackHandler.auth_code


# ---------- token exchange ----------

def _exchange_code(auth_code: str) -> str:
    resp = httpx.post(
        TOKEN_URL,
        data={
            "code": auth_code,
            "client_id": settings.api_key,
            "client_secret": settings.api_secret,
            "redirect_uri": settings.redirect_url,
            "grant_type": "authorization_code",
        },
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Token exchange {resp.status_code}: {resp.text}")
    data = resp.json()
    token = data.get("access_token")
    if not token:
        raise RuntimeError(f"Token exchange failed: {data}")
    return token


def fetch_access_token() -> str:
    if settings.totp_secret:
        auth_code = _headless_totp_flow()
    else:
        auth_code = _browser_oauth_flow()
    return _exchange_code(auth_code)
