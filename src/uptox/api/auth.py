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
TOKEN_URL = "https://api.upstox.com/v2/login/authorization/token"
LOGIN_URL = "https://api.upstox.com/v2/login"
OTP_URL = "https://api.upstox.com/v2/login/mpin"


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


# ---------- headless TOTP flow ----------

def _extract_auth_code(redirect_url: str) -> str:
    params = parse_qs(urlparse(redirect_url).query)
    codes = params.get("code")
    if not codes:
        raise ValueError(f"No auth code in redirect: {redirect_url}")
    return codes[0]


def _headless_totp_flow() -> str:
    auth_url = (
        f"{AUTH_DIALOG_URL}?response_type=code"
        f"&client_id={settings.api_key}"
        f"&redirect_uri={settings.redirect_url}"
    )

    with httpx.Client(follow_redirects=False, timeout=30) as client:
        resp = client.get(auth_url)
        cookies = dict(resp.cookies)

        resp = client.post(
            LOGIN_URL,
            data={"mobile_num": settings.mobile, "client_id": settings.mobile, "password": settings.password},
            cookies=cookies,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        cookies.update(dict(resp.cookies))

        totp = pyotp.TOTP(settings.totp_secret).now()
        resp = client.post(
            OTP_URL,
            data={"otp": totp},
            cookies=cookies,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        location = resp.headers.get("location", "")
        if not location:
            raise RuntimeError(f"Unexpected response after TOTP: {resp.status_code} {resp.text[:300]}")

        return _extract_auth_code(location)


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
