<p align="right">
  <a href="#english-version">🇬🇧 English</a> &nbsp;|&nbsp;
  <a href="#phiên-bản-tiếng-việt">🇻🇳 Tiếng Việt</a>
</p>

---

<div align="center">
  <img src="static/asset/Nexusbot.png" alt="Nexus" width="180"/>
  <h1>Nexus</h1>
  <p><strong>Conversational AI for the Physical World</strong></p>
  <p>Give your devices a voice. Nexus turns any ESP32 into a smart, real-time voice assistant — powered by the latest AI models.</p>

  <br/>

  <a href="https://nexus.tanlinh.dev/">🌐 <strong>nexus.tanlinh.dev</strong></a>

  <br/><br/>

  ![Python](https://img.shields.io/badge/Python-3.10+-3776AB?style=for-the-badge&logo=python&logoColor=white)
  ![FastAPI](https://img.shields.io/badge/FastAPI-009688?style=for-the-badge&logo=fastapi&logoColor=white)
  ![WebSocket](https://img.shields.io/badge/WebSocket-Realtime-FF6F00?style=for-the-badge)
  ![ESP32](https://img.shields.io/badge/ESP32-Ready-E7352C?style=for-the-badge&logo=espressif&logoColor=white)

</div>

---

# English Version

## What is Nexus?

**Nexus** is a production-ready voice AI platform that connects IoT hardware to the world's best AI models — in real time. Speak to your device, and it speaks back, with human-level understanding and natural voice responses.

> 🎯 **One platform. Any device. Instant intelligence.**

### ✨ Why Nexus?

| | Feature | Description |
|---|---------|-------------|
| ⚡ | **Real-time Streaming** | Sentence-level pipeline — audio playback begins before the AI finishes thinking |
| 🧠 | **Multi-Model Support** | Plug in any LLM, STT, or TTS provider — OpenAI, Google, local models, and more |
| 🔧 | **Zero-Config Devices** | ESP32 connects via WebSocket, auto-registers, and is ready to go |
| 🛡️ | **Secure by Default** | OAuth2, JWT, session-based auth, and per-device access control |
| 📊 | **Admin Dashboard** | Web-based control panel to manage devices, users, and AI configurations |
| 🌐 | **Cloud-Native** | Deploy anywhere — VPS, cloud, or on-premise |

---

## Live Demo

> 🔗 **[nexus.tanlinh.dev](https://nexus.tanlinh.dev/)** — Official production instance

---

### Pipeline Stages

| # | Stage | What Happens |
|---|-------|-------------|
| 1 | **Capture** | ESP32 records audio from the onboard microphone and streams it over WebSocket |
| 2 | **Decode** | Server decodes the Opus/PCM stream into a clean audio buffer |
| 3 | **Transcribe (STT)** | Audio buffer is sent to the configured speech-to-text engine |
| 4 | **Think (LLM)** | Transcript + conversation history are processed by the AI model, streamed sentence-by-sentence |
| 5 | **Speak (TTS)** | Each sentence is synthesized into speech instantly as it arrives from the LLM |
| 6 | **Stream Back** | Opus-encoded audio is streamed back to the ESP32 speaker in real time |

> 💡 The entire round-trip feels **instant** — the user hears the AI respond while it's still generating.

---

## Getting Started

### Prerequisites

| Requirement | Version | Notes |
|-------------|---------|-------|
| Python | `3.10+` | Runtime |
| ffmpeg | latest | Audio transcoding |
| libopus | latest | Opus codec support |

### Installation

```bash
# Clone the repository
git clone https://github.com/your-org/nexus-server.git
cd nexus-server

# Install dependencies
pip install -r requirements.txt

# Configure your environment
cp .env.example .env
# → Edit .env with your API keys and credentials

# Launch
python run.py
```

The server starts at `http://localhost:8000` by default.

### Development Mode

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

---

## Configuration

All settings are managed via environment variables in `.env`:

| Variable | Description |
|----------|-------------|
| `SESSION_SECRET` | Encryption key for session cookies |
| `JWT_SECRET` | Signing key for JWT tokens |
| `SECRET_KEY` | Application-level secret |
| `GOOGLE_CLIENT_ID` | Google OAuth2 Client ID |
| `GOOGLE_CLIENT_SECRET` | Google OAuth2 Client Secret |
| `OPENAI_API_KEY` | API key for OpenAI services |
| `LLM_PROVIDERS` | Active LLM provider configuration |
| `ADMIN_USERNAME` | Admin dashboard login |
| `ADMIN_PASSWORD` | Admin dashboard password |

---

## Admin Dashboard

Access the built-in management interface:

| URL | Description |
|-----|-------------|
| `/dashboard/` | Device & user management dashboard |
| `/docs` | Interactive API reference (Swagger UI) |
| `/auth/google-login` | Google OAuth2 sign-in |

---

## Project Structure

```
app/
├── api/            # RESTful endpoints — auth, OAuth2, device management, OTA
├── websocket/      # Real-time WebSocket voice handler
├── services/       # AI pipeline — LLM, STT, TTS orchestration
├── mcp/            # MCP tools & alarm scheduling
├── database/       # Database connection & initialization
├── robots/         # Device registry — models & CRUD
└── auth/           # Authentication — models, security, schemas

static/
├── admin/          # Admin dashboard frontend (HTML/CSS/JS)
└── asset/          # Brand assets & media

run.py              # Application entry point
requirements.txt    # Python dependencies
.env                # Environment config (not committed)
```

---

## Roadmap

- [x] Real-time voice pipeline (STT → LLM → TTS)
- [x] Multi-provider support (OpenAI, Google, custom)
- [x] Admin dashboard with device management
- [x] Google OAuth2 integration
- [x] OTA firmware updates for ESP32
- [x] MCP tools & alarm scheduling
- [ ] Multi-language voice support expansion
- [ ] Analytics & conversation insights dashboard
- [ ] Mobile companion app

---

## License

This project is proprietary software developed by **Tan Linh**. All rights reserved. Third-party dependencies are subject to their respective licenses. For licensing inquiries, please visit [nexus.tanlinh.dev](https://nexus.tanlinh.dev/).

---

<br/>

---

# Phiên Bản Tiếng Việt

## Nexus là gì?

**Nexus** là nền tảng AI giọng nói sẵn sàng triển khai, kết nối phần cứng IoT với các mô hình AI — theo thời gian thực. Nói chuyện với thiết bị của bạn, và nó sẽ trả lời bằng giọng nói tự nhiên, với khả năng hiểu ngữ cảnh như con người.

> 🎯 **Một nền tảng. Mọi thiết bị. Trí tuệ tức thì.**

### ✨ Tại sao chọn Nexus?

| | Tính năng | Mô tả |
|---|----------|-------|
| ⚡ | **Streaming thời gian thực** | Pipeline xử lý theo câu — audio phát lại trước khi AI suy nghĩ xong |
| 🧠 | **Hỗ trợ đa mô hình** | Tích hợp bất kỳ LLM, STT, TTS nào — OpenAI, Google, model local, v.v. |
| 🔧 | **Thiết bị không cần cấu hình** | ESP32 kết nối qua WebSocket, tự đăng ký và sẵn sàng hoạt động |
| 🛡️ | **Bảo mật mặc định** | OAuth2, JWT, xác thực session, kiểm soát truy cập theo thiết bị |
| 📊 | **Bảng điều khiển Admin** | Giao diện web quản lý thiết bị, người dùng và cấu hình AI |
| 🌐 | **Cloud-Native** | Triển khai mọi nơi — VPS, cloud hoặc on-premise |

---

## Demo trực tiếp

> 🔗 **[nexus.tanlinh.dev](https://nexus.tanlinh.dev/)** — Phiên bản production chính thức

---

### Các giai đoạn xử lý

| # | Giai đoạn | Chi tiết |
|---|-----------|----------|
| 1 | **Thu âm** | ESP32 ghi âm từ microphone và stream qua WebSocket |
| 2 | **Giải mã** | Server giải mã luồng Opus/PCM thành buffer audio sạch |
| 3 | **Nhận dạng (STT)** | Buffer audio được gửi tới engine nhận dạng giọng nói |
| 4 | **Suy luận (LLM)** | Văn bản + lịch sử hội thoại được xử lý bởi AI, stream theo từng câu |
| 5 | **Tổng hợp (TTS)** | Mỗi câu được tổng hợp thành giọng nói ngay khi nhận từ LLM |
| 6 | **Stream về** | Audio mã hóa Opus được stream về loa ESP32 theo thời gian thực |

> 💡 Toàn bộ chu trình diễn ra **tức thì** — người dùng nghe AI trả lời trong khi nó vẫn đang tạo câu trả lời.

---

## Bắt đầu nhanh

### Yêu cầu hệ thống

| Thành phần | Phiên bản | Ghi chú |
|------------|-----------|---------|
| Python | `3.10+` | Runtime |
| ffmpeg | mới nhất | Xử lý audio |
| libopus | mới nhất | Codec Opus |

### Cài đặt

```bash
# Clone repository
git clone https://github.com/your-org/nexus-server.git
cd nexus-server

# Cài đặt dependencies
pip install -r requirements.txt

# Cấu hình môi trường
cp .env.example .env
# → Chỉnh sửa .env với API keys và thông tin xác thực của bạn

# Khởi chạy
python run.py
```

Server mặc định chạy tại `http://localhost:8000`.

### Chế độ phát triển

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

---

## Cấu hình

Tất cả thiết lập được quản lý qua biến môi trường trong `.env`:

| Biến | Mô tả |
|------|-------|
| `SESSION_SECRET` | Khóa mã hóa session cookie |
| `JWT_SECRET` | Khóa ký JWT token |
| `SECRET_KEY` | Khóa bí mật ứng dụng |
| `GOOGLE_CLIENT_ID` | Client ID của Google OAuth2 |
| `GOOGLE_CLIENT_SECRET` | Client Secret của Google OAuth2 |
| `OPENAI_API_KEY` | API key cho dịch vụ OpenAI |
| `LLM_PROVIDERS` | Cấu hình LLM provider đang dùng |
| `ADMIN_USERNAME` | Tài khoản đăng nhập admin |
| `ADMIN_PASSWORD` | Mật khẩu admin |

---

## Bảng điều khiển Admin

Truy cập giao diện quản trị tích hợp:

| URL | Mô tả |
|-----|-------|
| `/dashboard/` | Quản lý thiết bị & người dùng |
| `/docs` | Tài liệu API tương tác (Swagger UI) |
| `/auth/google-login` | Đăng nhập qua Google OAuth2 |

---

## Cấu trúc dự án

```
app/
├── api/            # RESTful endpoints — auth, OAuth2, quản lý thiết bị, OTA
├── websocket/      # WebSocket xử lý giọng nói thời gian thực
├── services/       # Pipeline AI — điều phối LLM, STT, TTS
├── mcp/            # MCP tools & lập lịch báo thức
├── database/       # Kết nối & khởi tạo database
├── robots/         # Registry thiết bị — models & CRUD
└── auth/           # Xác thực — models, bảo mật, schemas

static/
├── admin/          # Frontend bảng điều khiển (HTML/CSS/JS)
└── asset/          # Tài nguyên thương hiệu & media

run.py              # Điểm khởi chạy ứng dụng
requirements.txt    # Dependencies Python
.env                # Cấu hình môi trường (không commit)
```

---

## Lộ trình phát triển

- [x] Pipeline giọng nói thời gian thực (STT → LLM → TTS)
- [x] Hỗ trợ đa provider (OpenAI, Google, tùy chỉnh)
- [x] Bảng điều khiển admin với quản lý thiết bị
- [x] Tích hợp Google OAuth2
- [x] Cập nhật firmware OTA cho ESP32
- [x] MCP tools & lập lịch báo thức
- [ ] Mở rộng hỗ trợ đa ngôn ngữ giọng nói
- [ ] Dashboard phân tích & insights hội thoại
- [ ] Ứng dụng mobile đồng hành

---

## Giấy phép

Dự án này là phần mềm độc quyền được phát triển bởi **Tan Linh**. Bảo lưu mọi quyền. Các thư viện bên thứ ba tuân theo giấy phép tương ứng. Mọi thắc mắc về giấy phép, vui lòng truy cập [nexus.tanlinh.dev](https://nexus.tanlinh.dev/).

---

<p align="center">
  <br/>
  <strong>Built with ❤️ by <a href="https://nexus.tanlinh.dev/">Tan Linh</a></strong>
  <br/><br/>
  <a href="https://nexus.tanlinh.dev/">🌐 nexus.tanlinh.dev</a>
</p>
