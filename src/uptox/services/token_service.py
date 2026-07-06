"""Read/write access token and encrypted credentials to .env."""
import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

ENV_FILE = Path(".env")
_PROFILE_URL = "https://api.upstox.com/v2/user/profile"


def is_token_valid(token: str) -> bool | None:
    """
    Ping profile endpoint. Returns True=valid, False=expired (401/403),
    None=network error (caller should assume valid rather than force a
    needless re-login on a transient network blip).
    """
    import httpx
    if not token:
        return False
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


def _set_env_var(key: str, value: str) -> None:
    content = ENV_FILE.read_text(encoding="utf-8") if ENV_FILE.exists() else ""
    pattern = rf"^{key}=.*$"
    new_line = f"{key}={value}"
    if re.search(pattern, content, flags=re.MULTILINE):
        content = re.sub(pattern, new_line, content, flags=re.MULTILINE)
    else:
        content = content.rstrip("\n") + f"\n{new_line}\n"
    ENV_FILE.write_text(content, encoding="utf-8")


def save_access_token(token: str) -> None:
    _set_env_var("ACCESS_TOKEN", token)


def save_encrypted_credentials(mobile: str, password: str, totp_secret: str) -> None:
    _set_env_var("ENCRYPTED_MOBILE", mobile)
    _set_env_var("ENCRYPTED_PASSWORD", password)
    _set_env_var("ENCRYPTED_TOTP_SECRET", totp_secret)
