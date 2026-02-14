"""
Speech-to-Text service.

Mặc định dùng Groq Whisper (nhanh + free + chính xác tiếng Việt).
Đổi provider trong config.py → STTConfig.provider.

Các provider hỗ trợ:
  - "groq": Groq Whisper API (nhanh nhất, free 7000 req/ngày)
  - "openai": OpenAI Whisper API (chính xác, trả phí)

Lấy API key:
  - Groq: https://console.groq.com/keys → set GROQ_API_KEY
  - OpenAI: https://platform.openai.com/api-keys → set OPENAI_API_KEY
"""

import io
import os
import wave
import logging
import tempfile
from typing import Optional

import openai

from app.config import STTConfig

logger = logging.getLogger(__name__)

MIN_PCM_BYTES = 16000 


class STTService:
    """Chuyển audio PCM thành text qua Whisper API (Groq/OpenAI)."""

    def __init__(self, cfg: STTConfig):
        self._client = openai.AsyncOpenAI(
            api_key=cfg.api_key,
            base_url=cfg.base_url,
        )
        self._model = cfg.model
        self._language = cfg.language
        logger.info(f"STT provider: {cfg.provider} | model: {cfg.model}")

    async def transcribe(self, pcm_data: bytes, sample_rate: int = 16000) -> Optional[str]:
        """PCM int16 mono → text. Returns None nếu quá ngắn hoặc lỗi."""
        if len(pcm_data) < MIN_PCM_BYTES:
            logger.debug("Audio quá ngắn, bỏ qua")
            return None

        wav_bytes = _pcm_to_wav(pcm_data, sample_rate)
        return await self._call_api(wav_bytes)

    async def _call_api(self, wav_bytes: bytes) -> Optional[str]:
        """Gửi WAV lên Whisper API (Groq hoặc OpenAI), trả text."""
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                tmp.write(wav_bytes)
                tmp_path = tmp.name

            with open(tmp_path, "rb") as f:
                result = await self._client.audio.transcriptions.create(
                    model=self._model,
                    file=f,
                    language=self._language,
                )
            text = result.text.strip()
            logger.info(f"\033[92m📝 STT result: {text}\033[0m")
            return text or None

        except Exception as e:
            logger.error(f"STT API error: {e}")
            return None

        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)


def _pcm_to_wav(pcm_data: bytes, sample_rate: int) -> bytes:
    """Đóng gói PCM int16 mono thành WAV."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_data)
    return buf.getvalue()
