"""
Pipeline orchestrator: STT → LLM → TTS.

Dùng asyncio.Queue để pre-fetch TTS:
- Producer: LLM stream → tách câu → TTS → đẩy opus frames vào queue
- Consumer: đọc queue → gửi frames cho ESP32
→ Trong khi đang gửi audio câu 1, đã TTS câu 2 sẵn rồi.
"""

import asyncio
import json
from app.server_logging import get_logger
import re
from typing import Callable, Awaitable

from app.mcp import MCPToolRegistry
from app.services.intent import IntentDetectorService
from app.services.stt import STTService
from app.services.llm import LLMService
from app.services.tts import TTSService
from app.services.learning_content import (
    build_conversation_lesson,
    build_vocab_lesson_steps,
    build_mode_suggestion,
    build_vocab_lesson,
    find_topic,
    get_topic_by_id,
)
from app.services.flashcard_vocab import (
    build_finish_reply,
    build_flashcard_start_reply,
    build_next_card_prompt,
    evaluate_flashcard_answer,
    flashcard_count,
    get_flashcard_by_word,
)

logger = get_logger(__name__)

SENTENCE_ENDINGS = frozenset(".!?;,\n")

CHUNK_MIN_CHARS = 28
CHUNK_TARGET_CHARS = 58
CHUNK_HARD_LIMIT = 90
CHUNK_PUNCT_BREAKS = frozenset(",，:：、")
CHUNK_SPACE_BREAK = " "


_DONE = object()

_SENTENCE_MARKER = "__sentence__"
VOCAB_BATCH_SIZE = 6
LOCK_WORD_LIMIT = 6


class ConversationPipeline:
    """
    Orchestrator: audio PCM → text → AI response → audio Opus.
    Pre-fetch TTS qua asyncio.Queue de giam giat giua cac cau.
    """

    def __init__(
        self,
        stt: STTService,
        llm: LLMService,
        tts: TTSService,
        intent_detector: IntentDetectorService | None = None,
        mcp_tools: MCPToolRegistry | None = None,
        *,
        prefer_fast_only: bool = True,
    ):
        self._stt = stt
        self._llm = llm
        self._tts = tts
        self._intent_detector = intent_detector
        self._mcp_tools = mcp_tools
        self._prefer_fast_only = prefer_fast_only

    async def process(
        self,
        pcm_data: bytes,
        chat_history: list[dict],
        *,
        learning_context: dict[str, str | None] | None = None,
        on_stt_result: Callable[[str], Awaitable[None]],
        on_tts_start: Callable[[], Awaitable[None]],
        on_tts_sentence: Callable[[str], Awaitable[None]],
        on_tts_audio: Callable[[bytes], Awaitable[None]],
        on_tts_stop: Callable[[], Awaitable[None]],
        on_music_action: Callable[[dict], Awaitable[None]],
        on_learning_card: Callable[[dict], Awaitable[None]] | None = None,
        assignment_provider: Callable[[], Awaitable[dict | None]] | None = None,
        on_emotion: Callable[[str], Awaitable[None]] | None = None,
        is_aborted: Callable[[], bool],
    ) -> tuple[str, str] | None:
        """Chay toan bo pipeline. Returns (user_text, assistant_response)."""

        # -- Buoc 1: STT --
        user_text = await self._stt.transcribe(pcm_data)
        if not user_text:
            logger.info("STT returned empty, skipping")
            return None

        logger.info(f"\033[92m🎤 User: {user_text}\033[0m")
        await on_stt_result(user_text)

        if learning_context and self._is_flashcard_vocab_active(learning_context):
            await on_tts_start()
            reply_text = await self._handle_flashcard_vocab_turn(
                user_text,
                learning_context=learning_context,
                on_tts_sentence=on_tts_sentence,
                on_tts_audio=on_tts_audio,
                on_learning_card=on_learning_card,
                is_aborted=is_aborted,
            )
            if not is_aborted():
                await on_tts_stop()
            return (user_text, reply_text)

        # Nếu đã vào mode học theo chủ đề (locked), ưu tiên chạy tiếp flow hiện tại,
        # tránh nhảy intent do STT nhiễu.
        if learning_context and self._is_learning_locked(learning_context):
            if self._looks_like_exit_learning_request(user_text):
                learning_context["locked"] = "0"
                learning_context["mode"] = None
                learning_context["topic_id"] = None
                learning_context["next_index"] = "0"
                learning_context["finished"] = "0"

                await on_tts_start()
                reply_text = "Đã thoát chế độ học theo chủ đề. Bạn muốn học chủ đề nào tiếp theo?"
                await on_tts_sentence(reply_text)
                await self._send_frames_with_pacing(
                    self._tts.synthesize(reply_text),
                    on_tts_audio=on_tts_audio,
                    is_aborted=is_aborted,
                )
                if not is_aborted():
                    await on_tts_stop()
                return (user_text, reply_text)

            # Không phải câu follow-up học tập thì mở lock để tránh tự động dạy nhầm.
            if not self._looks_like_learning_followup(user_text, learning_context):
                learning_context["locked"] = "0"
                learning_context["mode"] = None
                learning_context["topic_id"] = None
                learning_context["next_index"] = "0"
                learning_context["finished"] = "0"
                learning_context["lock_target_index"] = "0"
            else:
                locked_mode = str(learning_context.get("mode") or "").strip()
                locked_topic_id = str(learning_context.get("topic_id") or "").strip()

                if locked_mode == "vocabulary" and locked_topic_id:
                    selected_topic = get_topic_by_id("vocabulary", locked_topic_id)
                    if selected_topic:
                        await on_tts_start()
                        start_index = self._context_next_index(learning_context)
                        reply_text, next_index, total_words = await self._teach_vocabulary_stepwise(
                            selected_topic,
                            on_tts_sentence=on_tts_sentence,
                            on_tts_audio=on_tts_audio,
                            on_learning_card=on_learning_card,
                            is_aborted=is_aborted,
                            start_index=start_index,
                            batch_size=VOCAB_BATCH_SIZE,
                        )
                        learning_context["next_index"] = str(next_index)
                        learning_context["finished"] = "1" if next_index >= total_words else "0"
                        lock_target_index = self._lock_target_index(learning_context)
                        if next_index >= lock_target_index:
                            learning_context["locked"] = "0"
                        if not is_aborted():
                            await on_tts_stop()
                        return (user_text, reply_text)

                if locked_mode == "conversation" and locked_topic_id:
                    selected_topic = get_topic_by_id("conversation", locked_topic_id)
                    if selected_topic:
                        await on_tts_start()
                        reply_text = build_conversation_lesson(selected_topic)
                        await on_tts_sentence(reply_text)
                        await self._send_frames_with_pacing(
                            self._tts.synthesize(reply_text),
                            on_tts_audio=on_tts_audio,
                            is_aborted=is_aborted,
                        )
                        if not is_aborted():
                            await on_tts_stop()
                        return (user_text, reply_text)

            # Lock không hợp lệ thì mở khóa để fallback xử lý bình thường.
            learning_context["locked"] = "0"

        # Fast path: lệnh mở nhạc -> bỏ qua LLM hội thoại dài để giảm lag.
        if self._intent_detector:
            fast_intent = self._intent_detector.detect_fast(user_text)
            resolved_intent = fast_intent

            # Ưu tiên LLM cho intent luyện hội thoại để chịu lỗi STT tốt hơn.
            # Học từ vựng theo chủ đề không còn được kích hoạt bằng intent giọng nói.
            if self._looks_like_learning_conversation_request(user_text) or fast_intent.intent in {
                "learning_conversation",
                "learning_topic",
            }:
                try:
                    llm_intent = await self._intent_detector.detect_learning_intent(user_text)
                    if llm_intent.intent in {"learning_conversation", "learning_topic"}:
                        resolved_intent = llm_intent
                except Exception as e:
                    logger.warning("LLM learning intent fallback failed: %s", e)

            if (
                learning_context
                and str(learning_context.get("mode") or "") == "vocabulary"
                and self._looks_like_continue_request(user_text)
            ):
                current_topic_id = str(learning_context.get("topic_id") or "").strip()
                selected_topic = get_topic_by_id("vocabulary", current_topic_id) if current_topic_id else None
                if selected_topic:
                    await on_tts_start()
                    start_index = self._context_next_index(learning_context)
                    reply_text, next_index, total_words = await self._teach_vocabulary_stepwise(
                        selected_topic,
                        on_tts_sentence=on_tts_sentence,
                        on_tts_audio=on_tts_audio,
                        on_learning_card=on_learning_card,
                        is_aborted=is_aborted,
                        start_index=start_index,
                        batch_size=VOCAB_BATCH_SIZE,
                    )
                    learning_context["topic_id"] = str(selected_topic.get("id") or "")
                    learning_context["next_index"] = str(next_index)
                    learning_context["finished"] = "1" if next_index >= total_words else "0"
                    if not is_aborted():
                        await on_tts_stop()
                    return (user_text, reply_text)

            if resolved_intent.intent == "learning_conversation" or (
                resolved_intent.intent == "learning_topic"
                and resolved_intent.learning_mode == "conversation"
            ):
                await on_tts_start()
                mode, selected_topic, reply_text = self._handle_learning_intent(
                    user_text,
                    resolved_intent.intent,
                    learning_mode=resolved_intent.learning_mode,
                    topic_id=resolved_intent.topic_id,
                    learning_context=learning_context,
                )
                if mode == "vocabulary" and selected_topic:
                    start_index = 0
                    if learning_context is not None:
                        total_words = len(selected_topic.get("words") or [])
                        lock_target_index = min(total_words, start_index + LOCK_WORD_LIMIT)
                        learning_context["mode"] = "vocabulary"
                        learning_context["topic_id"] = str(selected_topic.get("id") or "")
                        learning_context["next_index"] = "0"
                        learning_context["finished"] = "0"
                        learning_context["locked"] = "1"
                        learning_context["lock_target_index"] = str(lock_target_index)

                    reply_text, next_index, total_words = await self._teach_vocabulary_stepwise(
                        selected_topic,
                        on_tts_sentence=on_tts_sentence,
                        on_tts_audio=on_tts_audio,
                        on_learning_card=on_learning_card,
                        is_aborted=is_aborted,
                        start_index=start_index,
                        batch_size=VOCAB_BATCH_SIZE,
                    )
                    if learning_context is not None:
                        learning_context["next_index"] = str(next_index)
                        learning_context["finished"] = "1" if next_index >= total_words else "0"
                        lock_target_index = self._lock_target_index(learning_context)
                        if next_index >= lock_target_index:
                            learning_context["locked"] = "0"
                else:
                    if learning_context is not None and mode == "conversation" and selected_topic:
                        learning_context["mode"] = "conversation"
                        learning_context["topic_id"] = str(selected_topic.get("id") or "")
                        learning_context["next_index"] = "0"
                        learning_context["finished"] = "0"
                        learning_context["locked"] = "1"
                        learning_context["lock_target_index"] = "0"
                    await on_tts_sentence(reply_text)
                    await self._send_frames_with_pacing(
                        self._tts.synthesize(reply_text),
                        on_tts_audio=on_tts_audio,
                        is_aborted=is_aborted,
                    )
                if not is_aborted():
                    await on_tts_stop()
                return (user_text, reply_text)

            if resolved_intent.intent == "flashcard_vocab":
                await on_tts_start()
                if learning_context is not None:
                    self._start_flashcard_vocab_context(learning_context)
                reply_text = build_flashcard_start_reply()
                await self._speak_text(
                    reply_text,
                    on_tts_sentence=on_tts_sentence,
                    on_tts_audio=on_tts_audio,
                    is_aborted=is_aborted,
                    language_hint="vi",
                )
                if not is_aborted():
                    await on_tts_stop()
                return (user_text, reply_text)

            if resolved_intent.intent == "assignment":
                await on_tts_start()
                reply_text = "Hiện chưa có bài tập nào được giao."
                if assignment_provider:
                    try:
                        assignment = await assignment_provider()
                    except Exception as e:
                        logger.warning("Assignment provider failed: %s", e)
                        assignment = None
                    if assignment:
                        title = str(assignment.get("title") or "Bài tập hôm nay").strip()
                        instructions = str(assignment.get("instructions") or "").strip()
                        due_at = str(assignment.get("due_at") or "").strip()
                        due_suffix = f" Hạn nộp: {due_at}." if due_at else ""
                        reply_text = (
                            f"Bài tập của con là: {title}. "
                            f"Yêu cầu: {instructions}."
                            f"{due_suffix} "
                            "Con đọc lại yêu cầu và bắt đầu làm từng bước nhé."
                        )
                await on_tts_sentence(reply_text)
                await self._send_frames_with_pacing(
                    self._tts.synthesize(reply_text),
                    on_tts_audio=on_tts_audio,
                    is_aborted=is_aborted,
                )
                if not is_aborted():
                    await on_tts_stop()
                return (user_text, reply_text)

            if learning_context and learning_context.get("mode") in {"vocabulary", "conversation"}:
                inferred_mode = str(learning_context.get("mode"))
                selected_topic = find_topic(inferred_mode, user_text)
                if selected_topic:
                    await on_tts_start()
                    if inferred_mode == "vocabulary":
                        reply_text, next_index, total_words = await self._teach_vocabulary_stepwise(
                            selected_topic,
                            on_tts_sentence=on_tts_sentence,
                            on_tts_audio=on_tts_audio,
                            on_learning_card=on_learning_card,
                            is_aborted=is_aborted,
                            start_index=0,
                            batch_size=VOCAB_BATCH_SIZE,
                        )
                        learning_context["topic_id"] = str(selected_topic.get("id") or "")
                        learning_context["next_index"] = str(next_index)
                        learning_context["finished"] = "1" if next_index >= total_words else "0"
                    else:
                        reply_text = build_conversation_lesson(selected_topic)
                        await on_tts_sentence(reply_text)
                        await self._send_frames_with_pacing(
                            self._tts.synthesize(reply_text),
                            on_tts_audio=on_tts_audio,
                            is_aborted=is_aborted,
                        )
                    if not is_aborted():
                        await on_tts_stop()
                    return (user_text, reply_text)

            if resolved_intent.intent == "music":
                logger.info("Fast music intent detected: %s", fast_intent.song_name)
                await on_tts_start()
                song_name = resolved_intent.song_name or "nhạc việt"
                music_payload = await self._call_music_tool(
                    song_name,
                    on_music_action=on_music_action,
                )
                await self._stream_music_preview(
                    music_payload,
                    on_tts_sentence=on_tts_sentence,
                    on_tts_audio=on_tts_audio,
                    is_aborted=is_aborted,
                )
                if not is_aborted():
                    await on_tts_stop()
                return (user_text, "")
            if resolved_intent.intent == "alarm":
                logger.info("Fast alarm intent detected: %s", resolved_intent.alarm_time)
                await on_tts_start()
                if not self._mcp_tools:
                    await on_tts_sentence("MCP tool chưa sẵn sàng để đặt báo thức.")
                    if not is_aborted():
                        await on_tts_stop()
                    return (user_text, "")

                # Call MCP set_alarm
                try:
                    args = {"time": resolved_intent.alarm_time or "", "message": resolved_intent.alarm_message or "Báo thức"}
                    tool_result = await self._mcp_tools.call_tool("set_alarm", args)
                    if tool_result.ok:
                        await on_tts_sentence(f"Đã đặt báo thức vào lúc {resolved_intent.alarm_time}.")
                    else:
                        # try to extract error text
                        err_text = "không thể đặt báo thức"
                        for it in tool_result.content:
                            if isinstance(it, dict) and it.get("type") == "text":
                                err_text = it.get("text")
                                break
                        await on_tts_sentence(f"Lỗi: {err_text}")
                except Exception as e:
                    logger.error("Alarm tool call failed: %s", e, exc_info=True)
                    await on_tts_sentence("Lỗi khi gọi tool đặt báo thức")

                if not is_aborted():
                    await on_tts_stop()
                return (user_text, "")

        # -- Buoc 2: LLM → tach cau → TTS (pre-fetch queue) --
        await on_tts_start()

        music_mode = {"active": False}

        response_task = asyncio.create_task(
            self._stream_response(
                user_text,
                chat_history,
                on_tts_sentence=on_tts_sentence,
                on_tts_audio=on_tts_audio,
                on_emotion=on_emotion,
                is_aborted=is_aborted,
                should_stop_generation=lambda: music_mode["active"],
            )
        )

        intent_task = None
        if not self._prefer_fast_only and self._intent_detector:
            intent_task = asyncio.create_task(
                self._detect_and_handle_music_intent(
                    user_text,
                    on_music_action=on_music_action,
                    on_music_detected=lambda: music_mode.__setitem__("active", True),
                )
            )

        full_response = await response_task
        music_payload = None
        if intent_task:
            music_payload = await intent_task

        if isinstance(music_payload, dict) and music_payload.get("intent") == "music":
            await self._stream_music_preview(
                music_payload,
                on_tts_sentence=on_tts_sentence,
                on_tts_audio=on_tts_audio,
                is_aborted=is_aborted,
            )

        if not is_aborted():
            await on_tts_stop()

        return (user_text, full_response) if full_response else None

    @staticmethod
    def _handle_learning_intent(
        user_text: str,
        intent: str,
        *,
        learning_mode: str | None,
        topic_id: str | None,
        learning_context: dict[str, str | None] | None,
    ) -> tuple[str | None, dict | None, str]:
        mode = learning_mode
        if intent == "learning_vocab":
            mode = "vocabulary"
        elif intent == "learning_conversation":
            mode = "conversation"

        # LLM intent đôi khi trả learning_topic nhưng thiếu learning_mode/topic_id.
        # Tự suy luận mode/topic từ câu user để tránh lặp hỏi lại.
        inferred_topic = None
        if mode not in {"vocabulary", "conversation"}:
            if learning_context and learning_context.get("mode") in {"vocabulary", "conversation"}:
                mode = str(learning_context.get("mode"))

            vocab_topic = find_topic("vocabulary", user_text)
            conv_topic = find_topic("conversation", user_text)
            if vocab_topic:
                mode = "vocabulary"
                inferred_topic = vocab_topic
            elif conv_topic:
                mode = "conversation"
                inferred_topic = conv_topic

        if mode in {"vocabulary", "conversation"} and learning_context is not None:
            learning_context["mode"] = mode

        selected_topic = None
        if topic_id and mode in {"vocabulary", "conversation"}:
            selected_topic = get_topic_by_id(mode, topic_id)
        if selected_topic is None and inferred_topic is not None:
            selected_topic = inferred_topic
        if selected_topic is None and mode in {"vocabulary", "conversation"}:
            selected_topic = find_topic(mode, user_text)

        if selected_topic:
            if mode == "vocabulary":
                return (mode, selected_topic, build_vocab_lesson(selected_topic))
            return (mode, selected_topic, build_conversation_lesson(selected_topic))

        if mode == "vocabulary":
            return (mode, None, build_mode_suggestion("vocabulary"))
        if mode == "conversation":
            return (mode, None, build_mode_suggestion("conversation"))
        return (None, None, "Mình đã sẵn sàng học theo chủ đề. Bạn muốn học từ vựng hay luyện hội thoại trước?")

    async def _speak_text(
        self,
        text: str,
        *,
        on_tts_sentence: Callable[[str], Awaitable[None]],
        on_tts_audio: Callable[[bytes], Awaitable[None]],
        is_aborted: Callable[[], bool],
        language_hint: str | None = None,
    ) -> None:
        await on_tts_sentence(text)
        await self._send_frames_with_pacing(
            self._tts.synthesize(text, language_hint=language_hint),
            on_tts_audio=on_tts_audio,
            is_aborted=is_aborted,
        )

    @staticmethod
    def _start_flashcard_vocab_context(learning_context: dict[str, str | None]) -> None:
        learning_context["mode"] = "flashcard_vocab"
        learning_context["topic_id"] = None
        learning_context["next_index"] = "0"
        learning_context["finished"] = "0"
        learning_context["locked"] = "0"
        learning_context["lock_target_index"] = "0"
        learning_context["attempt_count"] = "0"
        learning_context["seen_words"] = ""

    @staticmethod
    def _clear_learning_context(learning_context: dict[str, str | None]) -> None:
        learning_context["mode"] = None
        learning_context["topic_id"] = None
        learning_context["next_index"] = "0"
        learning_context["finished"] = "0"
        learning_context["locked"] = "0"
        learning_context["lock_target_index"] = "0"
        learning_context["attempt_count"] = "0"
        learning_context["seen_words"] = ""

    @staticmethod
    def _is_flashcard_vocab_active(learning_context: dict[str, str | None]) -> bool:
        return str(learning_context.get("mode") or "").strip() == "flashcard_vocab"

    async def _handle_flashcard_vocab_turn(
        self,
        user_text: str,
        *,
        learning_context: dict[str, str | None],
        on_tts_sentence: Callable[[str], Awaitable[None]],
        on_tts_audio: Callable[[bytes], Awaitable[None]],
        on_learning_card: Callable[[dict], Awaitable[None]] | None,
        is_aborted: Callable[[], bool],
    ) -> str:
        if self._looks_like_exit_learning_request(user_text):
            self._clear_learning_context(learning_context)
            reply_text = "Đã dừng luyện flash card. Khi nào muốn học tiếp, bạn nói học từ vựng nhé."
            await self._speak_text(
                reply_text,
                on_tts_sentence=on_tts_sentence,
                on_tts_audio=on_tts_audio,
                is_aborted=is_aborted,
                language_hint="vi",
            )
            return reply_text

        seen_words = self._context_seen_words(learning_context)
        if len(seen_words) >= flashcard_count():
            self._clear_learning_context(learning_context)
            reply_text = build_finish_reply()
            await self._speak_text(
                reply_text,
                on_tts_sentence=on_tts_sentence,
                on_tts_audio=on_tts_audio,
                is_aborted=is_aborted,
                language_hint="vi",
            )
            return reply_text

        evaluation = await evaluate_flashcard_answer(self._llm, student_text=user_text)
        attempts = self._context_attempt_count(learning_context)
        feedback = str(evaluation.get("feedback_vi") or "").strip()
        is_correct = bool(evaluation.get("is_correct"))
        matched_word = str(evaluation.get("matched_word") or "").strip().lower()
        matched_card = get_flashcard_by_word(matched_word) if matched_word else None

        if is_correct and matched_card:
            if on_learning_card:
                await on_learning_card(
                    {
                        "state": "flashcard",
                        "kind": "award",
                        "word": matched_card.get("word"),
                        "meaning": matched_card.get("meaning_vi"),
                        "image_url": "/static/asset/award_320.png",
                        "duration_ms": 2000,
                    }
                )
            if matched_word not in seen_words:
                seen_words.append(matched_word)
            learning_context["seen_words"] = ",".join(seen_words)
            learning_context["next_index"] = str(len(seen_words))
            learning_context["attempt_count"] = "0"
            if len(seen_words) >= flashcard_count():
                learning_context["finished"] = "1"
                reply_text = (
                    f"Tuyệt vời! Từ {matched_card.get('word')} nghĩa là {matched_card.get('meaning_vi')}. "
                    f"{build_finish_reply()}"
                )
                self._clear_learning_context(learning_context)
            else:
                reply_text = (
                    f"Tuyệt vời! Từ {matched_card.get('word')} nghĩa là {matched_card.get('meaning_vi')}. "
                    f"{build_next_card_prompt()}"
                )
        else:
            attempts += 1
            learning_context["attempt_count"] = str(attempts)
            unknown_word = str(evaluation.get("unknown_word") or "").strip()
            unknown_meaning_vi = str(evaluation.get("unknown_meaning_vi") or "").strip()
            if unknown_word:
                learning_context["attempt_count"] = "0"
                if feedback:
                    reply_text = feedback
                elif unknown_meaning_vi:
                    reply_text = (
                        f"Từ {unknown_word} nghĩa là {unknown_meaning_vi}. "
                        f"{build_next_card_prompt()}"
                    )
                else:
                    reply_text = (
                        f"Từ {unknown_word} là một từ tiếng Anh. "
                        f"{build_next_card_prompt()}"
                    )
            elif attempts >= 3:
                learning_context["attempt_count"] = "0"
                reply_text = (
                    "Bạn đổi sang một thẻ khác hoặc đọc lại chậm hơn nhé."
                )
            else:
                reply_text = (
                    f"{feedback} Bạn nhìn lại thẻ và đọc lại một lần nữa nhé."
                )

        await self._speak_text(
            reply_text,
            on_tts_sentence=on_tts_sentence,
            on_tts_audio=on_tts_audio,
            is_aborted=is_aborted,
            language_hint="vi",
        )
        return reply_text

    async def _teach_vocabulary_stepwise(
        self,
        topic: dict,
        *,
        on_tts_sentence: Callable[[str], Awaitable[None]],
        on_tts_audio: Callable[[bytes], Awaitable[None]],
        on_learning_card: Callable[[dict], Awaitable[None]] | None,
        is_aborted: Callable[[], bool],
        start_index: int = 0,
        batch_size: int = VOCAB_BATCH_SIZE,
    ) -> tuple[str, int, int]:
        total_words = len(topic.get("words") or [])
        steps = build_vocab_lesson_steps(topic, max_words=batch_size, start_index=start_index)
        spoken_lines: list[str] = []
        for step in steps:
            if is_aborted():
                break
            flashcard_payload = step.get("flashcard") if isinstance(step, dict) else None
            if flashcard_payload and on_learning_card:
                await on_learning_card(flashcard_payload)
            speech = str(step.get("speech") or "").strip() if isinstance(step, dict) else ""
            if not speech:
                continue
            spoken_lines.append(speech)
            await on_tts_sentence(speech)
            await self._send_frames_with_pacing(
                self._tts.synthesize(speech),
                on_tts_audio=on_tts_audio,
                is_aborted=is_aborted,
            )
            await asyncio.sleep(self._tts.frame_duration_s * 2)
        consumed = sum(1 for step in steps if isinstance(step, dict) and step.get("flashcard"))
        next_index = min(total_words, max(0, start_index) + consumed)
        return (" ".join(spoken_lines).strip(), next_index, total_words)

    @staticmethod
    def _looks_like_learning_conversation_request(text: str) -> bool:
        lowered = (text or "").lower()
        learning_markers = (
            "hội thoại",
            "hoi thoai",
            "luyện",
            "luyen",
            "giao tiếp",
            "giao tiep",
        )
        return any(marker in lowered for marker in learning_markers)

    @staticmethod
    def _looks_like_continue_request(text: str) -> bool:
        lowered = (text or "").lower()
        continue_markers = (
            "học tiếp",
            "hoc tiep",
            "tiếp tục",
            "tiep tuc",
            "học nữa",
            "hoc nua",
            "tiếp nữa",
            "tiep nua",
        )
        return any(marker in lowered for marker in continue_markers)

    @staticmethod
    def _looks_like_exit_learning_request(text: str) -> bool:
        lowered = (text or "").lower()
        exit_markers = (
            "thoát",
            "thoat",
            "dừng học",
            "dung hoc",
            "kết thúc",
            "ket thuc",
            "đổi chủ đề",
            "doi chu de",
            "chuyển chủ đề",
            "chuyen chu de",
        )
        return any(marker in lowered for marker in exit_markers)

    @staticmethod
    def _is_learning_locked(learning_context: dict[str, str | None]) -> bool:
        return str(learning_context.get("locked") or "0").strip() in {"1", "true", "yes", "on"}

    @staticmethod
    def _looks_like_learning_followup(text: str, learning_context: dict[str, str | None]) -> bool:
        lowered = (text or "").lower()
        markers = (
            "học",
            "hoc",
            "tiếp",
            "tiep",
            "nhắc lại",
            "nhac lai",
            "từ",
            "tu",
            "vựng",
            "vung",
            "hội thoại",
            "hoi thoai",
            "chủ đề",
            "chu de",
            "đọc lại",
            "doc lai",
        )
        if any(m in lowered for m in markers):
            return True

        topic_id = str(learning_context.get("topic_id") or "").strip().lower()
        if topic_id and topic_id in lowered:
            return True
        return False

    @staticmethod
    def _lock_target_index(learning_context: dict[str, str | None]) -> int:
        raw = str(learning_context.get("lock_target_index") or "0").strip()
        try:
            value = int(raw)
        except Exception:
            return LOCK_WORD_LIMIT
        return value if value > 0 else LOCK_WORD_LIMIT

    @staticmethod
    def _context_next_index(learning_context: dict[str, str | None]) -> int:
        raw = str(learning_context.get("next_index") or "0").strip()
        try:
            return max(0, int(raw))
        except Exception:
            return 0

    @staticmethod
    def _context_attempt_count(learning_context: dict[str, str | None]) -> int:
        raw = str(learning_context.get("attempt_count") or "0").strip()
        try:
            return max(0, int(raw))
        except Exception:
            return 0

    @staticmethod
    def _context_seen_words(learning_context: dict[str, str | None]) -> list[str]:
        raw = str(learning_context.get("seen_words") or "").strip()
        return [word.strip().lower() for word in raw.split(",") if word.strip()]

    async def _detect_and_handle_music_intent(
        self,
        user_text: str,
        *,
        on_music_action: Callable[[dict], Awaitable[None]],
        on_music_detected: Callable[[], None],
    ) -> dict | None:
        """Detect intent song song với luồng LLM chính, gọi tool nếu là music."""
        if not self._intent_detector:
            return None

        try:
            intent = await self._intent_detector.detect(user_text)
            if intent.intent != "music":
                await on_music_action({"intent": "other"})
                return {"intent": "other"}

            on_music_detected()

            if not self._mcp_tools:
                payload = {
                    "intent": "music",
                    "song_name": intent.song_name,
                    "ok": False,
                    "error": "MCP tool registry chưa sẵn sàng",
                }
                await on_music_action(payload)
                return payload

            song_name = intent.song_name or "nhạc việt"
            payload = await self._call_music_tool(
                song_name,
                on_music_action=on_music_action,
            )
            return payload
        except Exception as e:
            logger.error("Intent flow failed: %s", e, exc_info=True)
            payload = {
                "intent": "other",
                "ok": False,
                "error": str(e),
            }
            await on_music_action(payload)
            return payload

    async def _stream_response(
        self,
        user_text: str,
        chat_history: list[dict],
        *,
        on_tts_sentence: Callable[[str], Awaitable[None]],
        on_tts_audio: Callable[[bytes], Awaitable[None]],
        on_emotion: Callable[[str], Awaitable[None]] | None = None,
        is_aborted: Callable[[], bool],
        should_stop_generation: Callable[[], bool],
    ) -> str:
        """
        LLM streaming → tach cau → TTS pre-fetch → gui audio.

        Producer: LLM chunks → sentences → TTS → opus frames → queue
        Consumer: queue → on_tts_audio (gui ESP32)
        """
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        full_response = ""
        producer_error = None

        async def producer():
            nonlocal full_response, producer_error
            try:
                raw_response_parts: list[str] = []
                async for chunk in self._llm.chat_stream(user_text, chat_history):
                    if is_aborted() or should_stop_generation():
                        break
                    raw_response_parts.append(chunk)

                raw_response = "".join(raw_response_parts).strip()
                if not raw_response:
                    return

                response_language, response_text = self._parse_llm_tts_payload(raw_response)
                full_response = response_text

                buffer = response_text
                while True:
                    sentence, buffer = self._extract_sentence(buffer)
                    if not sentence:
                        break
                    await self._enqueue_sentence(
                        sentence,
                        queue,
                        on_tts_sentence,
                        is_aborted,
                        language=response_language,
                    )

                while len(buffer) >= CHUNK_HARD_LIMIT and not is_aborted() and not should_stop_generation():
                    text_chunk, buffer = self._extract_soft_chunk(buffer)
                    if not text_chunk:
                        break
                    await self._enqueue_sentence(
                        text_chunk,
                        queue,
                        on_tts_sentence,
                        is_aborted,
                        language=response_language,
                    )

                remaining = buffer.strip()
                if remaining and not is_aborted() and not should_stop_generation():
                    await self._enqueue_sentence(
                        remaining,
                        queue,
                        on_tts_sentence,
                        is_aborted,
                        language=response_language,
                    )
            except Exception as e:
                producer_error = e
                logger.error(f"Producer error: {e}", exc_info=True)
            finally:
                await queue.put(_DONE)

        async def consumer():
            total_frames = 0
            # Gửi trước vài frames để pre-buffer trên ESP32
            PRE_BUFFER = 3
            FRAME_S = self._tts.frame_duration_s
            PACE = 1.0      # đồng bộ 1:1 với tốc độ phát
            GRACE_S = 0.05  # nghỉ 50ms giữa các câu cho tự nhiên
            next_send_ts: float | None = None
            has_spoken_sentence = False
            loop = asyncio.get_running_loop()

            while True:
                item = await queue.get()
                if item is _DONE:
                    break
                if is_aborted() or should_stop_generation():
                    continue  # drain queue

                # Sentence marker: gui sentence_start SAU KHI audio cau truoc da gui het
                if isinstance(item, tuple) and item[0] == _SENTENCE_MARKER:
                    if has_spoken_sentence:
                        # Đợi thêm 1 frame + grace để client phát dứt câu trước.
                        await asyncio.sleep(FRAME_S + GRACE_S)

                    await on_tts_sentence(item[1])
                    has_spoken_sentence = True
                    continue

                if isinstance(item, bytes):
                    await on_tts_audio(item)
                total_frames += 1

                # Pacing: đảm bảo không gửi nhanh hơn tốc độ phát
                if total_frames == PRE_BUFFER:
                    next_send_ts = loop.time() + FRAME_S * PACE
                elif total_frames > PRE_BUFFER and next_send_ts is not None:
                    now = loop.time()
                    if now < next_send_ts:
                        await asyncio.sleep(next_send_ts - now)
                    next_send_ts += FRAME_S * PACE

            logger.info(f"\033[92m✅ Sent total {total_frames} opus frames\033[0m")

        # Chay song song: producer TTS cau tiep, consumer gui cau hien tai
        await asyncio.gather(producer(), consumer())

        if producer_error:
            logger.error(f"Pipeline had producer error: {producer_error}")

        return full_response

    async def _stream_music_preview(
        self,
        music_payload: dict,
        *,
        on_tts_sentence: Callable[[str], Awaitable[None]],
        on_tts_audio: Callable[[bytes], Awaitable[None]],
        is_aborted: Callable[[], bool],
    ) -> None:
        """Phát preview nhạc thật từ Deezer khi intent là music."""
        if not self._mcp_tools or is_aborted():
            return

        requested_song = str(music_payload.get("song_name") or "bài nhạc").strip()
        tracks = self._extract_tracks(music_payload)
        if not tracks:
            await on_tts_sentence("Mình chưa tìm thấy bản nhạc phù hợp, để mình thử nguồn khác.")
            await self._send_frames_with_pacing(
                self._tts.synthesize("Mình chưa tìm thấy bản nhạc phù hợp, để mình thử nguồn khác."),
                on_tts_audio=on_tts_audio,
                is_aborted=is_aborted,
            )

            streamed = await self._send_frames_with_pacing(
                self._tts.stream_full_song_by_query(requested_song),
                on_tts_audio=on_tts_audio,
                is_aborted=is_aborted,
            )
            if streamed == 0:
                await on_tts_sentence("Xin lỗi, hiện tại mình chưa phát được bài này. Bạn thử nói rõ tên bài hoặc ca sĩ nhé.")
                await self._send_frames_with_pacing(
                    self._tts.synthesize("Xin lỗi, hiện tại mình chưa phát được bài này. Bạn thử nói rõ tên bài hoặc ca sĩ nhé."),
                    on_tts_audio=on_tts_audio,
                    is_aborted=is_aborted,
                )
            return

        first = tracks[0]
        title = str(first.get("title") or music_payload.get("song_name") or "bài nhạc")
        artist = str(first.get("artist") or "")
        preview_url = str(first.get("preview_url") or "").strip()

        if artist:
            ack = f"Đang mở bài {title} của {artist}."
        else:
            ack = f"Đang mở bài {title}."
        await on_tts_sentence(ack)
        await self._send_frames_with_pacing(
            self._tts.synthesize(ack),
            on_tts_audio=on_tts_audio,
            is_aborted=is_aborted,
        )

        # Đợi ngắn để tránh đè frame cuối của câu xác nhận.
        await asyncio.sleep(self._tts.frame_duration_s)

        await on_tts_sentence("Đang phát bài hát.")
        streamed = await self._send_frames_with_pacing(
            self._tts.stream_full_song_by_query(f"{title} {artist}".strip()),
            on_tts_audio=on_tts_audio,
            is_aborted=is_aborted,
        )

        # Fallback: nếu không stream được full song thì phát preview 30s.
        if streamed == 0 and preview_url:
            streamed = await self._send_frames_with_pacing(
                self._tts.stream_audio_url(preview_url),
                on_tts_audio=on_tts_audio,
                is_aborted=is_aborted,
            )

        if streamed == 0:
            await on_tts_sentence("Xin lỗi, mình chưa phát được bài này lúc này.")
            await self._send_frames_with_pacing(
                self._tts.synthesize("Xin lỗi, mình chưa phát được bài này lúc này."),
                on_tts_audio=on_tts_audio,
                is_aborted=is_aborted,
            )

    async def _send_frames_with_pacing(
        self,
        frame_stream,
        *,
        on_tts_audio: Callable[[bytes], Awaitable[None]],
        is_aborted: Callable[[], bool],
    ) -> int:
        """Gửi Opus frames theo tốc độ phát thực để tránh audio chồng/chạy nhanh."""
        pre_buffer = 3
        frame_s = self._tts.frame_duration_s
        pace = 1.0
        next_send_ts: float | None = None
        sent = 0
        loop = asyncio.get_running_loop()

        async for opus_frame in frame_stream:
            if is_aborted():
                return 0

            await on_tts_audio(opus_frame)
            sent += 1

            if sent == pre_buffer:
                next_send_ts = loop.time() + frame_s * pace
            elif sent > pre_buffer and next_send_ts is not None:
                now = loop.time()
                if now < next_send_ts:
                    await asyncio.sleep(next_send_ts - now)
                next_send_ts += frame_s * pace
        return sent

    async def _call_music_tool(
        self,
        song_name: str,
        *,
        on_music_action: Callable[[dict], Awaitable[None]],
    ) -> dict:
        """Gọi tool tìm nhạc và trả payload đồng nhất."""
        if not self._mcp_tools:
            payload = {
                "intent": "music",
                "song_name": song_name,
                "ok": False,
                "error": "MCP tool registry chưa sẵn sàng",
            }
            await on_music_action(payload)
            return payload

        request_body = {
            "song_name": song_name,
            "query": song_name,
            "limit": 5,
        }
        tool_result = await self._mcp_tools.call_tool(
            "search_vietnamese_music", request_body
        )

        payload = {
            "intent": "music",
            "song_name": song_name,
            "request_body": request_body,
            "ok": tool_result.ok,
            "content": tool_result.content,
        }
        await on_music_action(payload)
        return payload

    @staticmethod
    def _extract_tracks(music_payload: dict) -> list[dict]:
        """Trích tracks từ payload tool result."""
        content = music_payload.get("content")
        if not isinstance(content, list):
            return []

        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") != "json":
                continue
            data = item.get("json")
            if not isinstance(data, dict):
                continue
            tracks = data.get("tracks")
            if isinstance(tracks, list):
                return [t for t in tracks if isinstance(t, dict)]
        return []

    async def _enqueue_sentence(
        self,
        sentence: str,
        queue: asyncio.Queue,
        on_tts_sentence: Callable[[str], Awaitable[None]],
        is_aborted: Callable[[], bool],
        *,
        language: str | None = None,
    ) -> None:
        """TTS 1 cau → day tung opus frame vao queue."""
        logger.info(f"\033[92m🔊 TTS[{language or 'auto'}]: {sentence}\033[0m")
        # Gui sentence marker qua queue de dong bo voi audio frames
        await queue.put((_SENTENCE_MARKER, sentence))

        frame_count = 0
        async for opus_frame in self._tts.synthesize(
            sentence, language_hint=language
        ):
            if is_aborted():
                break
            await queue.put(opus_frame)
            frame_count += 1
        logger.info(f"\033[92m   Queued {frame_count} frames for: {sentence[:40]}\033[0m")

    @staticmethod
    def _parse_llm_tts_payload(raw_response: str) -> tuple[str | None, str]:
        """
        Parse response format:
        {"language":"vi|en","text":"..."}
        Fallback: treat raw as plain text.
        """
        raw = (raw_response or "").strip()
        if not raw:
            return (None, "")

        def _extract_payload(obj: dict) -> tuple[str | None, str] | None:
            text_val = obj.get("text")
            if not isinstance(text_val, str):
                return None
            lang_val = str(obj.get("language", "")).strip().lower()
            if lang_val not in {"vi", "en"}:
                lang_val = None
            return (lang_val, text_val.strip())

        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                payload = _extract_payload(parsed)
                if payload:
                    return payload
        except Exception:
            pass

        fenced = re.sub(
            r"^```(?:json)?\s*|\s*```$",
            "",
            raw,
            flags=re.IGNORECASE | re.DOTALL,
        ).strip()
        try:
            parsed = json.loads(fenced)
            if isinstance(parsed, dict):
                payload = _extract_payload(parsed)
                if payload:
                    return payload
        except Exception:
            pass

        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1 and end > start:
            snippet = raw[start : end + 1]
            try:
                parsed = json.loads(snippet)
                if isinstance(parsed, dict):
                    payload = _extract_payload(parsed)
                    if payload:
                        return payload
            except Exception:
                pass

        return (None, raw)

    @staticmethod
    def _extract_sentence(buffer: str) -> tuple[str | None, str]:
        """Tach cau dau tien hoan chinh tu buffer."""
        for i, char in enumerate(buffer):
            if char in SENTENCE_ENDINGS:
                sentence = buffer[: i + 1].strip()
                remaining = buffer[i + 1 :]
                if sentence and len(sentence) > 1:
                    return sentence, remaining
                return None, remaining
        return None, buffer

    @staticmethod
    def _extract_soft_chunk(buffer: str) -> tuple[str | None, str]:
        """Cat mot chunk nho khi chua co dau cau de giam do tre TTS."""
        if len(buffer) < CHUNK_MIN_CHARS:
            return None, buffer

        limit = min(len(buffer), CHUNK_HARD_LIMIT)
        punct_cut = -1
        for i in range(limit - 1, CHUNK_MIN_CHARS - 1, -1):
            if buffer[i] in CHUNK_PUNCT_BREAKS:
                punct_cut = i
                break

        if punct_cut != -1:
            chunk = buffer[: punct_cut + 1].rstrip()
            remaining = buffer[punct_cut + 1 :].lstrip()
        else:
            # fallback: cat o khoang trang, KHONG cat giua tu
            space_cut = buffer.rfind(CHUNK_SPACE_BREAK, CHUNK_MIN_CHARS, limit)
            if space_cut == -1:
                return None, buffer
            chunk = buffer[:space_cut].rstrip()
            remaining = buffer[space_cut + 1 :].lstrip()

        if len(chunk) < CHUNK_MIN_CHARS:
            return None, buffer
        return chunk, remaining
