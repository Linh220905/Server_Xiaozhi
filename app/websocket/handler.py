"""
WebSocket handler — xử lý toàn bộ giao tiếp với ESP32.

Nhận message → phân loại → gọi đúng handler.
Mỗi client kết nối = 1 instance handle_client().
"""

import json
import struct
import asyncio
import logging

from fastapi import WebSocket, WebSocketDisconnect

from app.config import config
from app.mcp import MCPToolRegistry
from app.models import ServerHello, AudioParams
from app.websocket.session import Session, create_session, remove_session

logger = logging.getLogger(__name__)
mcp_tools = MCPToolRegistry()
_session_ws: dict[str, WebSocket] = {}
_session_send_locks: dict[str, asyncio.Lock] = {}


async def handle_client(ws: WebSocket) -> None:
    """Entry point cho mỗi client WebSocket."""
    await ws.accept()

    device_id = ws.headers.get("device-id", "unknown")
    client_id = ws.headers.get("client-id", "unknown")
    proto_version = ws.headers.get("protocol-version", "1")

    session = create_session(config, device_id, client_id)
    # Register websocket for this session so background tasks can push to it
    _session_ws[session.session_id] = ws
    _session_send_locks[session.session_id] = asyncio.Lock()
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
_pipeline_triggered: set[str] = set()  # Chặn trigger pipeline nhiều lần

# time out sau 10s
IDLE_TIMEOUT_FRAMES = 167  


def _on_binary(ws: WebSocket, session: Session, data: bytes, proto_version: int) -> None:
    """Parse binary audio frame, thêm vào buffer, check VAD."""
    opus_data = _extract_opus_payload(data, proto_version)
    if not opus_data:
        return

    pcm = session.append_audio(opus_data)
    if pcm is None:
        return

    count = _frame_counters.get(session.session_id, 0) + 1
    _frame_counters[session.session_id] = count

    rms = session._calc_rms(pcm)
    if count <= 10 or count % 5 == 0:
        logger.info(
            f"[{session.device_id}] #{count} rms={rms:.0f} "
            f"silent_frames={session._silent_frames} has_speech={session._has_speech} "
            f"({len(opus_data)}B opus, {session.buffer_size}B buf)"
        )


    if session.session_id in _pipeline_triggered:
        return  

    vad_state = session.check_vad(pcm)
    if vad_state == 'silence_after_speech':
        _pipeline_triggered.add(session.session_id)
        logger.info(f"[{session.device_id}] VAD: silence after speech -- {count} frames, buffer={session.buffer_size} bytes -> triggering STT")
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
    """Gửi câu chào tạm biệt qua TTS rồi đưa client về trạng thái chờ."""
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

  
    try:
        session.is_idling = True
        await _send_json(ws, session, {"type": "idle", "message": "Server is idling (connection kept open)"})
        logger.info(f"\033[93m[{session.device_id}] 💤 Connection left open in idle mode\033[0m")
    except Exception:
        
        try:
            await ws.close()
            logger.info(f"\033[93m[{session.device_id}] 👋 WebSocket closed after goodbye (fallback)\033[0m")
        except Exception:
            logger.exception("Failed to close websocket after goodbye (fallback)")

    session.reset_audio_buffer()
    _frame_counters.pop(session.session_id, None)


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
        _frame_counters[session.session_id] = 0
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
        return

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

    async def on_tts_audio(opus_frame: bytes) -> None:
        await safe_send_bytes(opus_frame)

    async def on_tts_stop() -> None:
        await safe_send_json({"type": "tts", "state": "stop"})

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

    try:
        result = await session.pipeline.process(
            pcm_data,
            session.chat_history,
            on_stt_result=on_stt_result,
            on_tts_start=on_tts_start,
            on_tts_sentence=on_tts_sentence,
            on_tts_audio=on_tts_audio,
            on_tts_stop=on_tts_stop,
            on_music_action=on_music_action,
            is_aborted=lambda: session.aborted,
        )

        if result:
            user_text, assistant_text = result
            session.save_history(user_text, assistant_text)
            logger.info(f"[{session.device_id}] Pipeline done -- user: '{user_text[:50]}' -> assistant: '{assistant_text[:50]}'")
        else:
            logger.warning(f"[{session.device_id}] Pipeline returned no result")
    except Exception as e:
        logger.error(f"[{session.device_id}] Pipeline error: {e}", exc_info=True)
    finally:
        session.is_speaking = False


async def _send_json(ws: WebSocket, session: Session, data: dict) -> None:
    """Gửi JSON message kèm session_id."""
    data["session_id"] = session.session_id
    await ws.send_text(json.dumps(data, ensure_ascii=False))
