"""
REST API endpoints.

Health check, quản lý sessions, MCP, v.v.
Mở rộng thêm endpoint ở đây.
"""

import logging
from fastapi import APIRouter

from app.models import HealthResponse, SessionInfo
from app.websocket.session import get_all_sessions

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["API"])


@router.get("/health", response_model=HealthResponse)
async def health_check():
    """Health check — kiểm tra server đang chạy."""
    sessions = get_all_sessions()
    return HealthResponse(active_sessions=len(sessions))


@router.get("/sessions", response_model=list[SessionInfo])
async def list_sessions():
    """Danh sách tất cả sessions đang kết nối."""
    return [
        SessionInfo(
            session_id=s.session_id,
            device_id=s.device_id,
            client_id=s.client_id,
            is_speaking=s.is_speaking,
            history_length=len(s.chat_history),
        )
        for s in get_all_sessions()
    ]


@router.get("/sessions/{session_id}/history")
async def get_history(session_id: str):
    """Xem lịch sử chat của 1 session."""
    for s in get_all_sessions():
        if s.session_id == session_id:
            return {"session_id": session_id, "history": s.chat_history}
    return {"error": "Session not found"}


# ── MCP endpoints (mở rộng sau) ──────────────────────────────


@router.post("/mcp/tools")
async def list_mcp_tools():
    """Liệt kê MCP tools có sẵn (placeholder)."""
    return {
        "tools": [
            {"name": "set_volume", "description": "Điều chỉnh âm lượng"},
            {"name": "set_brightness", "description": "Điều chỉnh độ sáng"},
            {"name": "reboot", "description": "Khởi động lại thiết bị"},
        ]
    }


@router.post("/mcp/call/{tool_name}")
async def call_mcp_tool(tool_name: str, params: dict = {}):
    """Gọi MCP tool trên ESP32 (placeholder)."""
    logger.info(f"MCP call: {tool_name} params={params}")
    return {
        "tool": tool_name,
        "status": "not_implemented",
        "message": "MCP tool calling chưa được implement. Xem docs/mcp-protocol.md",
    }
