"""
Opus encoder/decoder cho giao tiếp audio với ESP32.

ESP32 gửi: Opus 16kHz mono 60ms/frame
Server trả: Opus 24kHz mono 60ms/frame
"""

import logging
from app.config import AudioInputConfig, AudioOutputConfig

import opuslib

logger = logging.getLogger(__name__)


class OpusDecoder:
    """Decode Opus từ ESP32 → PCM int16."""

    def __init__(self, cfg: AudioInputConfig):
        self._decoder = opuslib.Decoder(fs=cfg.sample_rate, channels=cfg.channels)
        self._frame_size = cfg.frame_size

    def decode(self, opus_data: bytes) -> bytes:
        """Decode 1 frame Opus → PCM int16 bytes."""
        return self._decoder.decode(opus_data, self._frame_size)


class OpusEncoder:
    """Encode PCM int16 → Opus để gửi về ESP32."""

    def __init__(self, cfg: AudioOutputConfig):
        self._encoder = opuslib.Encoder(
            fs=cfg.sample_rate,
            channels=cfg.channels,
            application=opuslib.APPLICATION_AUDIO,
        )
        # Tăng bitrate để giảm rè (mặc định quá thấp cho TTS)
        self._encoder.bitrate = 32000
        self._frame_size = cfg.frame_size
        self._frame_bytes = cfg.frame_size * 2  # 2 bytes per int16 sample

    @property
    def frame_bytes(self) -> int:
        """Số bytes PCM cần cho 1 frame."""
        return self._frame_bytes

    def encode(self, pcm_data: bytes) -> bytes:
        """Encode 1 frame PCM int16 → Opus bytes."""
        return self._encoder.encode(pcm_data, self._frame_size)

    def encode_all(self, pcm_data: bytes) -> list[bytes]:
        """Chia PCM thành frames và encode tất cả → list Opus frames."""
        frames = []
        offset = 0
        while offset + self._frame_bytes <= len(pcm_data):
            chunk = pcm_data[offset : offset + self._frame_bytes]
            frames.append(self.encode(chunk))
            offset += self._frame_bytes
        return frames
