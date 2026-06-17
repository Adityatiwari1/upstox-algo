"""Read/write access token and encrypted credentials to .env."""
import re
from pathlib import Path

ENV_FILE = Path(".env")


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
