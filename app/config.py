"""
Cấu hình tập trung cho toàn bộ server.

Đọc từ biến môi trường, có giá trị mặc định cho dev.
Sửa file này để đổi provider STT/LLM/TTS.
"""

import os
from pathlib import Path
from pydantic import BaseModel
from .prompt_store import SYSTEM_PROMPT


_env_path = Path(__file__).resolve().parent.parent / ".env"
if _env_path.exists():
    for line in _env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000


class AudioInputConfig(BaseModel):
    """Audio ESP32 gửi lên: Opus 16kHz mono 60ms."""
    sample_rate: int = 16000
    channels: int = 1
    frame_duration_ms: int = 60

    @property
    def frame_size(self) -> int:
        """Số samples trong 1 frame: 16000 * 60 / 1000 = 960."""
        return self.sample_rate * self.frame_duration_ms // 1000


class AudioOutputConfig(BaseModel):
    """Audio server gửi về ESP32: Opus 24kHz mono 60ms."""
    sample_rate: int = 24000
    channels: int = 1
    frame_duration_ms: int = 60

    @property
    def frame_size(self) -> int:
        """Số samples trong 1 frame: 24000 * 60 / 1000 = 1440."""
        return self.sample_rate * self.frame_duration_ms // 1000


class LLMProviderConfig(BaseModel):
    """Config cho 1 LLM provider."""
    name: str = ""  
    api_key: str = ""
    base_url: str = ""
    model: str = ""


class LLMConfig(BaseModel):
    """LLM config với fallback — thử lần lượt từng provider."""
    providers: list[LLMProviderConfig] = []
    max_tokens: int = 500
    temperature: float = 0.7
    system_prompt: str = SYSTEM_PROMPT

    @classmethod
    def from_env(
        cls,
        *,
        providers_env: str = "LLM_PROVIDERS",
        default_api_key_env: str = "OPENAI_API_KEY",
        default_base_url_env: str = "OPENAI_BASE_URL",
        default_model_env: str = "OPENAI_LLM_MODEL",
    ) -> "LLMConfig":
        providers = []
        raw = os.environ.get(providers_env, "")
        if raw:
            for entry in raw.split(";"):
                entry = entry.strip()
                if not entry:
                    continue
                parts = entry.split("|")
                if len(parts) >= 3:
                    providers.append(LLMProviderConfig(
                        name=parts[0].strip(),
                        base_url=parts[1].strip(),
                        model=parts[2].strip(),
                        api_key=parts[3].strip() if len(parts) > 3 else os.environ.get(default_api_key_env, ""),
                    ))

        if not providers:
            providers.append(LLMProviderConfig(
                name="default",
                api_key=os.environ.get(default_api_key_env, os.environ.get("OPENAI_API_KEY", "")),
                base_url=os.environ.get(default_base_url_env, os.environ.get("OPENAI_BASE_URL", "http://127.0.0.1:8045/v1")),
                model=os.environ.get(default_model_env, os.environ.get("OPENAI_LLM_MODEL", "claude-sonnet-4-5")),
            ))
        return cls(
            providers=providers,
            max_tokens=int(os.environ.get("LLM_MAX_TOKENS", "500")),
            temperature=float(os.environ.get("LLM_TEMPERATURE", "0.7")),
        )


class OpenAIConfig(BaseModel):
    """Legacy — chỉ còn dùng cho STT nếu cần."""
    api_key: str = os.environ.get("OPENAI_API_KEY", "")
    base_url: str = os.environ.get("OPENAI_BASE_URL", "http://127.0.0.1:8045/v1")
    stt_model: str = "whisper-1"


class STTConfig(BaseModel):
    """STT config — mặc định dùng Groq Whisper."""
    provider: str = "groq" 
    api_key: str = os.environ.get("GROQ_API_KEY", "")
    base_url: str = "https://api.groq.com/openai/v1"
    model: str = "whisper-large-v3-turbo" 
    language: str = "vi"


class TTSConfig(BaseModel):
    model_path: str = os.environ.get("TTS_MODEL_PATH", "models/vi_VN-vais1000-medium.onnx")
    speaker_id: int | None = int(os.environ["TTS_SPEAKER_ID"]) if os.environ.get("TTS_SPEAKER_ID") else None  # cho multi-speaker model
    speed: float = float(os.environ.get("TTS_SPEED", "0.7"))
    voice_style: str = os.environ.get("TTS_VOICE_STYLE", "normal")


class AppConfig(BaseModel):
    server: ServerConfig = ServerConfig()
    audio_input: AudioInputConfig = AudioInputConfig()
    audio_output: AudioOutputConfig = AudioOutputConfig()
    openai: OpenAIConfig = OpenAIConfig()
    llm: LLMConfig = LLMConfig()
    intent_llm: LLMConfig = LLMConfig()
    stt: STTConfig = STTConfig()
    tts: TTSConfig = TTSConfig()
    max_chat_history: int = 20


_intent_provider_env = os.environ.get("INTENT_LLM_PROVIDERS", "").strip()
if _intent_provider_env:
    _intent_llm_cfg = LLMConfig.from_env(
        providers_env="INTENT_LLM_PROVIDERS",
        default_api_key_env="INTENT_LLM_API_KEY",
        default_base_url_env="INTENT_LLM_BASE_URL",
        default_model_env="INTENT_LLM_MODEL",
    )
else:
    # Mặc định dùng cùng provider chain với LLM chính để tránh rơi về endpoint local không tương thích.
    _intent_llm_cfg = LLMConfig.from_env()

config = AppConfig(
    llm=LLMConfig.from_env(),
    intent_llm=_intent_llm_cfg,
)
