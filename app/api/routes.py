import logging
from fastapi import APIRouter, Depends

from app.models import HealthResponse, SessionInfo
from app.websocket.session import get_all_sessions
from app.database.chat_history import get_chat_sessions_for_user
from .auth import router as auth_router
from .robot_api import router as robot_router
from .otp import router as otp_router
from .ota_activate import router as ota_activate_router
from .auth_google import router as auth_google_router, require_viewer
from .OTA.firmware import router as ota_firmware_router
from .admin import firmware_router as admin_firmware_router

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["API"])
v1_router = APIRouter(prefix="/api/v1", tags=["API v1"])


@router.get("/health", response_model=HealthResponse)
async def health_check():
    sessions = get_all_sessions()
    return HealthResponse(active_sessions=len(sessions))


@router.get("/sessions", response_model=list[SessionInfo])
async def list_sessions():
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
    for s in get_all_sessions():
        if s.session_id == session_id:
            return {"session_id": session_id, "history": s.chat_history}
    return {"error": "Session not found"}


v1_router.include_router(auth_router)
router.include_router(auth_google_router)
router.include_router(robot_router)
router.include_router(otp_router)
router.include_router(ota_activate_router)

router.include_router(ota_firmware_router)
router.include_router(admin_firmware_router)


@router.get("/chat-history")
async def chat_history(session: dict = Depends(require_viewer)):
    """Lấy lịch sử chat của tất cả robot thuộc user hiện tại (chỉ dành cho user/viewer)."""
    email = session.get("email", "")
    sessions = get_chat_sessions_for_user(email)
    return {"ok": True, "sessions": sessions}


@router.post("/mcp/tools")
async def list_mcp_tools():
    return {
        "tools": [
            {"name": "set_volume", "description": "Điều chỉnh âm lượng"},
            {"name": "set_brightness", "description": "Điều chỉnh độ sáng"},
            {"name": "reboot", "description": "Khởi động lại thiết bị"},
        ]
    }


@router.post("/mcp/call/{tool_name}")
async def call_mcp_tool(tool_name: str, params: dict = {}):
    logger.info(f"MCP call: {tool_name} params={params}")
    return {
        "tool": tool_name,
        "status": "not_implemented",
        "message": "MCP tool calling chưa được implement.",
    }
