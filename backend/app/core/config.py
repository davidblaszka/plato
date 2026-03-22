from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://plato:plato@db:5432/plato"
    secret_key: str = "dev-secret-key-change-in-production"
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 60 * 24 * 7  # 7 days
    environment: str = "development"

    # Cloudflare R2 — set these in .env, never commit to git
    r2_endpoint: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""
    r2_bucket: str = "plato-media"
    r2_public_url: str = ""

    # Email / notification services — optional, read from .env
    resend_api_key: str = ""
    email_from: str = ""
    email_verification_enabled: bool = False
    base_url: str = ""

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()
