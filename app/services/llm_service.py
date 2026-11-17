"""Thin HTTP clients for Gemini, OpenAI(-compatible) and Anthropic. No SDKs, just httpx."""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass

import httpx

from app.config import DEFAULT_MODELS, Settings

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
MAX_OUTPUT_TOKENS = 8192


class LLMError(Exception):
    """Any problem talking to the LLM provider. `status_code` is what our API returns."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 502,
        retryable: bool = False,
        upstream_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.retryable = retryable  # True -> worth trying the fallback model
        self.upstream_status = upstream_status  # HTTP status returned by the provider, if any


class LLMNotConfigured(LLMError):
    def __init__(self, message: str) -> None:
        super().__init__(message, status_code=503, retryable=False)


@dataclass
class LLMResult:
    text: str
    provider: str
    model: str
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: int


def ensure_configured(settings: Settings) -> None:
    if settings.llm_provider not in DEFAULT_MODELS:
        raise LLMNotConfigured(
            f"Unsupported LLM_PROVIDER '{settings.llm_provider}'. Use one of: {', '.join(DEFAULT_MODELS)}."
        )
    if not settings.api_key:
        raise LLMNotConfigured(
            f"The LLM is not configured: set {settings.api_key_env_name} in your environment (.env)."
        )


def parse_json_text(text: str) -> dict:
    """Extract a JSON object from model output (tolerates ``` fences and stray prose)."""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip(), flags=re.IGNORECASE)
    candidates = [cleaned]
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end > start:
        candidates.append(cleaned[start : end + 1])
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    raise ValueError("The LLM did not return a JSON object.")


def _check(response: httpx.Response, provider: str) -> None:
    if response.is_success:
        return
    code = response.status_code
    if code in (401, 403):
        raise LLMError(f"{provider} rejected the API key (HTTP {code}). Check the key and its permissions.")
    if code == 429:
        raise LLMError(f"{provider} rate limit reached (HTTP 429). Try again shortly.", status_code=503, retryable=True)
    if code == 404:
        raise LLMError(f"Model not found on {provider} (HTTP 404). Check LLM_MODEL.", retryable=True)
    if code >= 500:
        raise LLMError(
            f"{provider} is temporarily unavailable (HTTP {code}).", retryable=True, upstream_status=code
        )
    detail = ""
    try:
        detail = str(response.json().get("error", {}).get("message", ""))[:200]
    except (ValueError, AttributeError):
        pass
    raise LLMError(f"{provider} rejected the request (HTTP {code}). {detail}".strip())


def _json_body(response: httpx.Response, provider: str) -> dict:
    try:
        data = response.json()
    except ValueError as exc:
        raise LLMError(f"{provider} returned a non-JSON response.", retryable=True) from exc
    if not isinstance(data, dict):
        raise LLMError(f"{provider} returned an unexpected response.", retryable=True)
    return data


async def _call_gemini(client, system, user, model, key, settings):
    payload = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {"responseMimeType": "application/json", "maxOutputTokens": MAX_OUTPUT_TOKENS},
    }
    response = await client.post(GEMINI_URL.format(model=model), json=payload, headers={"x-goog-api-key": key})
    _check(response, "Gemini")
    data = _json_body(response, "Gemini")
    candidates = data.get("candidates") or []
    if not candidates:
        raise LLMError("Gemini returned no candidates (the prompt may have been blocked).", retryable=True)
    parts = (candidates[0].get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
    usage = data.get("usageMetadata") or {}
    out = (usage.get("candidatesTokenCount") or 0) + (usage.get("thoughtsTokenCount") or 0)
    return text, usage.get("promptTokenCount"), out or None


async def _call_openai(client, system, user, model, key, settings):
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "response_format": {"type": "json_object"},
    }
    response = await client.post(
        f"{settings.openai_base_url}/chat/completions",
        json=payload,
        headers={"Authorization": f"Bearer {key}"},
    )
    _check(response, "OpenAI")
    data = _json_body(response, "OpenAI")
    choices = data.get("choices") or []
    if not choices:
        raise LLMError("OpenAI returned no choices.", retryable=True)
    text = (choices[0].get("message") or {}).get("content") or ""
    usage = data.get("usage") or {}
    return text, usage.get("prompt_tokens"), usage.get("completion_tokens")


async def _call_anthropic(client, system, user, model, key, settings):
    payload = {
        "model": model,
        "max_tokens": MAX_OUTPUT_TOKENS,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    response = await client.post(
        ANTHROPIC_URL,
        json=payload,
        headers={"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION},
    )
    _check(response, "Anthropic")
    data = _json_body(response, "Anthropic")
    text = "".join(b.get("text", "") for b in data.get("content") or [] if b.get("type") == "text")
    usage = data.get("usage") or {}
    return text, usage.get("input_tokens"), usage.get("output_tokens")


_CALLERS = {"gemini": _call_gemini, "openai": _call_openai, "anthropic": _call_anthropic}


async def generate_text(
    system: str,
    user: str,
    settings: Settings,
    *,
    model: str,
    client: httpx.AsyncClient | None = None,
) -> LLMResult:
    """Call the configured provider once and return the raw text plus token usage."""
    ensure_configured(settings)
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=settings.llm_timeout)
    started = time.perf_counter()
    try:
        text, tokens_in, tokens_out = await _CALLERS[settings.llm_provider](
            client, system, user, model, settings.api_key, settings
        )
    except httpx.TimeoutException as exc:
        raise LLMError("The LLM provider timed out.", status_code=504, retryable=True) from exc
    except httpx.HTTPError as exc:
        raise LLMError("Could not reach the LLM provider.", retryable=True) from exc
    finally:
        if owns_client:
            await client.aclose()
    return LLMResult(
        text=text,
        provider=settings.llm_provider,
        model=model,
        input_tokens=tokens_in,
        output_tokens=tokens_out,
        latency_ms=int((time.perf_counter() - started) * 1000),
    )
