"""Unified async LLM service supporting Gemini, OpenAI, and Anthropic with structured JSON validation."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx
from pydantic import ValidationError

from app.config import Settings, estimate_llm_cost_usd
from app.schemas import LLMAnalysisPayload

logger = logging.getLogger(__name__)

TRANSIENT_HTTP_STATUSES = {429, 502, 503, 504}
MAX_TRANSIENT_RETRIES = 2


class LLMConfigurationError(Exception):
    """Raised when required LLM API keys are missing or invalid."""


class LLMProviderError(Exception):
    """Raised when the LLM provider fails or returns invalid structured output."""


@dataclass
class LLMResult:
    """Validated LLM analysis payload paired with operational telemetry."""

    payload: LLMAnalysisPayload
    provider: str
    model: str
    input_tokens: Optional[int]
    output_tokens: Optional[int]
    estimated_cost_usd: Optional[float]
    latency_ms: int


class LLMService:
    """Handles structured JSON generation across Gemini, OpenAI, and Anthropic with automatic fallback."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def generate_analysis(
        self,
        system_prompt: str,
        user_prompt: str,
    ) -> LLMResult:
        """Run structured analysis using primary provider, falling back if configured."""
        providers = self._settings.get_active_providers()
        configured = [p for p in providers if self._settings.get_api_key(p)]
        if not configured:
            raise LLMConfigurationError(
                f"No valid API key configured for LLM provider '{self._settings.llm_provider}'. "
                "Please set GEMINI_API_KEY, OPENAI_API_KEY, or ANTHROPIC_API_KEY in your .env file."
            )

        errors: list[str] = []
        for provider in configured:
            model = self._settings.get_model(provider)
            api_key = self._settings.get_api_key(provider)
            assert api_key is not None

            start_ts = time.perf_counter()
            try:
                raw_text, in_tokens, out_tokens = await self._call_provider(
                    provider=provider,
                    model=model,
                    api_key=api_key,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                )
                payload = self._parse_and_validate(raw_text)
                latency_ms = int((time.perf_counter() - start_ts) * 1000)
                cost = (
                    estimate_llm_cost_usd(model, in_tokens, out_tokens)
                    if in_tokens is not None and out_tokens is not None
                    else None
                )
                return LLMResult(
                    payload=payload,
                    provider=provider,
                    model=model,
                    input_tokens=in_tokens,
                    output_tokens=out_tokens,
                    estimated_cost_usd=cost,
                    latency_ms=latency_ms,
                )
            except (json.JSONDecodeError, ValidationError) as parse_exc:
                logger.warning(
                    "Initial structured JSON validation failed for %s/%s (%s); attempting 1-shot repair.",
                    provider,
                    model,
                    parse_exc,
                )
                try:
                    repair_prompt = (
                        f"{user_prompt}\n\n"
                        "IMPORTANT: Your previous response failed JSON schema validation with this error:\n"
                        f"{str(parse_exc)[:800]}\n"
                        "Return ONLY a single valid JSON object strictly conforming to the required schema."
                    )
                    raw_text, in_tokens, out_tokens = await self._call_provider(
                        provider=provider,
                        model=model,
                        api_key=api_key,
                        system_prompt=system_prompt,
                        user_prompt=repair_prompt,
                    )
                    payload = self._parse_and_validate(raw_text)
                    latency_ms = int((time.perf_counter() - start_ts) * 1000)
                    cost = (
                        estimate_llm_cost_usd(model, in_tokens, out_tokens)
                        if in_tokens is not None and out_tokens is not None
                        else None
                    )
                    return LLMResult(
                        payload=payload,
                        provider=provider,
                        model=model,
                        input_tokens=in_tokens,
                        output_tokens=out_tokens,
                        estimated_cost_usd=cost,
                        latency_ms=latency_ms,
                    )
                except Exception as retry_exc:  # noqa: BLE001
                    errors.append(f"{provider}/{model}: {retry_exc}")
            except Exception as exc:  # noqa: BLE001
                logger.warning("LLM provider %s/%s failed: %s", provider, model, exc)
                errors.append(f"{provider}/{model}: {exc}")

        raise LLMProviderError(
            "All configured LLM providers failed to generate a valid analysis. Details: "
            + " | ".join(errors)
        )

    async def _call_provider(
        self,
        provider: str,
        model: str,
        api_key: str,
        system_prompt: str,
        user_prompt: str,
    ) -> tuple[str, Optional[int], Optional[int]]:
        """Dispatch request to the chosen LLM REST API."""
        if provider == "gemini":
            return await self._call_gemini(model, api_key, system_prompt, user_prompt)
        if provider == "openai":
            return await self._call_openai(model, api_key, system_prompt, user_prompt)
        if provider == "anthropic":
            return await self._call_anthropic(model, api_key, system_prompt, user_prompt)
        raise LLMConfigurationError(f"Unsupported LLM provider: {provider}")

    async def _post_with_retry(
        self,
        endpoint: str,
        headers: dict[str, str],
        body: dict[str, Any],
        provider_label: str,
    ) -> httpx.Response:
        """Send an async POST request with exponential backoff on transient 429/5xx errors."""
        last_response: Optional[httpx.Response] = None
        async with httpx.AsyncClient(timeout=self._settings.llm_timeout_seconds) as client:
            for attempt in range(MAX_TRANSIENT_RETRIES + 1):
                response = await client.post(endpoint, headers=headers, json=body)
                last_response = response
                if response.status_code not in TRANSIENT_HTTP_STATUSES or attempt == MAX_TRANSIENT_RETRIES:
                    return response
                delay = 1.5 * (2**attempt)
                logger.warning(
                    "%s returned transient HTTP %d (attempt %d/%d); retrying in %.1fs.",
                    provider_label,
                    response.status_code,
                    attempt + 1,
                    MAX_TRANSIENT_RETRIES + 1,
                    delay,
                )
                await asyncio.sleep(delay)
        assert last_response is not None
        return last_response

    async def _call_gemini(
        self,
        model: str,
        api_key: str,
        system_prompt: str,
        user_prompt: str,
    ) -> tuple[str, Optional[int], Optional[int]]:
        """Call Google Gemini generateContent REST API with strict JSON MIME type."""
        endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
        }
        body: dict[str, Any] = {
            "systemInstruction": {
                "parts": [{"text": system_prompt}],
            },
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": user_prompt}],
                }
            ],
            "generationConfig": {
                "temperature": 0.2,
                "maxOutputTokens": 8192,
                "responseMimeType": "application/json",
            },
        }

        response = await self._post_with_retry(endpoint, headers, body, "Gemini API")

        if response.status_code >= 400:
            raise LLMProviderError(
                f"Gemini API error (HTTP {response.status_code}): {self._safe_error_excerpt(response.text)}"
            )

        data = response.json()
        candidates = data.get("candidates") or []
        if not candidates:
            raise LLMProviderError("Gemini API returned no candidates.")

        parts = candidates[0].get("content", {}).get("parts") or []
        text = "".join(part.get("text", "") for part in parts if isinstance(part, dict)).strip()
        if not text:
            raise LLMProviderError("Gemini API returned an empty response.")

        usage = data.get("usageMetadata") or {}
        in_tokens = usage.get("promptTokenCount")
        out_tokens = usage.get("candidatesTokenCount")
        return text, in_tokens, out_tokens

    async def _call_openai(
        self,
        model: str,
        api_key: str,
        system_prompt: str,
        user_prompt: str,
    ) -> tuple[str, Optional[int], Optional[int]]:
        """Call OpenAI Chat Completions REST API with JSON object mode."""
        endpoint = "https://api.openai.com/v1/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }
        body: dict[str, Any] = {
            "model": model,
            "temperature": 0.2,
            "max_tokens": 4096,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }

        response = await self._post_with_retry(endpoint, headers, body, "OpenAI API")

        if response.status_code >= 400:
            raise LLMProviderError(
                f"OpenAI API error (HTTP {response.status_code}): {self._safe_error_excerpt(response.text)}"
            )

        data = response.json()
        choices = data.get("choices") or []
        if not choices:
            raise LLMProviderError("OpenAI API returned no choices.")

        text = (choices[0].get("message", {}).get("content") or "").strip()
        if not text:
            raise LLMProviderError("OpenAI API returned an empty message.")

        usage = data.get("usage") or {}
        in_tokens = usage.get("prompt_tokens")
        out_tokens = usage.get("completion_tokens")
        return text, in_tokens, out_tokens

    async def _call_anthropic(
        self,
        model: str,
        api_key: str,
        system_prompt: str,
        user_prompt: str,
    ) -> tuple[str, Optional[int], Optional[int]]:
        """Call Anthropic Messages REST API with assistant JSON prefill."""
        endpoint = "https://api.anthropic.com/v1/messages"
        headers = {
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        }
        body: dict[str, Any] = {
            "model": model,
            "max_tokens": 4096,
            "temperature": 0.2,
            "system": system_prompt,
            "messages": [
                {"role": "user", "content": user_prompt},
                {"role": "assistant", "content": "{"},
            ],
        }

        response = await self._post_with_retry(endpoint, headers, body, "Anthropic API")

        if response.status_code >= 400:
            raise LLMProviderError(
                f"Anthropic API error (HTTP {response.status_code}): {self._safe_error_excerpt(response.text)}"
            )

        data = response.json()
        blocks = data.get("content") or []
        raw_suffix = "".join(
            block.get("text", "") for block in blocks if isinstance(block, dict) and block.get("type") == "text"
        )
        text = "{" + raw_suffix.lstrip("{")
        if not raw_suffix.strip():
            raise LLMProviderError("Anthropic API returned an empty message.")

        usage = data.get("usage") or {}
        in_tokens = usage.get("input_tokens")
        out_tokens = usage.get("output_tokens")
        return text, in_tokens, out_tokens

    @staticmethod
    def _parse_and_validate(raw_text: str) -> LLMAnalysisPayload:
        """Extract JSON from potential markdown fences and validate against Pydantic schema."""
        cleaned = raw_text.strip()
        fence_match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", cleaned, flags=re.DOTALL | re.IGNORECASE)
        if fence_match:
            cleaned = fence_match.group(1).strip()
        elif not cleaned.startswith("{"):
            first_brace = cleaned.find("{")
            last_brace = cleaned.rfind("}")
            if first_brace != -1 and last_brace > first_brace:
                cleaned = cleaned[first_brace : last_brace + 1]

        parsed = json.loads(cleaned)
        return LLMAnalysisPayload.model_validate(parsed)

    @staticmethod
    def _safe_error_excerpt(body: str) -> str:
        """Return a concise error message without leaking secrets."""
        snippet = re.sub(r"\s+", " ", body).strip()
        return snippet[:300]
