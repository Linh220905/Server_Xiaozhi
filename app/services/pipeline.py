"""
Pipeline orchestrator: STT → LLM → TTS.

Dùng asyncio.Queue để pre-fetch TTS:
- Producer: LLM stream → tách câu → TTS → đẩy opus frames vào queue
- Consumer: đọc queue → gửi frames cho ESP32
→ Trong khi đang gửi audio câu 1, đã TTS câu 2 sẵn rồi.
"""

import asyncio
import logging
from typing import Callable, Awaitable

from app.mcp import MCPToolRegistry
from app.services.intent import IntentDetectorService
from app.services.stt import STTService
from app.services.llm import LLMService
from app.services.tts import TTSService

logger = logging.getLogger(__name__)

SENTENCE_ENDINGS = frozenset(".!?;,\n")

CHUNK_MIN_CHARS = 28
CHUNK_TARGET_CHARS = 58
CHUNK_HARD_LIMIT = 90
CHUNK_PUNCT_BREAKS = frozenset(",，:：、")
CHUNK_SPACE_BREAK = " "


_DONE = object()

_SENTENCE_MARKER = "__sentence__"


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
        on_stt_result: Callable[[str], Awaitable[None]],
        on_tts_start: Callable[[], Awaitable[None]],
        on_tts_sentence: Callable[[str], Awaitable[None]],
        on_tts_audio: Callable[[bytes], Awaitable[None]],
        on_tts_stop: Callable[[], Awaitable[None]],
        on_music_action: Callable[[dict], Awaitable[None]],
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

        # Fast path: lệnh mở nhạc -> bỏ qua LLM hội thoại dài để giảm lag.
        if self._intent_detector:
            fast_intent = self._intent_detector.detect_fast(user_text)
            if fast_intent.intent == "music":
                logger.info("Fast music intent detected: %s", fast_intent.song_name)
                await on_tts_start()
                music_payload = await self._call_music_tool(
                    fast_intent.song_name,
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

        # -- Buoc 2: LLM → tach cau → TTS (pre-fetch queue) --
        await on_tts_start()

        music_mode = {"active": False}

        response_task = asyncio.create_task(
            self._stream_response(
                user_text,
                chat_history,
                on_tts_sentence=on_tts_sentence,
                on_tts_audio=on_tts_audio,
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

            payload = await self._call_music_tool(
                intent.song_name,
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
            buffer = ""
            try:
                async for chunk in self._llm.chat_stream(user_text, chat_history):
                    if is_aborted() or should_stop_generation():
                        break
                    full_response += chunk
                    buffer += chunk

                    # Tach tat ca cau da hoan chinh
                    while True:
                        sentence, buffer = self._extract_sentence(buffer)
                        if not sentence:
                            break
                        await self._enqueue_sentence(
                            sentence, queue, on_tts_sentence, is_aborted
                        )

                    # Neu chua co dau cau ma buffer da dai, cat chunk nho de TTS som
                    while len(buffer) >= CHUNK_HARD_LIMIT and not is_aborted() and not should_stop_generation():
                        text_chunk, buffer = self._extract_soft_chunk(buffer)
                        if not text_chunk:
                            break
                        await self._enqueue_sentence(
                            text_chunk, queue, on_tts_sentence, is_aborted
                        )

                # Phan con lai
                remaining = buffer.strip()
                if remaining and not is_aborted() and not should_stop_generation():
                    await self._enqueue_sentence(
                        remaining, queue, on_tts_sentence, is_aborted
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

        tracks = self._extract_tracks(music_payload)
        if not tracks:
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

        if not preview_url:
            return

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
            await self._send_frames_with_pacing(
                self._tts.stream_audio_url(preview_url),
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
    ) -> None:
        """TTS 1 cau → day tung opus frame vao queue."""
        logger.info(f"\033[92m🔊 TTS: {sentence}\033[0m")
        # Gui sentence marker qua queue de dong bo voi audio frames
        await queue.put((_SENTENCE_MARKER, sentence))

        frame_count = 0
        async for opus_frame in self._tts.synthesize(sentence):
            if is_aborted():
                break
            await queue.put(opus_frame)
            frame_count += 1
        logger.info(f"\033[92m   Queued {frame_count} frames for: {sentence[:40]}\033[0m")

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
