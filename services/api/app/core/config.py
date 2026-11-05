from functools import lru_cache
import math
from typing import Literal

from pydantic import AnyHttpUrl, PostgresDsn, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


_MAX_PROVIDER_CONCURRENCY = 64


def _coerce_int(
    value: object,
    *,
    name: str,
    minimum: int,
    maximum: int | None = None,
) -> int:
    """Coerce a settings value to a bounded integer.

    Environment variables arrive as strings, so numeric strings are
    coerced. Booleans, fractions, NaN, infinity, non-numeric strings, and
    out-of-range values are rejected with a message naming the setting.
    """
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer, got a boolean")

    if isinstance(value, int):
        numeric = value
    elif isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            raise ValueError(f"{name} must be a whole number")
        numeric = int(value)
    elif isinstance(value, str):
        text = value.strip()

        try:
            numeric = int(text)
        except ValueError:
            raise ValueError(
                f"{name} must be an integer, got {text!r}"
            ) from None
    else:
        raise ValueError(f"{name} must be an integer, got {value!r}")

    if numeric < minimum:
        raise ValueError(f"{name} must be at least {minimum}")

    if maximum is not None and numeric > maximum:
        raise ValueError(f"{name} must be at most {maximum}")

    return numeric


class Settings(BaseSettings):
    app_name: str = "ShopPilot AI API"
    environment: str = "development"

    # Phase 6.8: "text" keeps pre-6.8 logging byte-identical; "json"
    # renders one JSON object per record (see app.core.observability).
    log_format: Literal["text", "json"] = "text"

    database_url: PostgresDsn

    supabase_url: AnyHttpUrl
    supabase_service_role_key: SecretStr

    gemini_api_key: SecretStr | None = None


    # Default: Gemini 3.5 Flash Lite.
    # Can be overridden using GEMINI_GENERATION_MODEL.
    gemini_generation_model: str = "gemini-3.5-flash-lite"

    # Provider timeouts and the overall RAG pipeline deadline, in seconds.
    # The deadline should exceed the combined provider timeouts.
    gemini_embedding_timeout_seconds: float = 10.0
    gemini_generation_timeout_seconds: float = 30.0
    rag_request_deadline_seconds: float = 60.0

    # Provider concurrency limits, wait times, and retry settings.
    # Embedding calls share one concurrency limit across queries and ingestion.
    # Retry windows limit when another attempt may start; timeouts are not retried.
    gemini_embedding_max_concurrency: int = 4
    gemini_generation_max_concurrency: int = 2
    rag_provider_concurrency_wait_seconds: float = 2.0
    ingestion_provider_concurrency_wait_seconds: float = 20.0
    gemini_retry_max_attempts: int = 2
    gemini_retry_initial_delay_seconds: float = 0.25
    gemini_retry_window_seconds: float = 3.0

    @field_validator(
        "gemini_embedding_timeout_seconds",
        "gemini_generation_timeout_seconds",
        "rag_request_deadline_seconds",
        mode="before",
    )
    @classmethod
    def _validate_timeout_field(cls, value: object, info) -> float:
        # Validate before Pydantic can coerce booleans or invalid values.
        from app.core.provider_timeouts import coerce_timeout_seconds

        return coerce_timeout_seconds(value, name=info.field_name)

    @field_validator(
        "gemini_embedding_max_concurrency",
        "gemini_generation_max_concurrency",
        mode="before",
    )
    @classmethod
    def _validate_concurrency_limit(cls, value: object, info) -> int:
        return _coerce_int(
            value,
            name=info.field_name,
            minimum=1,
            maximum=_MAX_PROVIDER_CONCURRENCY,
        )

    @field_validator("gemini_retry_max_attempts", mode="before")
    @classmethod
    def _validate_retry_attempts(cls, value: object, info) -> int:
        # Keep the configured limit within RetryPolicy's maximum.
        return _coerce_int(value, name=info.field_name, minimum=1, maximum=5)

    @field_validator(
        "rag_provider_concurrency_wait_seconds",
        "ingestion_provider_concurrency_wait_seconds",
        "gemini_retry_initial_delay_seconds",
        "gemini_retry_window_seconds",
        mode="before",
    )
    @classmethod
    def _validate_positive_seconds_field(cls, value: object, info) -> float:
        from app.core.provider_timeouts import coerce_timeout_seconds

        return coerce_timeout_seconds(value, name=info.field_name)

    # Authentication uses Supabase claims; no local JWKS URL is needed.

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