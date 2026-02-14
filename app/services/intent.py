"""Intent detection service cho điều khiển nhạc.

Phân loại:
- music: người dùng muốn mở/phát nhạc
- other: còn lại
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from app.services.llm import LLMService
from app.prompt_store import INTENT_PROMPT

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class IntentResult:
    intent: str
    song_name: str


class IntentDetectorService:
    """Dùng 1 LLM riêng để detect intent phát nhạc."""

    def __init__(self, llm: LLMService):
        self._llm = llm

    def detect_fast(self, user_text: str) -> IntentResult:
        """Detect nhanh bằng rule-based."""
        text = (user_text or "").strip()
        lowered = text.lower()

        trigger_words = (
            "mở",
            "mơ",
            "mỡ",
            "phát",
            "bật",
            "nghe",
            "play",
        )
        music_words = (
            "nhạc",
            "bài",
            "bài hát",
            "ca sĩ",
            "playlist",
            "music",
        )

        has_trigger = any(w in lowered for w in trigger_words)
        has_music = any(w in lowered for w in music_words)
        if not (has_trigger and has_music):
            return IntentResult(intent="other", song_name="")

        # Chuẩn hóa câu lệnh thành tên bài hát truy vấn.
        cleaned = re.sub(
            r"\b(mở|mơ|phát|bật|nghe|cho\s+tôi|giúp\s+tôi|play|bài\s+hát|bài|nhạc|music)\b",
            " ",
            lowered,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,.!?\n\t")
        song_name = cleaned if cleaned else "nhạc việt"
        return IntentResult(intent="music", song_name=song_name)

    async def detect(self, user_text: str) -> IntentResult:
        """Trả về intent và tên bài hát (nếu có)."""
        prompt = INTENT_PROMPT

        data = await self._llm.chat_json(
            user_text,
            system_prompt=prompt,
            max_tokens=120,
            temperature=0.0,
        )

        if not isinstance(data, dict):
            return IntentResult(intent="other", song_name="")

        raw_intent = str(data.get("intent", "other")).strip().lower()
        intent = "music" if raw_intent == "music" else "other"
        song_name = str(data.get("song_name", "")).strip()

        if intent == "music" and not song_name:
            song_name = "nhạc việt"

        logger.info("Intent detect -> intent=%s, song_name=%s", intent, song_name)
        return IntentResult(intent=intent, song_name=song_name)
