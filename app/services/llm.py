"""LLM (Large Language Model) service — với fallback nhiều provider.
"""

import json
from app.server_logging import get_logger
import re
from typing import Any, AsyncGenerator

import openai

from app.config import LLMConfig, LLMProviderConfig

logger = get_logger(__name__)


class LLMService:
    """Chat với LLM — fallback qua nhiều provider."""

    def __init__(self, cfg: LLMConfig):
        self._providers = cfg.providers
        self._max_tokens = cfg.max_tokens
        self._temperature = cfg.temperature
        self._system_prompt = cfg.system_prompt

        # Log providers
        names = [f"{p.name}({p.model})" for p in self._providers]
        logger.info(f"LLM providers: {' → '.join(names)}")

    async def chat_stream(
        self, user_text: str, history: list[dict]
    ) -> AsyncGenerator[str, None]:
        """
        Thử từng provider lần lượt. Nếu provider đầu fail → thử tiếp.
        Yield từng chunk text từ provider thành công.
        """
        messages = self._build_messages(user_text, history)
        last_error = None

        for i, provider in enumerate(self._providers):
            try:
                logger.info(f"\033[92m🤖 LLM trying [{provider.name}] {provider.model} @ {provider.base_url}\033[0m")
                client = openai.AsyncOpenAI(
                    api_key=provider.api_key,
                    base_url=provider.base_url,
                    max_retries=0,  # Không retry để fallback ngay lập tức
                )
                stream = await client.chat.completions.create(
                    model=provider.model,
                    messages=messages,
                    stream=True,
                    max_tokens=self._max_tokens,
                    temperature=self._temperature,
                )

                # Đọc chunk đầu tiên để xác nhận provider hoạt động
                first_chunk = None
                async for chunk in stream:
                    delta = chunk.choices[0].delta.content if chunk.choices else None
                    if delta:
                        first_chunk = delta
                        break

                if first_chunk is None:
                    raise RuntimeError("Empty response from LLM")

                # Provider OK → yield tất cả
                logger.info(f"\033[92m🤖 LLM ✅ [{provider.name}] responding\033[0m")
                yield first_chunk
                async for chunk in stream:
                    delta = chunk.choices[0].delta.content if chunk.choices else None
                    if delta:
                        yield delta
                return  # Thành công, không cần fallback

            except Exception as e:
                last_error = e
                remaining = len(self._providers) - i - 1
                logger.warning(f"LLM  [{provider.name}] failed: {e} ({remaining} fallback(s) left)")
                continue

        # Tất cả providers đều fail
        logger.error(f"LLM all {len(self._providers)} providers failed. Last error: {last_error}")
        yield "Xin lỗi, tất cả LLM đều không phản hồi."

    def _build_messages(self, user_text: str, history: list[dict]) -> list[dict]:
        """Ghép system prompt + history + user message."""
        return [
            {"role": "system", "content": self._system_prompt},
            *history,
            {"role": "user", "content": user_text},
        ]

    async def chat_json(
        self,
        user_text: str,
        *,
        system_prompt: str,
        max_tokens: int = 180,
        temperature: float = 0.0,
    ) -> dict[str, Any] | None:
        """Gọi LLM non-stream và parse JSON output, có fallback providers."""
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ]
        last_error = None

        for i, provider in enumerate(self._providers):
            try:
                client = openai.AsyncOpenAI(
                    api_key=provider.api_key,
                    base_url=provider.base_url,
                    max_retries=0,
                )
                try:
                    response = await client.chat.completions.create(
                        model=provider.model,
                        messages=messages,
                        stream=False,
                        max_tokens=max_tokens,
                        temperature=temperature,
                        response_format={"type": "json_object"},
                    )
                except Exception:
                    # Một số provider/model không hỗ trợ response_format json_object.
                    response = await client.chat.completions.create(
                        model=provider.model,
                        messages=messages,
                        stream=False,
                        max_tokens=max_tokens,
                        temperature=temperature,
                    )
                content = (
                    response.choices[0].message.content
                    if response.choices and response.choices[0].message
                    else None
                )
                if not content:
                    raise RuntimeError("Empty JSON response")
                return self._parse_json_content(content)
            except Exception as e:
                last_error = e
                remaining = len(self._providers) - i - 1
                logger.warning(
                    "LLM JSON [%s] failed: %s (%s fallback(s) left)",
                    provider.name,
                    e,
                    remaining,
                )
                continue

        logger.error("LLM JSON all providers failed. Last error: %s", last_error)
        return None

    @staticmethod
    def _parse_json_content(content: str) -> dict[str, Any]:
        """Parse JSON từ output LLM kể cả khi có text thừa hoặc markdown fences."""
        raw = content.strip()

        # Case chuẩn: raw đã là JSON object.
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
            raise ValueError("JSON is not an object")
        except Exception:
            pass

        # Bỏ markdown fence nếu có.
        fenced = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.IGNORECASE | re.DOTALL).strip()
        try:
            parsed = json.loads(fenced)
            if isinstance(parsed, dict):
                return parsed
            raise ValueError("JSON is not an object")
        except Exception:
            pass

        # Tách object đầu tiên trong text.
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1 and end > start:
            snippet = raw[start : end + 1]
            parsed = json.loads(snippet)
            if isinstance(parsed, dict):
                return parsed

        raise ValueError("Cannot parse JSON object from LLM output")
