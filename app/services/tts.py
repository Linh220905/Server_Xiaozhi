"""
Text-to-Speech service using Piper TTS (local, fast).

Pipeline: Text → Piper (PCM int16) → resample → Opus frames.
Streaming per-chunk để giảm giật giữa các câu.
"""

import asyncio
import logging
import math
import shutil
import time
from math import gcd
from pathlib import Path
from queue import Queue
from typing import AsyncGenerator

import numpy as np
from piper import PiperVoice
from scipy.signal import resample_poly

from app.config import AudioOutputConfig, TTSConfig
from app.audio.opus_codec import OpusEncoder

logger = logging.getLogger(__name__)


class TTSService:
    """Chuyển text thành Opus audio frames dùng Piper TTS."""

    def __init__(self, tts_cfg: TTSConfig, audio_cfg: AudioOutputConfig):
        model_path = Path(tts_cfg.model_path)
        if not model_path.is_absolute():
            model_path = Path(__file__).resolve().parent.parent.parent / model_path

        logger.info(f"Loading Piper TTS model: {model_path}")
        self._voice = PiperVoice.load(str(model_path))
        logger.info(
            f"Piper TTS loaded — sample_rate={self._voice.config.sample_rate}Hz"
        )

        self._speaker_id = tts_cfg.speaker_id
        self._speed = tts_cfg.speed
        self._voice_style = (tts_cfg.voice_style or "normal").strip().lower()
        self._target_rate = audio_cfg.sample_rate  # 24000
        self._source_rate = self._voice.config.sample_rate  # 22050
        self._need_resample = self._source_rate != self._target_rate

        # Tính tỉ lệ resample: 24000/22050 = 160/147
        if self._need_resample:
            g = gcd(self._target_rate, self._source_rate)
            self._up = self._target_rate // g    # 160
            self._down = self._source_rate // g  # 147

        self._encoder = OpusEncoder(audio_cfg)
        self._frame_bytes = self._encoder.frame_bytes
        self._frame_duration_s = audio_cfg.frame_duration_ms / 1000.0

        # State cho hiệu ứng robot để tránh pop/crack giữa các chunk
        self._robot_phase = 0.0
        self._robot_lp_prev = 0.0

        self._style_profiles = {
            "normal": {"enabled": False},
            "robot": {
                "enabled": True,
                "mod_hz": 95.0,
                "mix": 0.72,
                "lp_hz": 3000.0,
            },
            "robot_soft": {
                "enabled": True,
                "mod_hz": 75.0,
                "mix": 0.55,
                "lp_hz": 3600.0,
            },
            "robot_deep": {
                "enabled": True,
                "mod_hz": 58.0,
                "mix": 0.8,
                "lp_hz": 2500.0,
            },
        }

        if self._voice_style not in self._style_profiles:
            logger.warning(
                f"Unknown TTS voice_style='{self._voice_style}', fallback to 'normal'"
            )
            self._voice_style = "normal"

        logger.info(f"TTS voice_style: {self._voice_style}")

        # Pre-tạo SynthesisConfig 1 lần
        from piper.config import SynthesisConfig
        self._syn_cfg = SynthesisConfig(
            speaker_id=self._speaker_id,
            length_scale=1.0 / self._speed if self._speed else 1.0,
        )

    @property
    def frame_duration_s(self) -> float:
        """Thời lượng 1 Opus frame (giây)."""
        return self._frame_duration_s

    async def synthesize(self, text: str) -> AsyncGenerator[bytes, None]:
        """
        Text → Opus frames (streaming per Piper chunk).
        Yield opus frames ngay khi có đủ dữ liệu, không đợi hết câu.
        """
        if not text or not text.strip():
            return

        loop = asyncio.get_running_loop()
        pcm_buffer = bytearray()
        started_at = time.perf_counter()
        first_frame_at: float | None = None
        total_pcm_bytes = 0
        total_frames = 0

        try:
            # Chạy Piper ở thread riêng và stream PCM chunks về async loop.
            queue: Queue[bytes | BaseException | object] = Queue(maxsize=8)
            _DONE = object()

            def producer() -> None:
                try:
                    for audio_chunk in self._voice.synthesize(text, syn_config=self._syn_cfg):
                        pcm = audio_chunk.audio_int16_bytes
                        if pcm:
                            queue.put(pcm)
                except BaseException as e:
                    queue.put(e)
                finally:
                    queue.put(_DONE)

            producer_future = loop.run_in_executor(None, producer)

            while True:
                item = await loop.run_in_executor(None, queue.get)
                if item is _DONE:
                    break
                if isinstance(item, BaseException):
                    raise item

                pcm_chunk = item
                if self._need_resample:
                    pcm_chunk = self._resample(pcm_chunk)

                pcm_chunk = self._apply_voice_style(pcm_chunk)
                total_pcm_bytes += len(pcm_chunk)

                pcm_buffer.extend(pcm_chunk)

                # Yield opus frames ngay khi đủ 1 frame
                while len(pcm_buffer) >= self._frame_bytes:
                    frame_data = bytes(pcm_buffer[: self._frame_bytes])
                    pcm_buffer = pcm_buffer[self._frame_bytes :]
                    total_frames += 1
                    if first_frame_at is None:
                        first_frame_at = time.perf_counter()
                    yield self._encoder.encode(frame_data)

            await producer_future

            # Pad và encode phần còn lại
            if len(pcm_buffer) > 0:
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
                "TTS timing | chars=%d frames=%d first_frame=%.1fms total=%.3fs audio=%.3fs rtf=%.2f model=%s style=%s",
                len(text),
                total_frames,
                first_frame_ms,
                elapsed,
                audio_seconds,
                rtf,
                Path(self._voice.config.model_path).name if getattr(self._voice.config, "model_path", None) else "(loaded)",
                self._voice_style,
            )

        except Exception as e:
            logger.error(f"Piper TTS error: {e}", exc_info=True)

    async def stream_audio_url(self, url: str) -> AsyncGenerator[bytes, None]:
        """Stream audio từ URL (ví dụ preview mp3) -> Opus frames 24kHz mono."""
        if not url:
            return

        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            logger.warning("ffmpeg not found, cannot stream audio url")
            return

        cmd = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-reconnect",
            "1",
            "-reconnect_streamed",
            "1",
            "-reconnect_delay_max",
            "3",
            "-i",
            url,
            "-f",
            "s16le",
            "-ac",
            "1",
            "-ar",
            str(self._target_rate),
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
                logger.warning("ffmpeg exited with code %s: %s", process.returncode, err.decode("utf-8", errors="ignore"))
            logger.info("Music preview streamed: %s frames", frame_count)
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
        """Tìm và phát full audio theo query (ưu tiên YouTube qua yt-dlp)."""
        if not query:
            return

        audio_url = await self._resolve_audio_url_from_youtube(query)
        if not audio_url:
            logger.warning("Cannot resolve full-song url for query: %s", query)
            return

        async for frame in self.stream_audio_url(audio_url):
            yield frame

    async def _resolve_audio_url_from_youtube(self, query: str) -> str | None:
        """Dùng yt-dlp lấy direct audio URL cho query."""
        ytdlp = shutil.which("yt-dlp")
        if not ytdlp:
            logger.warning("yt-dlp not found, full-song streaming unavailable")
            return None

        search_query = f"ytsearch1:{query} official audio"
        cmd = [
            ytdlp,
            "-f",
            "bestaudio/best",
            "-g",
            "--no-playlist",
            search_query,
        ]

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            logger.warning(
                "yt-dlp failed (%s): %s",
                process.returncode,
                (stderr or b"").decode("utf-8", errors="ignore"),
            )
            return None

        url = (stdout or b"").decode("utf-8", errors="ignore").strip().splitlines()
        return url[0].strip() if url else None

    def _resample(self, pcm_data: bytes) -> bytes:
        """Resample PCM int16 dùng polyphase filter (nhanh hơn FFT)."""
        samples = np.frombuffer(pcm_data, dtype=np.int16).astype(np.float32)
        resampled = resample_poly(samples, self._up, self._down)
        return np.clip(resampled, -32768, 32767).astype(np.int16).tobytes()

    def _apply_voice_style(self, pcm_data: bytes) -> bytes:
        """Áp hiệu ứng giọng nói theo `voice_style` (normal/robot*)."""
        profile = self._style_profiles[self._voice_style]
        if not profile.get("enabled", False):
            return pcm_data

        x = np.frombuffer(pcm_data, dtype=np.int16).astype(np.float32)
        if x.size == 0:
            return pcm_data

        # Normalize về [-1, 1]
        dry = x / 32768.0

        # Ring-modulation kiểu robot: carrier hình vuông để accent robotic rõ.
        mod_hz = float(profile["mod_hz"])
        mix = float(profile["mix"])
        lp_hz = float(profile["lp_hz"])

        phase_inc = 2.0 * math.pi * mod_hz / float(self._target_rate)
        idx = np.arange(dry.size, dtype=np.float32)
        phase = self._robot_phase + idx * phase_inc
        carrier = np.sign(np.sin(phase))
        carrier[carrier == 0] = 1.0
        wet = dry * carrier

        # 1-pole low-pass để bớt chói/cắt gắt
        dt = 1.0 / float(self._target_rate)
        rc = 1.0 / (2.0 * math.pi * max(lp_hz, 10.0))
        alpha = dt / (rc + dt)

        y = np.empty_like(wet)
        prev = self._robot_lp_prev
        for i, sample in enumerate(wet):
            prev = prev + alpha * (float(sample) - prev)
            y[i] = prev

        self._robot_lp_prev = prev
        self._robot_phase = float((phase[-1] + phase_inc) % (2.0 * math.pi))

        out = (1.0 - mix) * dry + mix * y
        out = np.clip(out * 32768.0, -32768, 32767).astype(np.int16)
        return out.tobytes()
