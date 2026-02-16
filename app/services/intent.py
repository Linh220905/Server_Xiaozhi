"""Intent detection service cho điều khiển nhạc.

Phân loại:
- music: người dùng muốn mở/phát nhạc
- other: còn lại
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional
import datetime

from app.services.llm import LLMService
from app.prompt_store import INTENT_PROMPT

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class IntentResult:
    intent: str
    song_name: str
    alarm_time: Optional[str] = None
    alarm_message: Optional[str] = None


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
            # Check for alarm keywords
            alarm_triggers = ("báo thức", "đặt báo thức", "hẹn giờ", "báo", "báo cho tôi")
            if any(w in lowered for w in alarm_triggers):
                # Try extract time with simple regexes
                # Patterns: HH:MM, H:MM, HhMM (8h30), Hh (8h), H AM/PM, e.g. '8h', '8:30', '8 am'

                time_patterns = [
                    r"(\d{1,2}:\d{2})\s*(am|pm)?",
                    r"(\d{1,2})\s*(am|pm)",
                    r"(\d{1,2})h(?:ố?i|ờ)?\s*(\d{1,2})?",
                    r"(\d{1,2})\s*giờ\s*(\d{1,2})?",
                ]

                found = None
                for pat in time_patterns:
                    m = re.search(pat, lowered)
                    if m:
                        found = m
                        break

                def normalize(m: re.Match | None) -> Optional[str]:
                    if not m:
                        return None
                    g1 = m.group(1)
                    g2 = m.group(2) if m.lastindex and m.lastindex >= 2 else None
                    try:
                        # Case HH:MM
                        if ":" in g1:
                            hh, mm = g1.split(":")
                            hh_i = int(hh) % 24
                            mm_i = int(mm) % 60
                            if g2 and g2.lower() in ("pm",):
                                if hh_i < 12:
                                    hh_i += 12
                            if g2 and g2.lower() in ("am",) and hh_i == 12:
                                hh_i = 0
                            return f"{hh_i:02d}:{mm_i:02d}"
                        # Case H AM/PM
                        if g2 and g2.lower() in ("am", "pm"):
                            hh_i = int(g1) % 12
                            if g2.lower() == "pm":
                                hh_i = (hh_i % 12) + 12
                            return f"{hh_i:02d}:00"
                        # Case '8h30' or '8h' or '8 giờ 30'
                        if g2 is None:
                            # maybe pattern captured only g1
                            hh = int(g1) % 24
                            return f"{hh:02d}:00"
                        # Case groups like (\d{1,2})h(\d{1,2})
                        hh = int(g1) % 24
                        mm = int(g2) if g2 and g2.isdigit() else 0
                        return f"{hh:02d}:{mm:02d}"
                    except Exception:
                        return None

                time_str = normalize(found)
                # Try also match words like 'sáng'/'chiều' to set AM/PM if no explicit
                if not time_str:
                    if "sáng" in lowered:
                        m = re.search(r"(\d{1,2})", lowered)
                        if m:
                            hh = int(m.group(1)) % 24
                            if hh == 12:
                                hh = 0
                            time_str = f"{hh:02d}:00"
                    elif "chiều" in lowered or "tối" in lowered:
                        m = re.search(r"(\d{1,2})", lowered)
                        if m:
                            hh = int(m.group(1)) % 12 + 12
                            time_str = f"{hh:02d}:00"

                message = re.sub(r"\b(đặt\s+báo\s+thức|báo\s+thức|hẹn\s+giờ|báo|báo\s+cho\s+tôi)\b", " ", lowered, flags=re.IGNORECASE)
                message = re.sub(r"\b(sáng|chiều|tối)\b", " ", message, flags=re.IGNORECASE)
                message = re.sub(r"\b(am|pm)\b", " ", message, flags=re.IGNORECASE)
                message = re.sub(r"\d{1,2}(:\d{2})?h?\b", " ", message)
                message = re.sub(r"\s+", " ", message).strip(" ,.!?\n\t")

                return IntentResult(intent="alarm", song_name="", alarm_time=time_str, alarm_message=message or "Báo thức")

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
