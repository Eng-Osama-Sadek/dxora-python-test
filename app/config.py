"""Application settings, read from environment variables (and an optional .env file)."""
from __future__ import annotations

import os
from dataclasses import dataclass

try:  # python-dotenv is optional at runtime; real env vars always work.
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass

# provider -> (default model, default fallback model)
DEFAULT_MODELS: dict[str, tuple[str, str]] = {
    "gemini": ("gemini-3.5-flash-lite", "gemini-3.8-flash"),
    "openai": ("gpt-5.4-nano", "gpt-5.4-mini"),
    "anthropic": ("claude-haiku-5-5", "claude-sonnet-5-5"),
}
KEY_ENV_NAMES = {
    "gemini": "GEMINI_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
}


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _number(name: str, default: float) -> float:
    try:
        return float(_env(name) or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    llm_provider: str = "gemini"
    llm_model: str = ""
    llm_fallback_model: str = ""
    gemini_api_key: str = ""
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    anthropic_api_key: str = ""
    fetch_timeout: float = 15.0
    llm_timeout: float = 90.0
    max_html_bytes: int = 2_000_000
    max_content_chars: int = 12_000
    cache_ttl_seconds: int = 3600

    @property
    def api_key(self) -> str:
        return {
            "gemini": self.gemini_api_key,
            "openai": self.openai_api_key,
            "anthropic": self.anthropic_api_key,
        }.get(self.llm_provider, "")

    @property
    def api_key_env_name(self) -> str:
        return KEY_ENV_NAMES.get(self.llm_provider, "API key")


def get_settings() -> Settings:
    """Read the environment on every call so changes (and tests) are picked up."""
    provider = (_env("LLM_PROVIDER") or "gemini").lower()
    default_model, default_fallback = DEFAULT_MODELS.get(provider, ("", ""))
    model = _env("LLM_MODEL") or default_model
    fallback = _env("LLM_FALLBACK_MODEL") or default_fallback
    if fallback.lower() in {"none", "off", "false"} or fallback == model:
        fallback = ""
    return Settings(
        llm_provider=provider,
        llm_model=model,
        llm_fallback_model=fallback,
        gemini_api_key=_env("GEMINI_API_KEY"),
        openai_api_key=_env("OPENAI_API_KEY"),
        openai_base_url=(_env("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/"),
        anthropic_api_key=_env("ANTHROPIC_API_KEY"),
        fetch_timeout=_number("FETCH_TIMEOUT", 15.0),
        llm_timeout=_number("LLM_TIMEOUT", 90.0),
        max_content_chars=int(_number("MAX_CONTENT_CHARS", 12_000)),
        cache_ttl_seconds=int(_number("CACHE_TTL_SECONDS", 3600)),
    )
