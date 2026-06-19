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
    iv_rank_block:    float = 70.0   # block buys if IV rank > this %

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
