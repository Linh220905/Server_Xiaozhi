"""
WebSocket handler — xử lý toàn bộ giao tiếp với ESP32.

Nhận message → phân loại → gọi đúng handler.
Mỗi client kết nối = 1 instance handle_client().
"""

import json
import struct
import time
import asyncio
import os
from app.server_logging import get_logger

from fastapi import WebSocket, WebSocketDisconnect

from app.config import config
from app.mcp import MCPToolRegistry
from app.models import ServerHello, AudioParams
from app.websocket.session import Session, create_session, remove_session, get_all_sessions
from app.robots.crud import get_robot_config, get_robot_by_mac, create_robot, update_robot_status, touch_robot_last_seen, generate_otp
from app.database.chat_history import save_chat_session
from app.database.assignments import get_latest_active_assignment_for_robot
from app.robots.models import RobotCreate

logger = get_logger(__name__)
mcp_tools = MCPToolRegistry()
_session_ws: dict[str, WebSocket] = {}
_session_send_locks: dict[str, asyncio.Lock] = {}
_pending_offline_tasks: dict[str, asyncio.Task] = {}
OFFLINE_DELAY_SECONDS = 300


def _cancel_pending_offline(device_id: str) -> None:
    task = _pending_offline_tasks.pop(device_id, None)
    if task and not task.done():
        task.cancel()


def _has_active_session_for_device(device_id: str) -> bool:
    return any(s.device_id == device_id for s in get_all_sessions())


def _schedule_offline_if_inactive(device_id: str) -> None:
    _cancel_pending_offline(device_id)

    async def _runner() -> None:
        try:
            await asyncio.sleep(OFFLINE_DELAY_SECONDS)
            if _has_active_session_for_device(device_id):
                logger.info("[%s] Skip offline: device has active session", device_id)
                return
            updated = update_robot_status(device_id, False)
            logger.info("[%s] Auto-offline after %ss inactivity: %s", device_id, OFFLINE_DELAY_SECONDS, "ok" if updated else "not-found")
        except asyncio.CancelledError:
            return
        finally:
            _pending_offline_tasks.pop(device_id, None)

    _pending_offline_tasks[device_id] = asyncio.create_task(_runner())


def _normalize_robot_id(device_id: str, client_id: str) -> str:
    if client_id and client_id != "unknown":
        return client_id
    compact_mac = (device_id or "unknown").replace(":", "").replace("-", "")
    return f"dev-{compact_mac}"


def _ensure_robot_registered(device_id: str, client_id: str) -> None:
    """Auto-create robot record when device connects via WebSocket."""
    if not device_id or device_id == "unknown":
        return

    existed = get_robot_by_mac(device_id)

    if existed is None:
        # New robot → auto-register + generate OTP
        try:
            robot = RobotCreate(
                mac_address=device_id,
                robot_id=_normalize_robot_id(device_id, client_id),
                name=f"Nexus-{device_id[-5:].replace(':', '')}",
            )
            create_robot(robot)
            logger.info("[%s] Auto-registered robot record", device_id)
            otp = generate_otp(device_id)
            logger.info("[%s] OTP generated: %s", device_id, otp)
        except Exception as e:
            logger.warning("[%s] Auto-register robot failed: %s", device_id, e)
    elif not existed.owner_username:
        # Existing robot without owner → refresh OTP
        try:
            otp = generate_otp(device_id)
            logger.info("[%s] OTP refreshed: %s", device_id, otp)
        except Exception as e:
            logger.warning("[%s] OTP refresh failed: %s", device_id, e)


async def handle_client(ws: WebSocket) -> None:
    """Entry point cho mỗi client WebSocket."""
    await ws.accept()

    device_id = ws.headers.get("device-id", "unknown")
    client_id = ws.headers.get("client-id", "unknown")
    proto_version = ws.headers.get("protocol-version", "1")

    session = create_session(config, device_id, client_id)
    _cancel_pending_offline(device_id)
    # Register websocket for this session so background tasks can push to it
    _session_ws[session.session_id] = ws
    _session_send_locks[session.session_id] = asyncio.Lock()

    # Auto register robot from ESP32 identity headers (Device-Id/Client-Id)
    _ensure_robot_registered(device_id, client_id)
    
    logger.info(f"[{device_id}] Connected (protocol v{proto_version})")

    try:
        while True:
            raw = await ws.receive()

            if raw["type"] == "websocket.receive":
                if "text" in raw and raw["text"]:
                    await _on_text(ws, session, raw["text"])
                elif "bytes" in raw and raw["bytes"]:
                    _on_binary(ws, session, raw["bytes"], int(proto_version))

            elif raw["type"] == "websocket.disconnect":
                break

    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error(f"[{device_id}] Error: {e}", exc_info=True)
    finally:

        # Nếu disconnect mà buffer còn data VÀ chưa trigger pipeline → trigger
        frames = _frame_counters.pop(session.session_id, 0)
        _low_rms_counters.pop(session.session_id, None)
        _high_rms_counters.pop(session.session_id, None)
        _post_high_silence_counters.pop(session.session_id, None)
        _high_rms_armed.discard(session.session_id)
        _rms_baseline_avg.pop(session.session_id, None)
        _rms_noise_jitter.pop(session.session_id, None)
        _pipeline_finished_at.pop(session.session_id, None)
        already = session.session_id in _pipeline_triggered
        if session.buffer_size > 3200 and not already:
            _pipeline_triggered.add(session.session_id)
            logger.info(f"[{device_id}] Disconnected with {frames} frames, buffer={session.buffer_size} bytes -> auto-triggering STT")
            try:
                await _run_pipeline(ws, session)
            except Exception:
                logger.warning(f"[{device_id}] Pipeline on disconnect failed (WS already closed)")
        else:
            logger.info(f"[{device_id}] Disconnected ({frames} frames, pipeline_already={'yes' if already else 'no'})")

        _pipeline_triggered.discard(session.session_id)

        try:
            touch_robot_last_seen(device_id)
            _schedule_offline_if_inactive(device_id)
        except Exception as e:
            logger.warning("[%s] Failed to schedule robot offline status: %s", device_id, e)

        # Cleanup websocket mapping and session
        try:
            _session_ws.pop(session.session_id, None)
            # remove send lock
            _session_send_locks.pop(session.session_id, None)
        except Exception:
            pass

        remove_session(session.session_id)



async def _on_text(ws: WebSocket, session: Session, raw: str) -> None:
    """Phân loại JSON message và gọi handler tương ứng."""
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning(f"[{session.device_id}] Invalid JSON: {raw[:100]}")
        return

    msg_type = msg.get("type", "")
    logger.info(f"[{session.device_id}] ← {msg_type}")

    handlers = {
        "hello": _handle_hello,
        "listen": _handle_listen,
        "abort": _handle_abort,
        "mcp": _handle_mcp,
    }

    handler = handlers.get(msg_type)
    if handler:
        await handler(ws, session, msg)
    else:
        logger.warning(f"[{session.device_id}] Unknown type: {msg_type}")



_frame_counters: dict[str, int] = {}
_low_rms_counters: dict[str, int] = {}
_high_rms_counters: dict[str, int] = {}
_post_high_silence_counters: dict[str, int] = {}
_high_rms_armed: set[str] = set()
_rms_baseline_avg: dict[str, float] = {}
_rms_noise_jitter: dict[str, float] = {}
_pipeline_triggered: set[str] = set()  # Chặn trigger pipeline nhiều lần
_pipeline_finished_at: dict[str, float] = {}  # Timestamp khi pipeline kết thúc

IDLE_TIMEOUT_FRAMES = 1000  
LOW_RMS_THRESHOLD_MIN = 700
LOW_RMS_FRAMES = 90
RMS_MARGIN_MIN = 500
RMS_MARGIN_MAX = 1500
RMS_SPIKE_DELTA = 3000
HIGH_RMS_FRAMES = 5  # >5 frames mới xác nhận có người nói
POST_HIGH_SILENCE_FRAMES = 8
COOLDOWN_SECONDS = 1.5  # Bỏ qua audio residual sau khi pipeline xong
MAX_UTTERANCE_FRAMES = 260  # ~15.6s @ 60ms/frame


def _update_consecutive_counter(
    counters: dict[str, int],
    session_id: str,
    *,
    enabled: bool,
    matched: bool,
) -> int:
    """Cập nhật bộ đếm frame liên tiếp theo điều kiện (clean helper)."""
    if enabled and matched:
        value = counters.get(session_id, 0) + 1
    else:
        value = 0
    counters[session_id] = value
    return value


def _update_rms_baseline(session_id: str, rms: float, *, freeze: bool) -> tuple[float, int]:
    """Theo dõi baseline RMS động và biên nhiễu (kẹp 500-800)."""
    baseline = _rms_baseline_avg.get(session_id)
    jitter = _rms_noise_jitter.get(session_id, 0.0)

    if baseline is None:
        baseline = rms
        _rms_baseline_avg[session_id] = baseline
        _rms_noise_jitter[session_id] = 0.0
        return baseline, RMS_MARGIN_MIN

    abs_delta = abs(rms - baseline)
    jitter = (1.0 - 0.08) * jitter + 0.08 * abs_delta
    margin = int(max(RMS_MARGIN_MIN, min(RMS_MARGIN_MAX, jitter * 3.0)))

    if not freeze:
        alpha = 0.09 if rms < baseline else 0.03
        capped_rms = min(rms, baseline + margin)
        baseline = (1.0 - alpha) * baseline + alpha * capped_rms

    _rms_baseline_avg[session_id] = baseline
    _rms_noise_jitter[session_id] = jitter
    return baseline, margin


def _on_binary(ws: WebSocket, session: Session, data: bytes, proto_version: int) -> None:
    """Parse binary audio frame, thêm vào buffer, check VAD."""
    # Đang chạy pipeline thì bỏ qua frame mới để tránh tích lũy buffer vô hạn.
    if session.session_id in _pipeline_triggered:
        return

    now = time.monotonic()
    finished_at = _pipeline_finished_at.get(session.session_id)
    if finished_at is not None and (now - finished_at) < COOLDOWN_SECONDS:
        return

    # Khi đã vào idle hoặc server đang phát TTS thì bỏ qua audio.
    # Tránh thu lại tiếng loa của chính thiết bị và trigger STT lặp.
    if session.is_idling or session.is_speaking:
        return

    opus_data = _extract_opus_payload(data, proto_version)
    if not opus_data:
        return

    pcm = session.append_audio(opus_data)
    if pcm is None:
        return

    count = _frame_counters.get(session.session_id, 0) + 1
    _frame_counters[session.session_id] = count

    rms = session._calc_rms(pcm)
    high_rms_armed = session.session_id in _high_rms_armed
    baseline_rms, adaptive_margin = _update_rms_baseline(
        session.session_id,
        rms,
        freeze=high_rms_armed,
    )
    dynamic_high_threshold = baseline_rms + RMS_SPIKE_DELTA
    dynamic_return_threshold = baseline_rms + adaptive_margin

    high_rms_count = _update_consecutive_counter(
        _high_rms_counters,
        session.session_id,
        enabled=True,
        matched=rms > dynamic_high_threshold,
    )

    if (not high_rms_armed) and high_rms_count >= HIGH_RMS_FRAMES:
        _high_rms_armed.add(session.session_id)
        high_rms_armed = True
        _post_high_silence_counters[session.session_id] = 0
        logger.info(
            f"[{session.device_id}] High-RMS armed: rms>{dynamic_high_threshold:.0f} "
            f"(base={baseline_rms:.0f}, spike=+{RMS_SPIKE_DELTA}) for {high_rms_count} frames"
        )

    post_high_silence_count = _update_consecutive_counter(
        _post_high_silence_counters,
        session.session_id,
        enabled=high_rms_armed,
        matched=rms <= dynamic_return_threshold,
    )

    # Cập nhật VAD trước để lấy trạng thái mới nhất (_has_speech/_silent_frames/thresholds)
    vad_state = session.check_vad(pcm)

    # Low-RMS fallback chỉ hoạt động SAU KHI đã có speech.
    # Dùng ngưỡng động theo noise floor để tránh timeout giả khi phòng yên tĩnh.
    dynamic_low_rms_threshold = max(LOW_RMS_THRESHOLD_MIN, int(session._last_silence_threshold + 80))
    low_rms_count = _update_consecutive_counter(
        _low_rms_counters,
        session.session_id,
        enabled=session._has_speech,
        matched=rms < dynamic_low_rms_threshold,
    )

    if count <= 10 or count % 5 == 0:
        logger.info(
            f"[{session.device_id}] #{count} rms={rms:.0f} "
            f"base={baseline_rms:.0f} margin={adaptive_margin} "
            f"noise_floor={session._noise_floor_rms:.0f} "
            f"delta={session._last_rms_delta:.0f} "
            f"th_s={session._last_speech_threshold:.0f} th_z={session._last_silence_threshold:.0f} "
            f"silent_frames={session._silent_frames} has_speech={session._has_speech} "
            f"high_rms={high_rms_count} "
            f"high_armed={high_rms_armed} post_sil={post_high_silence_count} "
            f"({len(opus_data)}B opus, {session.buffer_size}B buf)"
        )

    if high_rms_armed and post_high_silence_count >= POST_HIGH_SILENCE_FRAMES:
        _pipeline_triggered.add(session.session_id)
        _high_rms_armed.discard(session.session_id)
        _post_high_silence_counters[session.session_id] = 0
        logger.info(
            f"[{session.device_id}] Adaptive High-RMS trigger: "
            f"rms>{dynamic_high_threshold:.0f}({HIGH_RMS_FRAMES}f) rồi "
            f"rms<={dynamic_return_threshold:.0f} (base+silent_margin, {post_high_silence_count}f) -> triggering STT"
        )
        asyncio.create_task(_run_pipeline(ws, session))
        return

    # Trigger STT fallback khi đã có speech và đã im lặng đủ lâu.
    if session._has_speech and session._silent_frames >= 8 and low_rms_count >= LOW_RMS_FRAMES:
        _pipeline_triggered.add(session.session_id)
        logger.info(
            f"[{session.device_id}] Low-RMS timeout: rms<{dynamic_low_rms_threshold} for {low_rms_count} frames -> triggering STT"
        )
        asyncio.create_task(_run_pipeline(ws, session))
        return

    if vad_state == 'silence_after_speech':
        _pipeline_triggered.add(session.session_id)
        logger.info(f"[{session.device_id}] VAD: silence after speech -- {count} frames, buffer={session.buffer_size} bytes -> triggering STT")
        asyncio.create_task(_run_pipeline(ws, session))

    elif session._has_speech and count >= MAX_UTTERANCE_FRAMES:
        _pipeline_triggered.add(session.session_id)
        logger.info(f"[{session.device_id}] Max utterance length ({count} frames) -> triggering STT")
        asyncio.create_task(_run_pipeline(ws, session))

    elif not session._has_speech and count >= IDLE_TIMEOUT_FRAMES:
        # If already idling, ignore repeated idle triggers
        if session.is_idling:
            return

        _pipeline_triggered.add(session.session_id)
        logger.info(f"\033[93m[{session.device_id}] ⏰ Idle timeout ({count} frames, ~{count*0.06:.0f}s) → goodbye (enter idle)\033[0m")
        # Enter idle state and play goodbye once (or send idle notification).
        asyncio.create_task(_goodbye_and_idle(ws, session))


def _extract_opus_payload(data: bytes, version: int) -> bytes | None:
    """Tách Opus payload từ binary frame (bỏ header nếu có)."""
    if version == 2 and len(data) > 16:
        return data[16:]
    elif version == 3 and len(data) > 4:
        payload_size = struct.unpack("!H", data[2:4])[0]
        return data[4 : 4 + payload_size]
    elif version == 1:
        return data
    return data



async def _goodbye_and_idle(ws: WebSocket, session: Session) -> None:
    """Gửi câu chào tạm biệt qua TTS rồi đóng kênh để client về trạng thái chờ."""
    goodbye_text = "Bạn ơi, lâu quá không thấy nói gì, tôi đi ngủ đây nhé, khi nào cần thì gọi lại nha!"
    # mark idling to avoid retriggers
    session.is_idling = True
    try:
        await _send_json(ws, session, {"type": "tts", "state": "start"})
        await _send_json(ws, session, {"type": "tts", "state": "sentence_start", "text": goodbye_text})

        logger.info(f"\033[92m🔊 Goodbye TTS: {goodbye_text}\033[0m")
        async for opus_frame in session.pipeline._tts.synthesize(goodbye_text):
            if session.aborted:
                break
            await ws.send_bytes(opus_frame)
            await asyncio.sleep(0.054)  # pacing

        await _send_json(ws, session, {"type": "tts", "state": "stop"})
    except Exception as e:
        logger.error(f"[{session.device_id}] Goodbye error: {e}")

    session.reset_audio_buffer()
    _frame_counters.pop(session.session_id, None)
    _low_rms_counters.pop(session.session_id, None)
    _high_rms_counters.pop(session.session_id, None)
    _post_high_silence_counters.pop(session.session_id, None)
    _high_rms_armed.discard(session.session_id)
    _rms_baseline_avg.pop(session.session_id, None)
    _rms_noise_jitter.pop(session.session_id, None)

    # Với ESP32 auto mode, sau tts stop thiết bị sẽ tự quay lại listening.
    # Đóng websocket để thiết bị chuyển hẳn về idle, tránh lặp lại timeout-goodbye.
    try:
        await ws.close()
        logger.info(f"\033[93m[{session.device_id}] 👋 WebSocket closed after idle-timeout goodbye\033[0m")
    except Exception:
        logger.exception("Failed to close websocket after idle-timeout goodbye")


async def _handle_hello(ws: WebSocket, session: Session, msg: dict) -> None:
    """Trả server hello với session_id và audio params."""
    response = ServerHello(
        session_id=session.session_id,
        audio_params=AudioParams(
            sample_rate=config.audio_output.sample_rate,
            channels=config.audio_output.channels,
            frame_duration=config.audio_output.frame_duration_ms,
        ),
    )
    await ws.send_text(response.model_dump_json())
    logger.info(f"[{session.device_id}] → hello (session={session.session_id[:8]}...)")


async def _handle_listen(ws: WebSocket, session: Session, msg: dict) -> None:
    """Xử lý listen start/stop/detect."""
    state = msg.get("state", "")
    mode = msg.get("mode", "")
    logger.info(f"[{session.device_id}] listen state={state} mode={mode}")

    if state in ("start", "detect"):
        session.reset_audio_buffer()
        session.is_idling = False
        _pipeline_finished_at.pop(session.session_id, None)
        _frame_counters[session.session_id] = 0
        _low_rms_counters[session.session_id] = 0
        _high_rms_counters[session.session_id] = 0
        _post_high_silence_counters[session.session_id] = 0
        _high_rms_armed.discard(session.session_id)
        _rms_baseline_avg.pop(session.session_id, None)
        _rms_noise_jitter.pop(session.session_id, None)
        _pipeline_triggered.discard(session.session_id)
        logger.info(f"[{session.device_id}] Recording started (mode={mode})")

    elif state == "stop":
        if session.session_id not in _pipeline_triggered:
            _pipeline_triggered.add(session.session_id)
            frames = _frame_counters.get(session.session_id, 0)
            logger.info(f"[{session.device_id}] Recording stopped -- {frames} frames, buffer={session.buffer_size} bytes")
            asyncio.create_task(_run_pipeline(ws, session))
        else:
            logger.info(f"[{session.device_id}] Recording stopped -- pipeline already triggered, skipping")


async def _handle_abort(ws: WebSocket, session: Session, msg: dict) -> None:
    """Dừng phát audio ngay lập tức."""
    session.abort()
    logger.info(f"[{session.device_id}] Aborted")


async def _handle_mcp(ws: WebSocket, session: Session, msg: dict) -> None:
    """Xử lý MCP messages cơ bản: tools/list và tools/call."""
    payload = msg.get("payload") or {}

    # Hỗ trợ cả format phẳng và format payload
    op = (
        msg.get("op")
        or payload.get("op")
        or msg.get("method")
        or payload.get("method")
    )

    if op in ("tools/list", "list_tools", "mcp.tools.list"):
        await _send_json(
            ws,
            session,
            {
                "type": "mcp",
                "op": "tools/list",
                "ok": True,
                "tools": mcp_tools.list_tools(),
            },
        )
        return

    if op in ("tools/call", "call_tool", "mcp.tools.call"):
        name = (
            msg.get("name")
            or payload.get("name")
            or (msg.get("params") or {}).get("name")
            or (payload.get("params") or {}).get("name")
        )
        arguments = (
            msg.get("arguments")
            or payload.get("arguments")
            or (msg.get("params") or {}).get("arguments")
            or (payload.get("params") or {}).get("arguments")
            or {}
        )

        result = await mcp_tools.call_tool(str(name or ""), arguments)
        await _send_json(
            ws,
            session,
            {
                "type": "mcp",
                "op": "tools/call",
                "name": name,
                "ok": result.ok,
                "content": result.content,
            },
        )
        return

    await _send_json(
        ws,
        session,
        {
            "type": "mcp",
            "ok": False,
            "error": f"Unsupported MCP operation: {op}",
        },
    )



async def _run_pipeline(ws: WebSocket, session: Session) -> None:
    """Chạy pipeline STT → LLM → TTS và gửi kết quả về client."""
    pcm_data = session.take_audio_buffer()
    duration_s = len(pcm_data) / (16000 * 2)  
    logger.info(f"[{session.device_id}] Pipeline starting -- {len(pcm_data)} bytes ({duration_s:.1f}s audio)")

    if len(pcm_data) < 3200:
        logger.info(f"[{session.device_id}] Audio quá ngắn ({duration_s:.1f}s), bỏ qua")
        _pipeline_triggered.discard(session.session_id)
        _frame_counters[session.session_id] = 0
        _low_rms_counters[session.session_id] = 0
        _high_rms_counters[session.session_id] = 0
        _post_high_silence_counters[session.session_id] = 0
        _high_rms_armed.discard(session.session_id)
        _rms_baseline_avg.pop(session.session_id, None)
        _rms_noise_jitter.pop(session.session_id, None)
        session.reset_audio_buffer()
        return

    # Get robot config to customize behavior
    robot_config = get_robot_config(session.device_id)

    try:
        session.pipeline._tts.apply_runtime_config(
            robot_config.tts_config if robot_config else None,
        )
    except Exception as e:
        logger.warning("[%s] Failed to apply robot TTS config: %s", session.device_id, e)
    
    session.is_speaking = True
    ws_open = True

    async def safe_send_json(data: dict) -> None:
        nonlocal ws_open
        if not ws_open:
            return
        try:
            await _send_json(ws, session, data)
        except Exception:
            ws_open = False

    async def safe_send_bytes(data: bytes) -> None:
        nonlocal ws_open
        if not ws_open:
            return
        try:
            await ws.send_bytes(data)
        except Exception:
            ws_open = False

    async def on_stt_result(text: str) -> None:
        logger.info(f"[{session.device_id}] STT result: {text}")
        await safe_send_json({"type": "stt", "text": text})

    async def on_tts_start() -> None:
        await safe_send_json({"type": "tts", "state": "start"})

    async def on_tts_sentence(text: str) -> None:
        logger.info(f"[{session.device_id}] TTS sentence: {text}")
        await safe_send_json({"type": "tts", "state": "sentence_start", "text": text})

    async def on_emotion(emotion: str) -> None:
        logger.info(f"[{session.device_id}] Emotion: {emotion}")
        await safe_send_json({"type": "llm", "emotion": emotion})

    async def on_tts_audio(opus_frame: bytes) -> None:
        await safe_send_bytes(opus_frame)

    async def on_tts_stop() -> None:
        await safe_send_json({"type": "tts", "state": "stop"})

    async def on_learning_card(payload: dict) -> None:
        image_url = payload.get("image_url")
        state = str(payload.get("state") or "flashcard")
        if isinstance(image_url, str) and image_url.startswith("/"):
            public_http_base = os.getenv("NEXUS_HTTP_BASE_URL", "").strip().rstrip("/")
            if public_http_base:
                image_url = f"{public_http_base}{image_url}"
            else:
                forwarded_proto = ws.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
                scheme = forwarded_proto or ("https" if ws.url.scheme == "wss" else "http")
                host = (
                    ws.headers.get("x-forwarded-host", "").split(",")[0].strip()
                    or ws.headers.get("host")
                    or ws.url.netloc
                )
                image_url = f"{scheme}://{host}{image_url}"

        logger.info(
            "[%s] Learning flashcard -> topic=%s word=%s image_url=%s",
            session.device_id,
            payload.get("topic_id"),
            payload.get("word"),
            image_url,
        )

        await safe_send_json(
            {
                "type": "learning",
                "state": state,
                "word": payload.get("word"),
                "meaning": payload.get("meaning"),
                "image_url": image_url,
                "kind": payload.get("kind"),
            }
        )

    async def on_music_action(payload: dict) -> None:
        intent = payload.get("intent", "other")
        if intent != "music":
            logger.info(f"[{session.device_id}] Intent=other, skip music tool")
            return

        logger.info(
            f"[{session.device_id}] Intent=music, song='{payload.get('song_name', '')}', ok={payload.get('ok')}"
        )
        await safe_send_json(
            {
                "type": "mcp",
                "op": "tools/call",
                "name": "search_vietnamese_music",
                "intent": intent,
                "song_name": payload.get("song_name", ""),
                "request_body": payload.get("request_body", {}),
                "ok": payload.get("ok", False),
                "content": payload.get("content", []),
                "error": payload.get("error"),
            }
        )

    async def assignment_provider() -> dict | None:
        try:
            return get_latest_active_assignment_for_robot(session.device_id)
        except Exception as e:
            logger.warning("[%s] assignment provider failed: %s", session.device_id, e)
            return None

    # Use robot-specific system prompt if available
    chat_history = session.chat_history
    if robot_config and robot_config.system_prompt:
        # Create a temporary chat history with the robot's system prompt
        chat_history = [{"role": "system", "content": robot_config.system_prompt}]
        chat_history.extend(session.chat_history[1:])  # Add the rest of the history without the original system message

    try:
        result = await session.pipeline.process(
            pcm_data,
            chat_history,
            learning_context=session.learning_context,
            on_stt_result=on_stt_result,
            on_tts_start=on_tts_start,
            on_tts_sentence=on_tts_sentence,
            on_tts_audio=on_tts_audio,
            on_tts_stop=on_tts_stop,
            on_music_action=on_music_action,
            on_learning_card=on_learning_card,
            assignment_provider=assignment_provider,
            on_emotion=on_emotion,
            is_aborted=lambda: session.aborted,
        )

        if result:
            user_text, assistant_text = result
            session.save_history(user_text, assistant_text)
            logger.info(f"[{session.device_id}] Pipeline done -- user: '{user_text[:50]}' -> assistant: '{assistant_text[:50]}'")
            # Persist chat history to DB (upsert per robot)
            try:
                save_chat_session(
                    robot_mac=session.device_id,
                    session_id=session.session_id,
                    messages=session.chat_history,
                )
            except Exception as e:
                logger.warning("[%s] Failed to save chat session: %s", session.device_id, e)
        else:
            logger.warning(f"[{session.device_id}] Pipeline returned no result")
    except Exception as e:
        logger.error(f"[{session.device_id}] Pipeline error: {e}", exc_info=True)
    finally:
        session.is_speaking = False
        _pipeline_finished_at[session.session_id] = time.monotonic()
        # Reset emotion to blink (nháy mắt) when done speaking
        await safe_send_json({"type": "llm", "emotion": "blink"})

        # ── CRITICAL FIX: cho phép nhận audio tiếp sau khi pipeline xong ──
        _pipeline_triggered.discard(session.session_id)
        _frame_counters[session.session_id] = 0
        _low_rms_counters[session.session_id] = 0
        _high_rms_counters[session.session_id] = 0
        _post_high_silence_counters[session.session_id] = 0
        _high_rms_armed.discard(session.session_id)
        _rms_baseline_avg.pop(session.session_id, None)
        _rms_noise_jitter.pop(session.session_id, None)
        session.reset_audio_buffer()
        logger.info(f"[{session.device_id}] Pipeline finished → reset state, ready for next utterance")


async def _send_json(ws: WebSocket, session: Session, data: dict) -> None:
    """Gửi JSON message kèm session_id."""
    data["session_id"] = session.session_id
    await ws.send_text(json.dumps(data, ensure_ascii=False))
