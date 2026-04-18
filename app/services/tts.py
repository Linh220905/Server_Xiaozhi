"""
Text-to-Speech service using Google Cloud TTS API.

Improved pipeline:
Text -> normalize -> split mixed-language segments -> SSML -> Google Cloud TTS (LINEAR16 WAV)
-> strip WAV header -> PCM int16 24kHz -> Opus frames

Goals:
- Vietnamese and English pronunciation clearer
- More natural pauses and prosody
- Better handling of mixed vi/en text
- Better English pronunciation for technical/product terms
- Backward-compatible with old config fields where possible
"""

from __future__ import annotations

import asyncio
import base64
import html
from app.server_logging import get_logger
import math
import os
import re
import shutil
import struct
import time
from typing import AsyncGenerator

import aiohttp

try:
    import edge_tts  # type: ignore
except Exception:
    edge_tts = None

from app.audio.opus_codec import OpusEncoder
from app.config import AudioOutputConfig, TTSConfig

logger = get_logger(__name__)


VI_CHAR_RE = re.compile(
    r"[ăâđêôơưĂÂĐÊÔƠƯ"
    r"áàảãạấầẩẫậắằẳẵặ"
    r"éèẻẽẹếềểễệ"
    r"íìỉĩị"
    r"óòỏõọốồổỗộớờởỡợ"
    r"úùủũụứừửữự"
    r"ýỳỷỹỵ]"
)

ASCII_WORD_RE = re.compile(r"[A-Za-z]+(?:[A-Za-z0-9._+-]*[A-Za-z0-9]+)?")
TIME_RE = re.compile(r"\b([01]?\d|2[0-3])[:h]([0-5]\d)\b")
DATE_SLASH_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b")
MULTISPACE_RE = re.compile(r"\s+")
ELLIPSIS_RE = re.compile(r"\.{3,}")
REPEATED_PUNCT_RE = re.compile(r"([!?]){2,}")
EMOTION_TAG_RE = re.compile(r"\[emotion:[^\]]+\]\s*", re.IGNORECASE)

CLAUSE_SPLIT_RE = re.compile(r"([^.!?;:\n]+[.!?;:\n]?)", re.UNICODE)

EN_PHRASE_RE = re.compile(
    r"""
    (?:
        \b(?:OpenAI|ChatGPT|Realtime|WebSocket|Google|Gemini|Python|TypeScript|JavaScript|
        PostgreSQL|MongoDB|MySQL|Redis|Kubernetes|Docker|Linux|Windows|macOS|
        API|SDK|HTTP|HTTPS|TCP|UDP|URL|URI|JSON|HTML|CSS|SQL|NoSQL|GPU|CPU|
        AI|ML|LLM|ASR|TTS|NLP|GPT|OAuth|JWT|REST|GraphQL)\b
        (?:[ -]+\b[A-Za-z][A-Za-z0-9.+/_:-]*\b){0,6}
    )
    |
    (?:
        \b[A-Za-z][A-Za-z0-9.+/_:-]*\b
        (?:[ -]+\b[A-Za-z][A-Za-z0-9.+/_:-]*\b){1,8}
    )
    """,
    re.VERBOSE,
)

ALL_CAPS_TOKEN_RE = re.compile(r"\b[A-Z]{2,10}\b")
TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9+._:/#-]*")

MAX_TTS_INPUT_CHARS = 4200


# Cụm từ nên đọc theo cụm, không tách lẻ
EN_PHRASE_PRIORITY = [
    "OpenAI Realtime API",
    "Realtime API",
    "Google Cloud",
    "Text to Speech",
    "Speech to Text",
    "WebSocket",
    "TypeScript",
    "JavaScript",
    "PostgreSQL",
    "MongoDB",
    "MySQL",
    "Redis",
    "GraphQL",
    "REST API",
    "JSON Web Token",
    "machine learning",
    "large language model",
]

# Map cụm/từ sang dạng dễ đọc hơn cho TTS en-US
# Không dùng IPA để tránh lỗi SSML/phụ thuộc voice; ưu tiên text thay thế ổn định hơn.
EN_ALIAS_MAP: dict[str, str] = {
    "OpenAI": "Open A I",
    "ChatGPT": "Chat G P T",
    "GPT": "G P T",
    "AI": "A I",
    "ML": "M L",
    "LLM": "L L M",
    "NLP": "N L P",
    "ASR": "A S R",
    "TTS": "T T S",
    "API": "A P I",
    "SDK": "S D K",
    "URL": "U R L",
    "URI": "U R I",
    "HTTP": "H T T P",
    "HTTPS": "H T T P S",
    "TCP": "T C P",
    "UDP": "U D P",
    "JWT": "J W T",
    "OAuth": "O Auth",
    "JSON": "Jason",
    "SQL": "sequel",
    "NoSQL": "no sequel",
    "PostgreSQL": "Postgres S Q L",
    "MySQL": "My S Q L",
    "MongoDB": "Mongo D B",
    "Redis": "Redis",
    "Docker": "Docker",
    "Kubernetes": "Kubernetes",
    "Linux": "Linux",
    "macOS": "mac O S",
    "TypeScript": "Type Script",
    "JavaScript": "Java Script",
    "WebSocket": "Web Socket",
    "GraphQL": "Graph Q L",
    "Gemini": "Gemini",
    "OpenAI Realtime API": "Open A I Realtime A P I",
    "Realtime API": "Realtime A P I",
    "REST API": "Rest A P I",
    "HTML": "H T M L",
    "CSS": "C S S",
    "CPU": "C P U",
    "GPU": "G P U",
}

# Token nào nên đọc character-by-character
EN_CHARACTER_TOKENS = {
    "AI", "ML", "LLM", "NLP", "ASR", "TTS", "API", "SDK", "URL", "URI",
    "HTTP", "HTTPS", "TCP", "UDP", "JWT", "HTML", "CSS", "CPU", "GPU",
    "GPT", "SQL",
}


class TTSService:
    """Convert text to Opus audio frames using Google Cloud TTS."""

    def __init__(self, tts_cfg: TTSConfig, audio_cfg: AudioOutputConfig):
        self._provider = (getattr(tts_cfg, "provider", "google") or "google").strip().lower()
        if self._provider not in {"google", "edge"}:
            self._provider = "google"

        self._api_key = getattr(tts_cfg, "google_tts_api_key", "") or ""

        legacy_voice = getattr(tts_cfg, "google_tts_voice", "") or "vi-VN-Neural2-A"
        legacy_lang = getattr(tts_cfg, "google_tts_language", "") or "vi-VN"
        legacy_speed = float(getattr(tts_cfg, "speed", 1.0) or 1.0)
        legacy_style = (getattr(tts_cfg, "voice_style", "natural") or "natural").strip().lower()

        self._voice_name_vi = getattr(tts_cfg, "google_tts_voice_vi", "") or legacy_voice
        self._voice_name_en = getattr(tts_cfg, "google_tts_voice_en", "") or "en-US-Neural2-F"

        self._language_code_vi = getattr(tts_cfg, "google_tts_language_vi", "") or legacy_lang or "vi-VN"
        self._language_code_en = getattr(tts_cfg, "google_tts_language_en", "") or "en-US"

        self._edge_voice_vi = getattr(tts_cfg, "edge_tts_voice_vi", "") or "vi-VN-HoaiMyNeural"
        self._edge_voice_en = getattr(tts_cfg, "edge_tts_voice_en", "") or "en-US-JennyNeural"
        self._edge_rate_vi = getattr(tts_cfg, "edge_tts_rate_vi", "+0%") or "+0%"
        self._edge_rate_en = getattr(tts_cfg, "edge_tts_rate_en", "+0%") or "+0%"
        self._edge_pitch_vi = getattr(tts_cfg, "edge_tts_pitch_vi", "+0Hz") or "+0Hz"
        self._edge_pitch_en = getattr(tts_cfg, "edge_tts_pitch_en", "+0Hz") or "+0Hz"

        raw_default_lang = (getattr(tts_cfg, "language", "auto") or "auto").strip().lower()
        self._default_language_hint: str | None = raw_default_lang if raw_default_lang in {"vi", "en"} else None

        self._speaking_rate_vi = float(
            getattr(tts_cfg, "speed_vi", legacy_speed if legacy_lang.startswith("vi") else 0.96) or 0.96
        )
        self._speaking_rate_en = float(getattr(tts_cfg, "speed_en", 0.89) or 0.89)

        self._pitch_vi = float(getattr(tts_cfg, "pitch_vi", 0.5) or 0.5)
        self._pitch_en = float(getattr(tts_cfg, "pitch_en", 0.0) or 0.0)

        self._volume_gain_db = float(getattr(tts_cfg, "volume_gain_db", 0.0) or 0.0)
        self._audio_profile = getattr(tts_cfg, "audio_profile", None) or "small-bluetooth-speaker-class-device"
        self._request_timeout_s = float(getattr(tts_cfg, "request_timeout_s", 30.0) or 30.0)
        self._enable_post_loudness = bool(getattr(tts_cfg, "enable_post_loudness", True))
        self._post_gain_db = float(getattr(tts_cfg, "post_gain_db", 8.0) or 0.0)
        self._target_rms = float(getattr(tts_cfg, "target_rms", 9500.0) or 9500.0)
        self._max_peak = int(getattr(tts_cfg, "max_peak", 30000) or 30000)
        self._max_boost_db = float(getattr(tts_cfg, "max_boost_db", 18.0) or 18.0)
        self._compressor_threshold = float(getattr(tts_cfg, "compressor_threshold", 0.70) or 0.70)
        self._compressor_ratio = max(1.2, float(getattr(tts_cfg, "compressor_ratio", 3.0) or 3.0))
        self._post_makeup_db = float(getattr(tts_cfg, "post_makeup_db", 10.0) or 10.0)
        self._softclip_drive = max(1.0, float(getattr(tts_cfg, "softclip_drive", 1.6) or 1.6))
        self._log_audio_stats = bool(getattr(tts_cfg, "log_audio_stats", False))

        self._voice_style = legacy_style

        if not self._api_key or self._api_key == "your-google-tts-api-key-here":
            logger.warning("Google TTS API key chưa được cấu hình. Hãy set GOOGLE_TTS_API_KEY trong .env")

        self._target_rate = audio_cfg.sample_rate
        self._encoder = OpusEncoder(audio_cfg)
        self._frame_bytes = self._encoder.frame_bytes
        self._frame_duration_s = audio_cfg.frame_duration_ms / 1000.0

        self._tts_url = f"https://texttospeech.googleapis.com/v1/text:synthesize?key={self._api_key}"

        self._base_runtime = {
            "provider": self._provider,
            "voice_name_vi": self._voice_name_vi,
            "voice_name_en": self._voice_name_en,
            "language_code_vi": self._language_code_vi,
            "language_code_en": self._language_code_en,
            "voice_style": self._voice_style,
            "default_language_hint": self._default_language_hint,
            "edge_voice_vi": self._edge_voice_vi,
            "edge_voice_en": self._edge_voice_en,
            "edge_rate_vi": self._edge_rate_vi,
            "edge_rate_en": self._edge_rate_en,
            "edge_pitch_vi": self._edge_pitch_vi,
            "edge_pitch_en": self._edge_pitch_en,
        }

        logger.info(
            "TTS initialized | provider=%s | google vi=%s(%s rate=%.2f pitch=%.2f) "
            "en=%s(%s rate=%.2f pitch=%.2f) | edge vi=%s(rate=%s pitch=%s) "
            "en=%s(rate=%s pitch=%s) | profile=%s style=%s",
            self._provider,
            self._voice_name_vi,
            self._language_code_vi,
            self._speaking_rate_vi,
            self._pitch_vi,
            self._voice_name_en,
            self._language_code_en,
            self._speaking_rate_en,
            self._pitch_en,
            self._edge_voice_vi,
            self._edge_rate_vi,
            self._edge_pitch_vi,
            self._edge_voice_en,
            self._edge_rate_en,
            self._edge_pitch_en,
            self._audio_profile,
            self._voice_style,
        )

    def apply_runtime_config(self, tts_config: dict | None = None) -> None:
        """Apply per-robot TTS overrides from robot.config.tts_config."""
        base = self._base_runtime
        self._provider = base["provider"]
        self._voice_name_vi = base["voice_name_vi"]
        self._voice_name_en = base["voice_name_en"]
        self._language_code_vi = base["language_code_vi"]
        self._language_code_en = base["language_code_en"]
        self._voice_style = base["voice_style"]
        self._default_language_hint = base["default_language_hint"]
        self._edge_voice_vi = base["edge_voice_vi"]
        self._edge_voice_en = base["edge_voice_en"]
        self._edge_rate_vi = base["edge_rate_vi"]
        self._edge_rate_en = base["edge_rate_en"]
        self._edge_pitch_vi = base["edge_pitch_vi"]
        self._edge_pitch_en = base["edge_pitch_en"]

        if not isinstance(tts_config, dict):
            return

        provider = (str(tts_config.get("provider", "")).strip().lower() or self._provider)
        if provider in {"google", "edge"}:
            self._provider = provider

        self._voice_name_vi = str(tts_config.get("google_voice_vi") or self._voice_name_vi)
        self._voice_name_en = str(tts_config.get("google_voice_en") or self._voice_name_en)
        self._edge_voice_vi = str(tts_config.get("edge_voice_vi") or self._edge_voice_vi)
        self._edge_voice_en = str(tts_config.get("edge_voice_en") or self._edge_voice_en)

        self._edge_rate_vi = str(tts_config.get("edge_rate_vi") or self._edge_rate_vi)
        self._edge_rate_en = str(tts_config.get("edge_rate_en") or self._edge_rate_en)
        self._edge_pitch_vi = str(tts_config.get("edge_pitch_vi") or self._edge_pitch_vi)
        self._edge_pitch_en = str(tts_config.get("edge_pitch_en") or self._edge_pitch_en)

        forced_lang = (str(tts_config.get("language") or "").strip().lower())
        self._default_language_hint = forced_lang if forced_lang in {"vi", "en"} else None

    @property
    def frame_duration_s(self) -> float:
        return self._frame_duration_s

    async def synthesize(
        self,
        text: str,
        *,
        language_hint: str | None = None,
    ) -> AsyncGenerator[bytes, None]:
        if not text or not text.strip():
            return

        started_at = time.perf_counter()
        first_frame_at: float | None = None
        total_frames = 0
        total_pcm_bytes = 0
        total_chunks = 0

        try:
            clean_text = self._strip_emotion_tags(text)
            if not clean_text:
                return

            normalized = self._normalize_text(clean_text)
            effective_lang_hint = language_hint or self._default_language_hint
            chunks = self._prepare_chunks(normalized, language_hint=effective_lang_hint)
            if not chunks:
                return

            if self._provider == "edge":
                for chunk in chunks:
                    total_chunks += 1
                    pcm_data = await self._synthesize_chunk_edge(chunk)
                    if not pcm_data:
                        continue

                    total_pcm_bytes += len(pcm_data)
                    pcm_buffer = bytearray(pcm_data)

                    while len(pcm_buffer) >= self._frame_bytes:
                        frame_data = bytes(pcm_buffer[: self._frame_bytes])
                        del pcm_buffer[: self._frame_bytes]
                        total_frames += 1
                        if first_frame_at is None:
                            first_frame_at = time.perf_counter()
                        yield self._encoder.encode(frame_data)

                    if pcm_buffer:
                        pcm_buffer.extend(b"\x00" * (self._frame_bytes - len(pcm_buffer)))
                        total_frames += 1
                        if first_frame_at is None:
                            first_frame_at = time.perf_counter()
                        yield self._encoder.encode(bytes(pcm_buffer))
            else:
                timeout = aiohttp.ClientTimeout(total=self._request_timeout_s)

                async with aiohttp.ClientSession(timeout=timeout) as session:
                    for chunk in chunks:
                        total_chunks += 1
                        pcm_data = await self._synthesize_chunk(session, chunk)
                        if not pcm_data:
                            continue

                        total_pcm_bytes += len(pcm_data)
                        pcm_buffer = bytearray(pcm_data)

                        while len(pcm_buffer) >= self._frame_bytes:
                            frame_data = bytes(pcm_buffer[: self._frame_bytes])
                            del pcm_buffer[: self._frame_bytes]
                            total_frames += 1
                            if first_frame_at is None:
                                first_frame_at = time.perf_counter()
                            yield self._encoder.encode(frame_data)

                        if pcm_buffer:
                            pcm_buffer.extend(b"\x00" * (self._frame_bytes - len(pcm_buffer)))
                            total_frames += 1
                            if first_frame_at is None:
                                first_frame_at = time.perf_counter()
                            yield self._encoder.encode(bytes(pcm_buffer))

            elapsed = time.perf_counter() - started_at
            first_frame_ms = (
                (first_frame_at - started_at) * 1000.0 if first_frame_at is not None else -1.0
            )
            total_samples = total_pcm_bytes / 2.0
            audio_seconds = total_samples / float(self._target_rate) if total_samples > 0 else 0.0
            rtf = (elapsed / audio_seconds) if audio_seconds > 0 else 0.0

            logger.info(
                "TTS timing | chars=%d chunks=%d frames=%d first_frame=%.1fms total=%.3fs "
                "audio=%.3fs rtf=%.2f vi_voice=%s en_voice=%s style=%s post_loudness=%s",
                len(clean_text),
                total_chunks,
                total_frames,
                first_frame_ms,
                elapsed,
                audio_seconds,
                rtf,
                self._edge_voice_vi if self._provider == "edge" else self._voice_name_vi,
                self._edge_voice_en if self._provider == "edge" else self._voice_name_en,
                self._voice_style,
                self._enable_post_loudness,
            )

        except asyncio.TimeoutError:
            logger.error("Google TTS API timeout (%.1fs)", self._request_timeout_s)
        except Exception as e:
            logger.error("TTS error: %s", e, exc_info=True)

    async def _synthesize_chunk_edge(self, chunk: dict[str, str]) -> bytes | None:
        if edge_tts is None:
            logger.error("edge-tts is not installed. Run: pip install edge-tts")
            return None

        text = chunk["text"]
        lang = chunk["lang"]
        if not text.strip():
            return None

        if lang == "en":
            voice_name = self._edge_voice_en
            rate = self._edge_rate_en
            pitch = self._edge_pitch_en
        else:
            voice_name = self._edge_voice_vi
            rate = self._edge_rate_vi
            pitch = self._edge_pitch_vi

        try:
            communicate = edge_tts.Communicate(text=text, voice=voice_name, rate=rate, pitch=pitch)
            audio_chunks: list[bytes] = []
            async for event in communicate.stream():
                if event.get("type") == "audio":
                    data = event.get("data")
                    if isinstance(data, (bytes, bytearray)):
                        audio_chunks.append(bytes(data))

            if not audio_chunks:
                logger.error("Edge TTS returned empty audio stream")
                return None

            audio_bytes = b"".join(audio_chunks)
            pcm_data = await self._decode_audio_bytes_with_ffmpeg(audio_bytes, input_format="mp3")
            if not pcm_data:
                logger.error("Failed to decode Edge TTS audio stream")
                return None
            return self._apply_loudness_chain(pcm_data, lang=lang)
        except Exception as e:
            logger.error("Edge TTS synthesis error: %s", e, exc_info=True)
            return None

    async def _decode_audio_bytes_with_ffmpeg(self, audio_bytes: bytes, *, input_format: str) -> bytes | None:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            logger.warning("ffmpeg not found, cannot decode %s TTS audio", input_format)
            return None

        process = await asyncio.create_subprocess_exec(
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            input_format,
            "-i",
            "pipe:0",
            "-f",
            "s16le",
            "-ac",
            "1",
            "-ar",
            str(self._target_rate),
            "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        out, err = await process.communicate(input=audio_bytes)
        if process.returncode != 0:
            logger.warning(
                "ffmpeg decode failed (%s): %s",
                process.returncode,
                (err or b"").decode("utf-8", errors="ignore"),
            )
            return None
        return out

    def _strip_emotion_tags(self, text: str) -> str:
        cleaned = EMOTION_TAG_RE.sub("", text or "")
        return cleaned.strip()

    async def _synthesize_chunk(
        self,
        session: aiohttp.ClientSession,
        chunk: dict[str, str],
    ) -> bytes | None:
        lang = chunk["lang"]
        text = chunk["text"]

        if not text.strip():
            return None

        if lang == "en":
            language_code = self._language_code_en
            voice_name = self._voice_name_en
            speaking_rate = self._speaking_rate_en
            pitch = self._pitch_en
        else:
            language_code = self._language_code_vi
            voice_name = self._voice_name_vi
            speaking_rate = self._speaking_rate_vi
            pitch = self._pitch_vi

        ssml_text = self._build_ssml(text=text, lang=lang)

        request_body = {
            "input": {"ssml": ssml_text},
            "voice": {
                "languageCode": language_code,
                "name": voice_name,
            },
            "audioConfig": {
                "audioEncoding": "LINEAR16",
                "sampleRateHertz": self._target_rate,
                "speakingRate": speaking_rate,
                "pitch": pitch,
                "volumeGainDb": self._volume_gain_db,
            },
        }

        if self._audio_profile:
            request_body["audioConfig"]["effectsProfileId"] = [self._audio_profile]

        async with session.post(self._tts_url, json=request_body) as resp:
            if resp.status != 200:
                error_text = await resp.text()
                logger.error(
                    "Google TTS API error %s | lang=%s | voice=%s | body=%s",
                    resp.status,
                    lang,
                    voice_name,
                    error_text,
                )
                return None

            result = await resp.json()

        audio_content_b64 = result.get("audioContent")
        if not audio_content_b64:
            logger.error("Google TTS returned empty audioContent")
            return None

        audio_content = base64.b64decode(audio_content_b64)
        pcm = self._strip_wav_header_if_needed(audio_content)
        return self._apply_loudness_chain(pcm, lang=lang)

    def _apply_loudness_chain(self, pcm_data: bytes, *, lang: str) -> bytes:
        if not pcm_data or not self._enable_post_loudness:
            return pcm_data
        if len(pcm_data) < 2:
            return pcm_data

        n_samples = len(pcm_data) // 2
        samples = list(struct.unpack(f"<{n_samples}h", pcm_data[: n_samples * 2]))
        if not samples:
            return pcm_data

        pre_rms = self._calc_rms(samples)
        pre_peak = max(abs(s) for s in samples)
        if pre_peak == 0:
            return pcm_data

        post_gain_linear = math.pow(10.0, self._post_gain_db / 20.0)
        rms_gain = (self._target_rms / pre_rms) if pre_rms > 1.0 else post_gain_linear
        max_boost_linear = math.pow(10.0, self._max_boost_db / 20.0)
        total_gain = min(post_gain_linear * rms_gain, max_boost_linear)
        total_gain = max(1.0, total_gain)

        thr = int(max(2000.0, min(32000.0, self._compressor_threshold * 32767.0)))
        ratio = self._compressor_ratio
        out: list[int] = []
        for s in samples:
            v = float(s) * total_gain
            sign = 1.0 if v >= 0 else -1.0
            av = abs(v)

            if av > thr:
                av = thr + (av - thr) / ratio

            # Soft clip nhẹ để tăng loudness cảm nhận, tránh méo cứng.
            av = 32767.0 * math.tanh((av / 32767.0) * self._softclip_drive)
            out.append(int(max(-self._max_peak, min(self._max_peak, sign * av))))

        # Makeup gain sau compressor để đẩy loudness gần target RMS.
        post_rms = self._calc_rms(out)
        post_peak_before_makeup = max(abs(s) for s in out) if out else 0
        if post_rms > 1.0 and post_peak_before_makeup > 0:
            makeup_cap = math.pow(10.0, self._post_makeup_db / 20.0)
            # Chừa headroom để không đẩy peak sát trần gây bể tiếng.
            headroom_gain = (self._max_peak * 0.97) / float(post_peak_before_makeup)
            makeup = min(self._target_rms / post_rms, makeup_cap, headroom_gain)
            if makeup > 1.0:
                out = [
                    int(max(-self._max_peak, min(self._max_peak, s * makeup)))
                    for s in out
                ]

        # Safety trim cuối: nếu peak vẫn cao, hạ đồng đều một chút để sạch tiếng.
        post_peak = max(abs(s) for s in out) if out else 0
        safe_peak = int(self._max_peak * 0.95)
        if post_peak > safe_peak and post_peak > 0:
            trim = safe_peak / float(post_peak)
            out = [int(s * trim) for s in out]

        post_rms = self._calc_rms(out)
        post_peak = max(abs(s) for s in out)
        if self._log_audio_stats:
            logger.info(
                "TTS loudness | lang=%s pre_rms=%.0f pre_peak=%d post_rms=%.0f post_peak=%d gain=%.2fx makeup_db=%.1f",
                lang,
                pre_rms,
                pre_peak,
                post_rms,
                post_peak,
                total_gain,
                self._post_makeup_db,
            )

        return struct.pack(f"<{len(out)}h", *out)

    @staticmethod
    def _calc_rms(samples: list[int]) -> float:
        if not samples:
            return 0.0
        mean = sum(samples) / len(samples)
        return math.sqrt(sum((s - mean) * (s - mean) for s in samples) / len(samples))

    def _prepare_chunks(
        self,
        text: str,
        *,
        language_hint: str | None = None,
    ) -> list[dict[str, str]]:
        forced_lang = (language_hint or "").strip().lower()
        raw_clauses: list[str] = []
        for m in CLAUSE_SPLIT_RE.finditer(text):
            clause = (m.group(0) or "").strip()
            if clause:
                raw_clauses.append(clause)

        if not raw_clauses and text.strip():
            raw_clauses = [text.strip()]

        if forced_lang in {"vi", "en"}:
            lang_chunks: list[dict[str, str]] = []
            for clause in raw_clauses:
                if lang_chunks:
                    merged = f'{lang_chunks[-1]["text"]} {clause}'.strip()
                    if len(merged) <= MAX_TTS_INPUT_CHARS:
                        lang_chunks[-1]["text"] = merged
                    else:
                        lang_chunks.append({"lang": forced_lang, "text": clause})
                else:
                    lang_chunks.append({"lang": forced_lang, "text": clause})

            final_chunks: list[dict[str, str]] = []
            for item in lang_chunks:
                for part in self._split_long_text(item["text"]):
                    cleaned = part.strip()
                    if cleaned:
                        final_chunks.append({"lang": forced_lang, "text": cleaned})
            return final_chunks

        runs: list[dict[str, str]] = []
        for clause in raw_clauses:
            clause_runs = self._split_mixed_language_runs(clause)
            for run in clause_runs:
                if not run["text"].strip():
                    continue

                if runs and runs[-1]["lang"] == run["lang"]:
                    merged = f'{runs[-1]["text"]} {run["text"]}'.strip()
                    if len(merged) <= MAX_TTS_INPUT_CHARS:
                        runs[-1]["text"] = merged
                    else:
                        runs.append(run)
                else:
                    runs.append(run)

        final_chunks: list[dict[str, str]] = []
        for item in runs:
            for part in self._split_long_text(item["text"]):
                cleaned = part.strip()
                if cleaned:
                    final_chunks.append({"lang": item["lang"], "text": cleaned})

        return final_chunks

    def _split_mixed_language_runs(self, text: str) -> list[dict[str, str]]:
        if not text.strip():
            return []

        matches = list(self._iter_english_spans(text))
        if not matches:
            return [{"lang": self._guess_language(text), "text": text.strip()}]

        runs: list[dict[str, str]] = []
        last_end = 0

        for start, end in matches:
            if start > last_end:
                vi_part = text[last_end:start].strip()
                if vi_part:
                    runs.append({"lang": "vi", "text": vi_part})

            en_part = text[start:end].strip()
            if en_part:
                runs.append({"lang": "en", "text": en_part})

            last_end = end

        if last_end < len(text):
            tail = text[last_end:].strip()
            if tail:
                runs.append({"lang": self._guess_language(tail), "text": tail})

        return self._merge_short_runs(runs)

    def _iter_english_spans(self, text: str) -> list[tuple[int, int]]:
        spans: list[tuple[int, int]] = []

        # phrase priority first
        for phrase in EN_PHRASE_PRIORITY:
            for m in re.finditer(re.escape(phrase), text, flags=re.IGNORECASE):
                spans.append((m.start(), m.end()))

        # generic english phrases
        for match in EN_PHRASE_RE.finditer(text):
            phrase = match.group(0).strip()
            if not phrase:
                continue
            words = ASCII_WORD_RE.findall(phrase)
            if not words:
                continue
            if not self._looks_like_english_phrase(phrase, words):
                continue
            spans.append((match.start(), match.end()))

        return self._merge_spans(spans)

    def _looks_like_english_phrase(self, phrase: str, words: list[str]) -> bool:
        lower_words = [w.lower() for w in words]

        strong_terms = {
            "openai", "chatgpt", "realtime", "api", "sdk", "websocket", "python",
            "typescript", "javascript", "postgresql", "mongodb", "mysql", "redis",
            "docker", "kubernetes", "linux", "windows", "macos", "json", "html",
            "css", "sql", "graphql", "oauth", "jwt", "gpu", "cpu", "ai", "ml",
            "llm", "tts", "asr", "nlp", "gpt", "http", "https", "url", "uri",
        }
        common_en = {
            "the", "and", "for", "with", "from", "this", "that", "you", "your",
            "hello", "hi", "thanks", "thank", "assistant", "voice", "stream",
            "audio", "system", "please", "install", "setup", "server", "client",
            "model", "function", "class", "error", "token", "request", "response",
            "input", "output", "deploy", "streaming",
        }

        if any(w in strong_terms for w in lower_words):
            return True
        if len(words) >= 2 and any(w in common_en for w in lower_words):
            return True
        if ALL_CAPS_TOKEN_RE.search(phrase):
            return True

        title_or_ascii = sum(1 for w in words if w[0].isupper() or w.isascii())
        if len(words) >= 2 and title_or_ascii == len(words):
            return True

        if len(words) == 1:
            token = words[0]
            if token.lower() in strong_terms:
                return True
            if token.isupper() and 2 <= len(token) <= 10:
                return True
            if token[0].isupper() and len(token) >= 4:
                return True

        return False

    def _merge_spans(self, spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
        if not spans:
            return []

        spans = sorted(spans)
        merged: list[list[int]] = [[spans[0][0], spans[0][1]]]

        for start, end in spans[1:]:
            cur = merged[-1]
            if start <= cur[1] + 1:
                cur[1] = max(cur[1], end)
            else:
                merged.append([start, end])

        return [(a, b) for a, b in merged]

    def _merge_short_runs(self, runs: list[dict[str, str]]) -> list[dict[str, str]]:
        if not runs:
            return []

        merged: list[dict[str, str]] = []
        for run in runs:
            text = run["text"].strip()
            if not text:
                continue

            if merged and merged[-1]["lang"] == run["lang"]:
                merged[-1]["text"] = f'{merged[-1]["text"]} {text}'.strip()
                continue

            if merged and len(text) <= 2 and run["lang"] == "en":
                merged[-1]["text"] = f'{merged[-1]["text"]} {text}'.strip()
                continue

            merged.append({"lang": run["lang"], "text": text})

        return merged

    def _split_long_text(self, text: str) -> list[str]:
        if len(text) <= MAX_TTS_INPUT_CHARS:
            return [text]

        pieces: list[str] = []
        current: list[str] = []
        current_len = 0

        sub_parts = re.split(r"(?<=[,.!?;:])\s+", text)
        if len(sub_parts) <= 1:
            sub_parts = text.split()

        for part in sub_parts:
            candidate = (part or "").strip()
            if not candidate:
                continue

            sep = " " if current else ""
            projected = current_len + len(sep) + len(candidate)

            if projected > MAX_TTS_INPUT_CHARS and current:
                pieces.append(" ".join(current).strip())
                current = [candidate]
                current_len = len(candidate)
            else:
                current.append(candidate)
                current_len = projected

        if current:
            pieces.append(" ".join(current).strip())

        safe_pieces: list[str] = []
        for piece in pieces:
            if len(piece) <= MAX_TTS_INPUT_CHARS:
                safe_pieces.append(piece)
                continue

            start = 0
            while start < len(piece):
                end = start + MAX_TTS_INPUT_CHARS
                safe_pieces.append(piece[start:end].strip())
                start = end

        return safe_pieces

    def _guess_language(self, text: str) -> str:
        if not text:
            return "vi"
        if VI_CHAR_RE.search(text):
            return "vi"

        words = ASCII_WORD_RE.findall(text)
        if not words:
            return "vi"

        lower_words = [w.lower() for w in words]
        strong_en = {
            "openai", "chatgpt", "realtime", "api", "sdk", "websocket", "python",
            "typescript", "javascript", "postgresql", "mongodb", "mysql", "redis",
            "docker", "kubernetes", "linux", "windows", "macos", "json", "html",
            "css", "sql", "graphql", "oauth", "jwt", "gpu", "cpu", "ai", "ml",
            "llm", "tts", "asr", "nlp", "gpt", "http", "https", "url", "uri",
        }
        common_en = {
            "the", "and", "for", "with", "from", "this", "that", "you", "your",
            "hello", "hi", "thanks", "thank", "assistant", "voice", "stream",
            "audio", "system", "please", "install", "setup", "server", "client",
            "model", "function", "class", "error", "token", "request", "response",
        }

        if any(w in strong_en for w in lower_words):
            return "en"
        if sum(1 for w in lower_words if w in common_en) >= 1 and len(words) >= 2:
            return "en"
        if ALL_CAPS_TOKEN_RE.search(text):
            return "en"

        return "vi"

    def _normalize_text(self, text: str) -> str:
        text = (text or "").strip()

        replacements = {
            "TP.HCM": "Thành phố Hồ Chí Minh",
            "Tp.HCM": "Thành phố Hồ Chí Minh",
            "tp.HCM": "Thành phố Hồ Chí Minh",
            "HN": "Hà Nội",
            "SG": "Sài Gòn",
        }
        for src, dst in replacements.items():
            text = text.replace(src, dst)

        text = ELLIPSIS_RE.sub("…", text)
        text = REPEATED_PUNCT_RE.sub(r"\1", text)

        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = re.sub(r"\n{3,}", "\n\n", text)

        text = re.sub(r"([,;:])([^\s])", r"\1 \2", text)
        text = re.sub(r"([.!?])([^\s])", r"\1 \2", text)

        text = re.sub(r"https?://\S+", " đường dẫn website ", text, flags=re.IGNORECASE)
        text = re.sub(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b", " địa chỉ email ", text, flags=re.IGNORECASE)

        text = re.sub(r"\b([A-Za-z]+)\/([A-Za-z]+)\b", r"\1 / \2", text)
        text = MULTISPACE_RE.sub(" ", text)
        return text.strip()

    def _build_ssml(self, text: str, lang: str) -> str:
        if lang == "en":
            text = self._normalize_english_pronunciation(text)

        body = self._to_inline_ssml(text=text, lang=lang)

        if lang == "en":
            rate_pct = "89%"
            pitch_st = "0st"
        else:
            rate_pct = "96%"
            pitch_st = "+1st"

        return (
            "<speak>"
            f"<prosody rate='{rate_pct}' pitch='{pitch_st}'>"
            f"{body}"
            "</prosody>"
            "</speak>"
        )

    def _normalize_english_pronunciation(self, text: str) -> str:
        """
        Replace technical terms with more speakable English forms.
        Longest phrases first to avoid partial replacement.
        """
        normalized = text

        for phrase in sorted(EN_ALIAS_MAP.keys(), key=len, reverse=True):
            replacement = EN_ALIAS_MAP[phrase]
            normalized = re.sub(
                rf"(?<![A-Za-z0-9]){re.escape(phrase)}(?![A-Za-z0-9])",
                replacement,
                normalized,
                flags=re.IGNORECASE,
            )

        normalized = MULTISPACE_RE.sub(" ", normalized).strip()
        return normalized

    def _to_inline_ssml(self, text: str, lang: str) -> str:
        parts: list[str] = []
        i = 0
        n = len(text)

        while i < n:
            m_time = TIME_RE.match(text, i)
            if m_time:
                hh = m_time.group(1)
                mm = m_time.group(2)
                parts.append(f"<say-as interpret-as='time' format='hms12'>{hh}:{mm}:00</say-as>")
                i = m_time.end()
                continue

            m_date = DATE_SLASH_RE.match(text, i)
            if m_date:
                dd, mm, yyyy = m_date.groups()
                yyyy = yyyy if len(yyyy) == 4 else f"20{yyyy}"
                parts.append(
                    f"<say-as interpret-as='date' format='dmy'>{dd.zfill(2)}/{mm.zfill(2)}/{yyyy}</say-as>"
                )
                i = m_date.end()
                continue

            ch = text[i]

            if ch == ",":
                parts.append(", <break time='90ms'/>")
                i += 1
                continue
            if ch == ";":
                parts.append("; <break time='140ms'/>")
                i += 1
                continue
            if ch == ":":
                parts.append(": <break time='120ms'/>")
                i += 1
                continue
            if ch in ".!?":
                parts.append(html.escape(ch) + " <break time='220ms'/>")
                i += 1
                continue
            if ch == "…":
                parts.append("<break time='280ms'/>")
                i += 1
                continue
            if ch == "\n":
                parts.append("<break time='260ms'/>")
                i += 1
                continue

            if ch.isalpha():
                j = i
                while j < n and (text[j].isalnum() or text[j] in "_-+./:#"):
                    j += 1
                token = text[i:j]

                upper_token = token.upper()
                if upper_token in EN_CHARACTER_TOKENS:
                    parts.append(
                        f"<say-as interpret-as='characters'>{html.escape(upper_token)}</say-as>"
                    )
                elif token.isupper() and 2 <= len(token) <= 10:
                    parts.append(
                        f"<say-as interpret-as='characters'>{html.escape(token)}</say-as>"
                    )
                else:
                    parts.append(html.escape(token))

                i = j
                continue

            parts.append(html.escape(ch))
            i += 1

        joined = "".join(parts)
        joined = re.sub(r"\s{2,}", " ", joined).strip()
        return joined

    def _strip_wav_header_if_needed(self, data: bytes) -> bytes:
        if not data:
            return b""

        if data[:4] != b"RIFF":
            return data

        idx = data.find(b"data")
        if idx >= 0 and len(data) >= idx + 8:
            return data[idx + 8:]

        return data

    async def stream_audio_url(self, url: str) -> AsyncGenerator[bytes, None]:
        if not url:
            return

        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            logger.warning("ffmpeg not found, cannot stream audio url")
            return

        is_remote = str(url).lower().startswith(("http://", "https://", "rtsp://", "ftp://"))

        if is_remote:
            cmd = [
                ffmpeg,
                "-hide_banner",
                "-loglevel", "error",
                "-reconnect", "1",
                "-reconnect_streamed", "1",
                "-reconnect_delay_max", "3",
                "-i", url,
                "-f", "s16le",
                "-ac", "1",
                "-ar", str(self._target_rate),
                "pipe:1",
            ]
        else:
            cmd = [
                ffmpeg,
                "-hide_banner",
                "-loglevel", "error",
                "-i", url,
                "-f", "s16le",
                "-ac", "1",
                "-ar", str(self._target_rate),
                "pipe:1",
            ]

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        assert process.stdout is not None
        buffer = bytearray()
        frame_count = 0

        try:
            while True:
                chunk = await process.stdout.read(8192)
                if not chunk:
                    break
                buffer.extend(chunk)

                while len(buffer) >= self._frame_bytes:
                    frame = bytes(buffer[: self._frame_bytes])
                    del buffer[: self._frame_bytes]
                    frame_count += 1
                    yield self._encoder.encode(frame)

            if buffer:
                buffer.extend(b"\x00" * (self._frame_bytes - len(buffer)))
                frame_count += 1
                yield self._encoder.encode(bytes(buffer))

            await process.wait()
            if process.returncode != 0:
                err = b""
                if process.stderr is not None:
                    err = await process.stderr.read()
                logger.warning(
                    "ffmpeg exited with code %s: %s",
                    process.returncode,
                    err.decode("utf-8", errors="ignore"),
                )

            logger.info("Music/audio preview streamed: %s frames", frame_count)

        except asyncio.CancelledError:
            process.kill()
            raise
        except Exception as e:
            logger.error("stream_audio_url error: %s", e, exc_info=True)
            process.kill()
        finally:
            if process.returncode is None:
                process.kill()

    async def stream_full_song_by_query(self, query: str) -> AsyncGenerator[bytes, None]:
        if not query:
            return

        audio_url = await self._resolve_audio_url_from_youtube(query)
        if not audio_url:
            logger.warning("Cannot resolve full-song url for query: %s", query)
            return

        async for frame in self.stream_audio_url(audio_url):
            yield frame

    async def _resolve_audio_url_from_youtube(self, query: str) -> str | None:
        ytdlp = shutil.which("yt-dlp")
        if not ytdlp:
            logger.warning("yt-dlp not found, full-song streaming unavailable")
            return None

        cookies_file = os.getenv("YTDLP_COOKIES_FILE", "").strip()
        browser_cookies = os.getenv("YTDLP_BROWSER_COOKIES", "").strip()

        common_args = [
            ytdlp,
            "-f", "bestaudio/best",
            "-g",
            "--no-playlist",
            "--no-warnings",
        ]
        if cookies_file:
            common_args.extend(["--cookies", cookies_file])
        elif browser_cookies:
            common_args.extend(["--cookies-from-browser", browser_cookies])

        attempts: list[tuple[str, list[str]]] = [
            (
                "youtube-official",
                [
                    "--extractor-args", "youtube:player_client=android,web",
                    f"ytsearch1:{query} official audio",
                ],
            ),
            (
                "youtube-generic",
                [
                    "--extractor-args", "youtube:player_client=android,web",
                    f"ytsearch1:{query}",
                ],
            ),
            (
                "soundcloud",
                [f"scsearch1:{query}"],
            ),
        ]

        last_error = ""
        for source, extra_args in attempts:
            cmd = [*common_args, *extra_args]
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await process.communicate()
            stderr_text = (stderr or b"").decode("utf-8", errors="ignore").strip()

            if process.returncode == 0:
                urls = (stdout or b"").decode("utf-8", errors="ignore").strip().splitlines()
                url = urls[0].strip() if urls else ""
                if url:
                    logger.info("yt-dlp resolved audio url via %s", source)
                    return url

            if stderr_text:
                last_error = stderr_text
                if "Sign in to confirm you\u2019re not a bot" in stderr_text or "Sign in to confirm you're not a bot" in stderr_text:
                    logger.warning(
                        "yt-dlp %s blocked by YouTube bot-check; configure YTDLP_COOKIES_FILE or YTDLP_BROWSER_COOKIES",
                        source,
                    )
                else:
                    logger.warning("yt-dlp %s failed (%s): %s", source, process.returncode, stderr_text)

        if last_error:
            logger.warning("yt-dlp exhausted all sources for query '%s'", query)
        return None
