from functools import lru_cache

from pydantic import AnyHttpUrl, PostgresDsn, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "ShopPilot AI API"
    environment: str = "development"

    database_url: PostgresDsn

    supabase_url: AnyHttpUrl
    supabase_service_role_key: SecretStr

    gemini_api_key: SecretStr | None = None


#   This part is not needed bcoz im using supabase get_claims() authentication approach

    # @property
    # def supabase_jwks_url(self) -> str:
    #     return f"{str(self.supabase_url).rstrip('/')}/auth/v1/.well-known/jwks.json"


    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()