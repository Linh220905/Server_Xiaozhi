"""
Session state cho mỗi client kết nối.

Mỗi ESP32 kết nối = 1 Session.
Quản lý: audio buffer, chat history, trạng thái.
"""

import uuid
import struct
import math
import logging

from app.config import AppConfig
from app.audio.opus_codec import OpusDecoder
from app.mcp import MCPToolRegistry
from app.services.intent import IntentDetectorService
from app.services.stt import STTService
from app.services.llm import LLMService
from app.services.tts import TTSService
from app.services.pipeline import ConversationPipeline

logger = logging.getLogger(__name__)


class Session:
    """State cho 1 phiên kết nối client."""

    def __init__(self, config: AppConfig, device_id: str, client_id: str):
        self.session_id = str(uuid.uuid4())
        self.device_id = device_id
        self.client_id = client_id

        # Audio
        self._decoder = OpusDecoder(config.audio_input)
        self._pcm_buffer = bytearray()

        # Services
        stt = STTService(config.stt)
        llm = LLMService(config.llm)
        intent_llm = LLMService(config.intent_llm)
        intent_detector = IntentDetectorService(intent_llm)
        tts = TTSService(config.tts, config.audio_output)
        mcp_tools = MCPToolRegistry()
        self.pipeline = ConversationPipeline(
            stt,
            llm,
            tts,
            intent_detector=intent_detector,
            mcp_tools=mcp_tools,
        )

        # State
        self.chat_history: list[dict] = []
        self.is_speaking = False
        self.aborted = False

        self._max_history = config.max_chat_history

        # VAD (Voice Activity Detection)
        self._silent_frames = 0  # Số frames im lặng liên tiếp
        self._has_speech = False  # Đã xác nhận giọng nói chưa
        self._speech_frames = 0  # Số frames có năng lượng cao (đếm để xác nhận)

    @property
    def buffer_size(self) -> int:
        """Kích thước buffer PCM hiện tại (bytes)."""
        return len(self._pcm_buffer)

    def reset_audio_buffer(self) -> None:
        """Xóa buffer audio, chuẩn bị nhận recording mới."""
        self._pcm_buffer = bytearray()
        self._silent_frames = 0
        self._has_speech = False
        self._speech_frames = 0
        self.aborted = False

    def append_audio(self, opus_data: bytes) -> bytes | None:
        """Decode 1 Opus frame, thêm PCM vào buffer, trả về PCM để phân tích."""
        if self.aborted:
            return None
        try:
            pcm = self._decoder.decode(opus_data)
            self._pcm_buffer.extend(pcm)
            return pcm
        except Exception as e:
            logger.error(f"[{self.device_id}] Opus decode error: {e}")
            return None

    def check_vad(
        self,
        pcm: bytes,
        speech_threshold: int = 2500,
        silence_threshold: int = 2000,
        speech_frames_needed: int = 8,
        silence_frames_needed: int = 10,
    ) -> str:
        """
        Phân tích năng lượng âm thanh, trả về trạng thái.

        Yêu cầu ít nhất `speech_frames_needed` frames có RMS > speech_threshold
        để xác nhận có người nói thật. Sau đó, nếu RMS < silence_threshold
        trong `silence_frames_needed` frames liên tiếp → trigger STT.

        Returns:
            'speech': Đang nói
            'silence_after_speech': Im lặng sau khi đã nói → trigger STT
            'silence': Im lặng (chưa nói gì)
        """
        rms = self._calc_rms(pcm)

        if rms > speech_threshold:
            self._silent_frames = 0
            self._speech_frames += 1
            if self._speech_frames >= speech_frames_needed:
                self._has_speech = True
            return 'speech'
        elif rms > silence_threshold:
            self._silent_frames = 0
            return 'speech' if self._has_speech else 'silence'
        else:
            self._silent_frames += 1
            if self._has_speech and self._silent_frames >= silence_frames_needed:
                return 'silence_after_speech'
            return 'silence'

    @property
    def has_speech(self) -> bool:
        return self._has_speech

    @staticmethod
    def _calc_rms(pcm: bytes) -> float:
        """Tính RMS (Root Mean Square) của PCM int16."""
        if len(pcm) < 2:
            return 0.0
        n_samples = len(pcm) // 2
        samples = struct.unpack(f'<{n_samples}h', pcm[:n_samples * 2])
        if not samples:
            return 0.0
        sum_sq = sum(s * s for s in samples)
        return math.sqrt(sum_sq / n_samples)

    def take_audio_buffer(self) -> bytes:
        """Lấy toàn bộ PCM buffer và xóa."""
        data = bytes(self._pcm_buffer)
        self._pcm_buffer = bytearray()
        return data

    def save_history(self, user_text: str, assistant_text: str) -> None:
        """Lưu 1 lượt hội thoại vào history."""
        self.chat_history.append({"role": "user", "content": user_text})
        self.chat_history.append({"role": "assistant", "content": assistant_text})
        # Giới hạn kích thước
        if len(self.chat_history) > self._max_history:
            self.chat_history = self.chat_history[-self._max_history :]

    def abort(self) -> None:
        """Đánh dấu abort — dừng phát audio."""
        self.aborted = True
        self.is_speaking = False



_active_sessions: dict[str, Session] = {}


def create_session(config: AppConfig, device_id: str, client_id: str) -> Session:
    """Tạo session mới và lưu vào registry."""
    session = Session(config, device_id, client_id)
    _active_sessions[session.session_id] = session
    logger.info(f"[{device_id}] Session created: {session.session_id}")
    return session


def remove_session(session_id: str) -> None:
    """Xóa session khi client disconnect."""
    removed = _active_sessions.pop(session_id, None)
    if removed:
        logger.info(f"[{removed.device_id}] Session removed: {session_id}")


def get_all_sessions() -> list[Session]:
    """Lấy danh sách tất cả sessions đang active."""
    return list(_active_sessions.values())
