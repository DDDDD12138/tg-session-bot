import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    openai_api_key: str
    openai_base_url: str | None
    openai_model: str
    system_prompt: str
    telegram_streaming_enabled: bool


def _require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _read_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return default


def load_settings() -> Settings:
    return Settings(
        telegram_bot_token=_require_env("TELEGRAM_BOT_TOKEN"),
        openai_api_key=os.getenv("OPENAI_API_KEY", os.getenv("API_KEY", "")).strip() or _require_env("OPENAI_API_KEY"),
        openai_base_url=os.getenv("OPENAI_BASE_URL", os.getenv("BASE_URL", "")).strip() or None,
        openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip(),
        system_prompt=os.getenv("SYSTEM_PROMPT", "你是一个简洁、专业的中文助手。"),
        telegram_streaming_enabled=_read_bool("TELEGRAM_STREAMING_ENABLED", True),
    )
