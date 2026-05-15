"""Intent detection service cho điều khiển nhanh.

Phân loại:
- music: người dùng muốn mở/phát nhạc
- alarm/set_volume/set_brightness/reboot/assignment: lệnh nhanh
- flashcard_vocab: luyện từ vựng bằng flash card vật lí
- learning_conversation: luyện hội thoại theo chủ đề
- other: còn lại
"""

from __future__ import annotations

from app.server_logging import get_logger
import re
import unicodedata
from dataclasses import dataclass
from typing import Optional
import datetime

from app.services.llm import LLMService
from app.prompt_store import INTENT_PROMPT, LEARNING_INTENT_PROMPT
from app.services.learning_content import find_topic

logger = get_logger(__name__)


def _normalize_vi_text(text: str) -> str:
    lowered = (text or "").strip().lower()
    normalized = unicodedata.normalize("NFD", lowered)
    without_marks = "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")
    without_marks = without_marks.replace("đ", "d")
    return re.sub(r"\s+", " ", without_marks).strip()


@dataclass(slots=True)
class IntentResult:
    intent: str
    song_name: Optional[str] = None
    alarm_time: Optional[str] = None
    alarm_message: Optional[str] = None
    volume: Optional[int] = None
    brightness: Optional[int] = None
    learning_mode: Optional[str] = None
    topic_id: Optional[str] = None
    assignment_requested: Optional[bool] = None


class IntentDetectorService:
    """Dùng 1 LLM riêng để detect intent điều khiển nhanh."""

    def __init__(self, llm: LLMService):
        self._llm = llm

    def detect_fast(self, user_text: str) -> IntentResult:
        """Detect nhanh bằng rule-based."""
        text = (user_text or "").strip()
        lowered = text.lower()
        normalized = _normalize_vi_text(text)

        # 1. Volume/Brightness intent
        # Regex: tăng/giảm âm lượng/độ sáng (lên|xuống)? (xx%)?
        volume_patterns = [
            r"(tăng|giảm)\s*(âm\s*lượng|volume)\s*(lên|xuống)?\s*(\d{1,3})?\s*%?",
        ]
        brightness_patterns = [
            r"(tăng|giảm)\s*(độ\s*sáng|brightness)\s*(lên|xuống)?\s*(\d{1,3})?\s*%?",
        ]

        for pat in volume_patterns:
            m = re.search(pat, lowered)
            if m:
                action = m.group(1)
                value = m.group(4)
                try:
                    volume = int(value) if value else (100 if action == "tăng" else 0)
                    volume = max(0, min(100, volume))
                except Exception:
                    volume = 100 if action == "tăng" else 0
                return IntentResult(intent="set_volume", volume=volume)

        for pat in brightness_patterns:
            m = re.search(pat, lowered)
            if m:
                action = m.group(1)
                value = m.group(4)
                try:
                    brightness = int(value) if value else (100 if action == "tăng" else 0)
                    brightness = max(0, min(100, brightness))
                except Exception:
                    brightness = 100 if action == "tăng" else 0
                return IntentResult(intent="set_brightness", brightness=brightness)

        # 2. Physical flash card vocabulary practice.
        if any(
            k in normalized
            for k in (
                "hoc tu vung",
                "tu vung",
                "hoc tu moi",
                "flash card",
                "flashcard",
                "the tu vung",
                "the hoc tu",
                "on tu vung",
                "luyen tu vung",
            )
        ):
            return IntentResult(intent="flashcard_vocab", learning_mode="flashcard_vocab")

        # 3. Learning conversation intent.
        # Luồng học từ vựng theo chủ đề vẫn còn trong learning_content/pipeline,
        # nhưng không còn được kích hoạt bằng intent giọng nói ở đây.
        if any(k in normalized for k in ("hoi thoai", "luyen noi", "giao tiep")):
            topic = find_topic("conversation", normalized)
            return IntentResult(
                intent="learning_conversation",
                learning_mode="conversation",
                topic_id=str(topic.get("id")) if topic else None,
            )

        if any(
            k in lowered
            for k in (
                "bài tập",
                "bai tap",
                "được giao",
                "duoc giao",
                "làm bài",
                "lam bai",
                "bai hoc me giao",
            )
        ):
            return IntentResult(intent="assignment", assignment_requested=True)

        topic = find_topic("conversation", normalized)
        if topic:
            return IntentResult(
                intent="learning_topic",
                learning_mode="conversation",
                topic_id=str(topic.get("id")),
            )

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
            # 3. Alarm intent
            alarm_triggers = ("báo thức", "đặt báo thức", "hẹn giờ", "báo", "báo cho tôi")
            if any(w in lowered for w in alarm_triggers):
                # Try extract time with simple regexes
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
        """Trả về intent và tham số động."""
        prompt = INTENT_PROMPT
        data = await self._llm.chat_json(
            user_text,
            system_prompt=prompt,
            max_tokens=120,
            temperature=0.0,
        )
        if not isinstance(data, dict):
            return IntentResult(intent="other")
        intent = str(data.get("intent", "other")).strip().lower()
        # Map các intent và tham số
        if intent == "music":
            song_name = str(data.get("song_name", "")).strip() or "nhạc việt"
            return IntentResult(intent="music", song_name=song_name)
        if intent == "alarm":
            alarm_time = str(data.get("alarm_time", "")).strip()
            alarm_message = str(data.get("alarm_message", "")).strip() or "Báo thức"
            return IntentResult(intent="alarm", alarm_time=alarm_time, alarm_message=alarm_message)
        if intent == "set_volume":
            try:
                volume = int(data.get("volume", -1))
            except Exception:
                volume = -1
            return IntentResult(intent="set_volume", volume=volume)
        if intent == "set_brightness":
            try:
                brightness = int(data.get("brightness", -1))
            except Exception:
                brightness = -1
            return IntentResult(intent="set_brightness", brightness=brightness)
        if intent == "reboot":
            return IntentResult(intent="reboot")
        if intent == "flashcard_vocab":
            return IntentResult(intent="flashcard_vocab", learning_mode="flashcard_vocab")
        if intent == "learning_vocab":
            return IntentResult(intent="other")
        if intent == "learning_conversation":
            topic_id = str(data.get("topic_id", "")).strip() or None
            return IntentResult(intent="learning_conversation", learning_mode="conversation", topic_id=topic_id)
        if intent == "learning_topic":
            learning_mode = str(data.get("learning_mode", "")).strip() or None
            topic_id = str(data.get("topic_id", "")).strip() or None
            if learning_mode != "conversation":
                return IntentResult(intent="other")
            return IntentResult(intent="learning_topic", learning_mode=learning_mode, topic_id=topic_id)
        if intent == "assignment":
            return IntentResult(intent="assignment", assignment_requested=True)
        return IntentResult(intent="other")

    async def detect_learning_intent(self, user_text: str) -> IntentResult:
        """Detect learning intent bằng LLM chuyên biệt để tăng độ bền với STT lỗi nhẹ."""
        data = await self._llm.chat_json(
            user_text,
            system_prompt=LEARNING_INTENT_PROMPT,
            max_tokens=160,
            temperature=0.0,
        )
        if not isinstance(data, dict):
            return IntentResult(intent="other")

        intent = str(data.get("intent", "other")).strip().lower()
        learning_mode = str(data.get("learning_mode", "")).strip().lower() or None
        topic_id = str(data.get("topic_id", "")).strip().lower() or None
        topic_name = str(data.get("topic_name", "")).strip()

        if learning_mode not in {None, "conversation"}:
            learning_mode = None

        if topic_id is None:
            # Cho phép LLM trả topic_name thay vì topic_id và tự map lại phía server.
            if learning_mode == "conversation" and topic_name:
                topic = find_topic(learning_mode, topic_name)
                if topic:
                    topic_id = str(topic.get("id") or "").strip().lower() or None
            if topic_id is None and learning_mode == "conversation":
                topic = find_topic(learning_mode, user_text)
                if topic:
                    topic_id = str(topic.get("id") or "").strip().lower() or None

        if intent == "learning_vocab":
            return IntentResult(intent="other")
        if intent == "learning_conversation":
            return IntentResult(intent="learning_conversation", learning_mode="conversation", topic_id=topic_id)
        if intent == "learning_topic":
            if learning_mode != "conversation":
                return IntentResult(intent="other")
            return IntentResult(intent="learning_topic", learning_mode=learning_mode, topic_id=topic_id)

        return IntentResult(intent="other")
