from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    api_key: str = ""
    api_secret: str = ""
    redirect_url: str = "http://127.0.0.1:8000/callback"

    mobile: str = ""
    password: str = ""
    totp_secret: str = ""

    access_token: str = ""

    telegram_token:   str   = ""
    telegram_chat_id: str   = ""

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

    def reload(self) -> None:
        """Re-read .env into this same instance — no process restart needed.
        Feed/trade state untouched; only config fields (tokens, thresholds) refresh."""
        fresh = Settings()
        for field in self.model_fields:
            setattr(self, field, getattr(fresh, field))


settings = Settings()
