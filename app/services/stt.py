import io
from app.server_logging import get_logger
import re
from typing import Optional

import openai

from app.config import STTConfig

logger = get_logger(__name__)


MIN_PCM_BYTES = 16000

# Các ngôn ngữ được phép — nếu Whisper detect ra ngôn ngữ khác sẽ thử lại
ALLOWED_LANGUAGES = ("vi", "en")


class STTService:
    """Chuyển audio PCM thành text qua Whisper API (Groq/OpenAI).
    
    Hỗ trợ cả tiếng Việt và tiếng Anh:
    - Lần 1: auto-detect (không ép language) + prompt hint tiếng Việt/Anh
    - Nếu detect ra ngôn ngữ không phải vi/en → thử lại với language="vi"
    """

BYTES_PER_SAMPLE = 2  # PCM int16
CHANNELS = 1


class STTService:
    """PCM int16 mono -> text bằng Whisper-compatible API (Groq/OpenAI)."""


    def __init__(self, cfg: STTConfig):
        self._cfg = cfg
        self._client = openai.AsyncOpenAI(
            api_key=cfg.api_key,
            base_url=cfg.base_url,
            timeout=getattr(cfg, "timeout", 20.0),
            max_retries=0,  # tự retry có kiểm soát ở dưới
        )
        self._model = cfg.model

        self._language = (cfg.language or "").strip().lower()


        # các giá trị nên thêm trong STTConfig
        self._min_audio_seconds = float(getattr(cfg, "min_audio_seconds", 0.8))
        self._max_audio_seconds = float(getattr(cfg, "max_audio_seconds", 30.0))
        self._prompt = getattr(
            cfg,
            "prompt",
            (
                "Day la hoi thoai song ngu tieng Viet va tieng Anh. "
                "Hay nhan dien chinh xac ngon ngu nguoi noi su dung theo tung cau, "
                "giu nguyen ten rieng, email, so dien thoai va thuat ngu ky thuat."
            ),
        )
        self._temperature = float(getattr(cfg, "temperature", 0.0))

        # Auto-detect when language is empty/auto to improve mixed vi/en recognition.
        if self._language in {"", "auto", "vi-en", "mixed", "multilingual"}:
            self._language = ""

        logger.info(
            "STT provider=%s model=%s language=%s min_audio_seconds=%.2f",
            cfg.provider,
            self._model,
            self._language or "auto",
            self._min_audio_seconds,
        )

    async def transcribe(
        self,
        pcm_data: bytes,
        sample_rate: int = 16000,
    ) -> Optional[str]:
        """
        PCM int16 mono -> text.
        Returns None nếu audio quá ngắn / PCM lỗi / API lỗi.
        """
        if not pcm_data:
            logger.debug("PCM rỗng, bỏ qua")
            return None

        if sample_rate <= 0:
            logger.warning("sample_rate không hợp lệ: %s", sample_rate)
            return None

        # PCM int16 phải chia hết cho 2 byte
        if len(pcm_data) % BYTES_PER_SAMPLE != 0:
            logger.warning("PCM bị lệch byte (len=%s), cắt 1 byte cuối", len(pcm_data))
            pcm_data = pcm_data[: len(pcm_data) - (len(pcm_data) % BYTES_PER_SAMPLE)]
            if not pcm_data:
                return None

        duration_sec = len(pcm_data) / (sample_rate * BYTES_PER_SAMPLE * CHANNELS)
        if duration_sec < self._min_audio_seconds:
            logger.debug("Audio quá ngắn: %.3fs < %.3fs, bỏ qua", duration_sec, self._min_audio_seconds)
            return None

        # Nếu audio dài quá mức mong muốn cho 1 chunk realtime, chỉ lấy đoạn cuối
        # để giảm latency và tránh gửi blob quá lớn.
        if duration_sec > self._max_audio_seconds:
            keep_bytes = int(self._max_audio_seconds * sample_rate * BYTES_PER_SAMPLE * CHANNELS)
            pcm_data = pcm_data[-keep_bytes:]
            duration_sec = len(pcm_data) / (sample_rate * BYTES_PER_SAMPLE * CHANNELS)
            logger.debug("Trim audio còn %.2fs", duration_sec)

        wav_bytes = _pcm_to_wav(pcm_data, sample_rate)
        text = await self._call_api(wav_bytes)


        if not text:

            return None

        text = _normalize_text(text)
        return text or None

    async def _call_api(self, wav_bytes: bytes) -> Optional[str]:
        """
        Gửi WAV lên Whisper API.
        Retry ngắn để chịu lỗi mạng tạm thời tốt hơn.
        """
        file_obj = io.BytesIO(wav_bytes)
        file_obj.name = "audio.wav"  # một số SDK/provider dùng tên file để suy format

        last_error: Optional[Exception] = None

        for attempt in range(2):
            try:
                file_obj.seek(0)

                request_kwargs = {
                    "model": self._model,
                    "file": file_obj,
                    "prompt": self._prompt,
                    "temperature": self._temperature,
                }
                if self._language:
                    request_kwargs["language"] = self._language

                result = await self._client.audio.transcriptions.create(
                    **request_kwargs
                )

                text = getattr(result, "text", "") or ""
                text = text.strip()

                if text:
                    logger.info("STT result: %s", text)
                    return text

                logger.debug("STT trả về rỗng")
                return None

            except Exception as e:
                last_error = e
                logger.warning(
                    "STT API error (attempt %s/%s): %s",
                    attempt + 1,
                    2,
                    e,
                )

        logger.error("STT failed after retries: %s", last_error)
        return None

    async def _transcribe_verbose(self, wav_path: str) -> Optional[tuple[str, str]]:
        """Transcribe với response_format=verbose_json để lấy detected language."""
        try:
            with open(wav_path, "rb") as f:
                result = await self._client.audio.transcriptions.create(
                    model=self._model,
                    file=f,
                    response_format="verbose_json",
                    prompt="Transcribe Vietnamese or English speech.",
                )
            text = result.text.strip() if result.text else ""
            detected_lang = getattr(result, "language", "vi") or "vi"
            return text, detected_lang
        except Exception as e:
            logger.error(f"STT verbose API error: {e}")
            return None

    async def _transcribe_with_lang(self, wav_path: str, language: str) -> Optional[str]:
        """Transcribe với language cố định."""
        try:
            with open(wav_path, "rb") as f:
                result = await self._client.audio.transcriptions.create(
                    model=self._model,
                    file=f,
                    language=language,
                )
            text = result.text.strip()
            logger.info(f"\033[92m📝 STT [{language}]: {text}\033[0m")
            return text or None
        except Exception as e:
            logger.error(f"STT API error ({language}): {e}")
            return None


def _pcm_to_wav(pcm_data: bytes, sample_rate: int) -> bytes:
    """Đóng gói PCM int16 mono thành WAV."""
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(BYTES_PER_SAMPLE)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_data)
    return buf.getvalue()


def _normalize_text(text: str) -> str:
    """
    Dọn text nhẹ nhàng, không phá tiếng Việt.
    """
    text = text.strip()
    text = re.sub(r"\s+", " ", text)
    return text
