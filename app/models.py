"""
Pydantic models cho JSON messages giữa ESP32 và server.

Mỗi model tương ứng 1 loại message trong protocol.
"""

from pydantic import BaseModel, Field
from typing import Optional


# ── Client → Server ──────────────────────────────────────────


class AudioParams(BaseModel):
    format: str = "opus"
    sample_rate: int = 16000
    channels: int = 1
    frame_duration: int = 60


class ClientHello(BaseModel):
    type: str = "hello"
    version: int = 1
    transport: str = "websocket"
    features: dict = Field(default_factory=dict)
    audio_params: AudioParams = Field(default_factory=AudioParams)


class ListenMessage(BaseModel):
    type: str = "listen"
    state: str  # "start" | "stop" | "detect"
    mode: str = "auto"  # "auto" | "manual" | "realtime"
    text: Optional[str] = None  # wake word text (khi state="detect")


class AbortMessage(BaseModel):
    type: str = "abort"
    reason: str = "none"


# ── Server → Client ──────────────────────────────────────────


class ServerHello(BaseModel):
    type: str = "hello"
    transport: str = "websocket"
    session_id: str
    audio_params: AudioParams


class TTSMessage(BaseModel):
    type: str = "tts"
    state: str  # "start" | "stop" | "sentence_start"
    session_id: str
    text: Optional[str] = None  # chỉ dùng khi state="sentence_start"


class STTMessage(BaseModel):
    type: str = "stt"
    text: str
    session_id: str


class LLMMessage(BaseModel):
    type: str = "llm"
    emotion: str  # "happy" | "sad" | "neutral" | ...
    session_id: str


# ── REST API ──────────────────────────────────────────────────


class SessionInfo(BaseModel):
    session_id: str
    device_id: str
    client_id: str
    is_speaking: bool
    history_length: int


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str = "1.0.0"
    active_sessions: int = 0
