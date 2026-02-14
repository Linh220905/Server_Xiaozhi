# XiaoZhi ESP32 - Custom Server Hướng Dẫn Chi Tiết

## 📌 Mục đích

Server WebSocket tối giản để giao tiếp với client **xiaozhi-esp32**.  
Mục tiêu: hiểu rõ **từng điểm cần can thiệp** khi muốn tự build server riêng.

---

## 🏗️ Kiến trúc tổng quan

```
┌──────────────────────────────────────────────────────────────────────┐
│                        ESP32 Client (xiaozhi-esp32)                  │
│                                                                      │
│  MIC → Audio Processor → Opus Encoder → ──── WebSocket ────→ Server │
│  Speaker ← Opus Decoder ← ──────────── WebSocket ←──────── Server  │
│  Display ← ─────────────── JSON text ←─────────────────── Server   │
└──────────────────────────────────────────────────────────────────────┘
         │                                           │
         │  1. hello (JSON)                          │
         │  2. listen start/stop (JSON)              │
         │  3. binary audio frames (Opus 60ms)       │
         │  4. abort (JSON)                          │
         │                                           │
         │          Server trả về:                   │
         │  1. hello response (JSON)                 │
         │  2. stt result (JSON)                     │
         │  3. tts start/sentence_start/stop (JSON)  │
         │  4. binary audio frames (Opus 60ms)       │
         │  5. llm emotion (JSON)                    │
```

---

## 📁 Cấu trúc file

```
custom-server/
├── server.py           ← Toàn bộ server trong 1 file (có comment chi tiết)
├── requirements.txt    ← Dependencies
└── README.md           ← File này
```

---

## 🚀 Cách chạy

### 1. Cài dependencies

```bash
cd custom-server
pip install -r requirements.txt
```

Ngoài ra cần cài **ffmpeg** (dùng cho TTS convert MP3 → PCM):
```bash
# Ubuntu/Debian
sudo apt install ffmpeg

# macOS
brew install ffmpeg
```

Và cần cài **libopus** (cho opuslib):
```bash
# Ubuntu/Debian
sudo apt install libopus-dev

# macOS
brew install opus
```

### 2. Đặt API key

```bash
export OPENAI_API_KEY="sk-your-key-here"
# Nếu dùng provider khác (Ollama, vLLM...):
export OPENAI_BASE_URL="http://localhost:11434/v1"
```

### 3. Chạy server

```bash
python server.py
```

Server sẽ lắng nghe tại `ws://0.0.0.0:8000`

### 4. Trỏ ESP32 client về server

Trên ESP32, khi device gọi OTA check version, server OTA trả config chứa WebSocket URL. Bạn cần sửa OTA response để trả:

```json
{
  "websocket": {
    "url": "ws://YOUR_SERVER_IP:8000",
    "token": "your-token"
  }
}
```

Hoặc dùng MQTT config tùy protocol bạn chọn.

---

## 🔍 Giải thích chi tiết từng phần code

### PHẦN 1: CONFIG (dòng 68-87)

```python
CONFIG = {
    "server": {"host": "0.0.0.0", "port": 8000},
    "audio": {
        "input_sample_rate": 16000,    # Client gửi 16kHz
        "output_sample_rate": 24000,   # Server trả 24kHz
        "input_frame_duration_ms": 60, # Mỗi frame 60ms
    },
    "openai": {...},  # API cho STT và LLM
    "tts": {"voice": "vi-VN-HoaiMyNeural"},  # Giọng TTS
}
```

**Can thiệp:**
- Đổi `output_sample_rate` thành 16000 nếu muốn tiết kiệm bandwidth
- Đổi `voice` để thay giọng nói
- Đổi `base_url` để trỏ đến Ollama/vLLM local

---

### PHẦN 2: OPUS ENCODER/DECODER (dòng 93-136)

Client ESP32 **chỉ hỗ trợ Opus codec**. Server cần:
- **Decoder**: nhận Opus từ client → PCM để làm STT
- **Encoder**: PCM từ TTS → Opus gửi về client

```python
class OpusHelper:
    def decode_opus_to_pcm(self, opus_data: bytes) -> bytes:
        # 1 frame Opus 60ms → 960 samples PCM 16-bit
    
    def encode_pcm_to_opus(self, pcm_data: bytes) -> bytes:
        # 1 frame PCM → 1 gói Opus
```

**Can thiệp:** Không cần thay đổi vì client chỉ hỗ trợ Opus.

---

### PHẦN 3: STT - Speech To Text (dòng 146-198)

Nhận PCM audio → gọi API → trả text.

```python
class SimpleSTT:
    async def transcribe(self, pcm_data, sample_rate=16000) -> str:
        # PCM → WAV → gửi lên Whisper API → text
```

**Luồng:** Batch mode (đợi user nói xong mới xử lý)

**Can thiệp chính:**
| Muốn làm | Sửa ở đâu |
|-----------|-----------|
| Dùng Whisper local | Thay body `transcribe()`, import `faster_whisper` |
| Dùng Google Speech | Thay API call |
| Streaming STT (giảm latency) | Nhận audio frame → gửi real-time, nhận partial text |
| Đổi ngôn ngữ | Thay `language="vi"` thành ngôn ngữ khác |

---

### PHẦN 4: LLM - Large Language Model (dòng 208-261)

Nhận text → gọi LLM API **streaming** → yield từng chunk.

```python
class SimpleLLM:
    async def chat_stream(self, user_text, history):
        # Gọi OpenAI-compatible API với stream=True
        async for chunk in stream:
            yield chunk.choices[0].delta.content
```

**Streaming rất quan trọng!** Nếu không stream, user phải đợi toàn bộ response → latency cao.

**Can thiệp chính:**
| Muốn làm | Sửa ở đâu |
|-----------|-----------|
| Dùng Ollama | Đổi `base_url` thành `http://localhost:11434/v1` |
| Dùng Claude | Thay bằng Anthropic SDK |
| Thêm RAG | Thêm context từ vector DB vào messages |
| Thêm system prompt | Sửa `self.system_prompt` |
| Function calling | Thêm `tools` param vào `create()` |

---

### PHẦN 5: TTS - Text To Speech (dòng 272-332)

Nhận text → audio PCM → encode Opus frames.

```python
class SimpleTTS:
    async def synthesize_to_opus(self, text):
        # text → edge-tts (MP3) → ffmpeg (PCM) → Opus frames
        for opus_frame in frames:
            yield opus_frame
```

**Can thiệp chính:**
| Muốn làm | Sửa ở đâu |
|-----------|-----------|
| Giọng khác | Đổi `voice` trong CONFIG |
| Dùng VITS/Coqui | Thay toàn bộ body `synthesize_to_opus()` |
| Azure TTS | Thay edge-tts bằng Azure SDK |
| Tốc độ nói | Thêm param rate vào edge-tts |

---

### PHẦN 6: SESSION (dòng 341-364)

Quản lý trạng thái 1 kết nối:

```python
class Session:
    self.pcm_buffer = bytearray()    # Tích lũy audio từ client
    self.chat_history = []           # Lịch sử chat cho LLM
    self.is_speaking = False         # Server đang phát audio?
    self.aborted = False             # Client yêu cầu dừng?
```

**Can thiệp:** Thêm user profile, persistent memory, device registry...

---

### PHẦN 7: WEBSOCKET HANDLER (dòng 378-590) ⭐ QUAN TRỌNG NHẤT

Đây là core xử lý message. Mỗi hàm handle 1 loại message:

#### 7.1 Main loop
```python
async for message in self.ws:
    if isinstance(message, str):   → JSON message (hello/listen/abort/mcp)
    elif isinstance(message, bytes): → Binary audio (Opus frames)
```

#### 7.2 Phân loại message
```python
msg_type = msg.get("type")
if msg_type == "hello":    → _handle_hello()
elif msg_type == "listen": → _handle_listen()  
elif msg_type == "abort":  → _handle_abort()
elif msg_type == "mcp":    → _handle_mcp()
```

#### 7.3 Hello handshake ⚠️ BẮT BUỘC

```
Client gửi:                          Server PHẢI trả:
{                                    {
  "type": "hello",                     "type": "hello",
  "version": 1,                       "transport": "websocket",  ← PHẢI KHỚP
  "transport": "websocket",           "session_id": "uuid...",
  "audio_params": {                   "audio_params": {
    "format": "opus",                   "sample_rate": 24000,
    "sample_rate": 16000,               "channels": 1,
    "channels": 1,                      "frame_duration": 60
    "frame_duration": 60               }
  }                                  }
}
```

**Nếu server không trả hello đúng format trong 10 giây → client báo timeout!**

#### 7.4 Listen state machine
```
listen start → Xóa buffer, sẵn sàng nhận audio
listen stop  → Trigger pipeline: STT → LLM → TTS
listen detect → Wake word detected
```

#### 7.5 Abort
```
Client abort → Server dừng gửi audio ngay lập tức
```

#### 7.8 Pipeline STT → LLM → TTS ⭐

```python
async def _process_conversation(self):
    # 1. STT: PCM buffer → text
    user_text = await self.session.stt.transcribe(pcm_data)
    
    # 2. Gửi STT result về client (hiển thị)
    await self._send_stt(user_text)
    
    # 3. LLM streaming → tách câu → TTS → gửi audio
    await self._stream_llm_tts_response(user_text)
```

#### 7.9 Streaming response (giảm latency)

```python
async def _stream_llm_tts_response(self, user_text):
    await self._send_tts_state("start")          # Báo client bắt đầu nghe
    
    async for chunk in self.session.llm.chat_stream(...):
        sentence_buffer += chunk
        if gặp_dấu_chấm:
            await self._send_tts_sentence(câu)    # Hiển thị text
            await self._synthesize_and_send(câu)   # Gửi audio
    
    await self._send_tts_state("stop")            # Báo client xong
```

**Chiến lược tách câu:** Tích lũy token từ LLM, khi gặp dấu câu (. ! ? 。) → gửi ngay cho TTS → client bắt đầu nghe trong khi LLM vẫn sinh tiếp.

---

## 📊 So sánh với xiaozhi-esp32-server chính thức

| Tính năng | Custom Server (file này) | xiaozhi-esp32-server |
|-----------|-------------------------|---------------------|
| WebSocket | ✅ Cơ bản | ✅ Đầy đủ + MQTT |
| Hello handshake | ✅ | ✅ |
| STT | ✅ Batch (Whisper API) | ✅ Streaming + nhiều provider |
| LLM | ✅ Streaming (OpenAI) | ✅ Streaming + nhiều provider |
| TTS | ✅ edge-tts | ✅ Nhiều TTS engine |
| VAD | ❌ Chưa có | ✅ Silero VAD |
| MCP/IoT | ❌ Chưa implement | ✅ Đầy đủ |
| Auth | ❌ | ✅ JWT + device whitelist |
| Memory | ❌ | ✅ Chat memory |
| Intent | ❌ | ✅ Intent detection |
| Multi-device | ❌ | ✅ |
| Rate control | ❌ | ✅ AudioRateController |
| OTA server | ❌ | ✅ HTTP server |

---

## 🎯 Roadmap custom tiếp

### 1. Thêm VAD (Voice Activity Detection)
Để server tự detect khi user ngừng nói (mode="auto"):
```python
# pip install silero-vad
# Trong _handle_audio_data(), sau khi decode Opus:
is_voice = vad.is_speech(pcm_frame)
if was_speaking and not is_voice:
    # User ngừng nói → trigger _process_conversation()
```

### 2. Thêm Streaming STT
Thay vì đợi user nói xong mới STT:
```python
# Mỗi frame audio nhận được → gửi real-time cho STT engine
# STT trả partial result → update liên tục
# Khi user dừng → STT trả final result
```

### 3. Thêm MCP (IoT Control)
```python
# Sau hello, gửi MCP initialize:
await self._send_json({
    "type": "mcp",
    "payload": {
        "jsonrpc": "2.0",
        "method": "initialize",
        "params": {"capabilities": {}},
        "id": 1
    }
})
# Sau đó gọi tools/list để lấy danh sách tool từ device
# LLM quyết định khi nào gọi tool
```

### 4. Thêm OTA HTTP Server
ESP32 cần 1 HTTP endpoint để lấy config:
```python
# GET /xiaozhi/ota/ → trả config JSON chứa websocket URL, token...
```

---

## 📚 Tài liệu tham khảo

- [WebSocket Protocol](../xiaozhi-esp32/docs/websocket.md) - Chi tiết protocol
- [MQTT+UDP Protocol](../xiaozhi-esp32/docs/mqtt-udp.md) - Protocol thay thế
- [MCP Protocol](../xiaozhi-esp32/docs/mcp-protocol.md) - IoT control
- [MCP Usage](../xiaozhi-esp32/docs/mcp-usage.md) - Cách dùng MCP
- [xiaozhi-esp32-server](../xiaozhi-esp32-server/) - Server chính thức (tham khảo)
