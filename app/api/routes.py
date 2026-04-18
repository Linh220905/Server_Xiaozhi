from app.server_logging import get_logger
from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from io import BytesIO
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except Exception:  # pragma: no cover - graceful fallback when pillow is missing
    Image = None
    ImageDraw = None
    ImageFont = None

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
from .admin import users_router as admin_users_router
from app.services.learning_content import get_learning_payload
from app.database.assignments import (
    create_assignment_for_user,
    delete_assignment_for_user,
    list_assignments_for_user,
    update_assignment_for_user,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/api", tags=["API"])
v1_router = APIRouter(prefix="/api/v1", tags=["API v1"])


def _pick_flashcard_font(size: int, bold: bool = False):
    if ImageFont is None:
        return None

    candidates = []
    try:
        import PIL

        pil_font_dir = Path(PIL.__file__).resolve().parent / "fonts"
        if bold:
            candidates.append(str(pil_font_dir / "DejaVuSans-Bold.ttf"))
        candidates.append(str(pil_font_dir / "DejaVuSans.ttf"))
    except Exception:
        pass

    if bold:
        candidates.extend(
            [
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
            ]
        )
    candidates.extend(
        [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        ]
    )

    for path in candidates:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size=size)
            except Exception:
                continue
    return ImageFont.load_default()


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
router.include_router(admin_users_router)


@router.get("/chat-history")
async def chat_history(session: dict = Depends(require_viewer)):
    """Lấy lịch sử chat của tất cả robot thuộc user hiện tại (chỉ dành cho user/viewer)."""
    email = session.get("email", "")
    sessions = get_chat_sessions_for_user(email)
    return {"ok": True, "sessions": sessions}


@router.get("/learning/topics")
async def learning_topics(session: dict = Depends(require_viewer)):
    _ = session
    return get_learning_payload()


@router.get("/assignments")
async def list_assignments(session: dict = Depends(require_viewer)):
    email = session.get("email", "")
    return {"ok": True, "items": list_assignments_for_user(email)}


@router.post("/assignments")
async def create_assignment(payload: dict, session: dict = Depends(require_viewer)):
    email = session.get("email", "")
    try:
        item = create_assignment_for_user(email, payload)
    except ValueError as e:
        return {"ok": False, "detail": str(e)}
    return {"ok": True, "item": item}


@router.put("/assignments/{assignment_id}")
async def update_assignment(assignment_id: int, payload: dict, session: dict = Depends(require_viewer)):
    email = session.get("email", "")
    try:
        item = update_assignment_for_user(email, assignment_id, payload)
    except ValueError as e:
        return {"ok": False, "detail": str(e)}
    return {"ok": True, "item": item}


@router.delete("/assignments/{assignment_id}")
async def delete_assignment(assignment_id: int, session: dict = Depends(require_viewer)):
    email = session.get("email", "")
    delete_assignment_for_user(email, assignment_id)
    return {"ok": True}


@router.get("/learning/flashcard")
async def learning_flashcard(
        topic_id: str = Query("general"),
        word: str = Query("Word"),
        meaning: str = Query("Nghia"),
        w: int = Query(320, ge=120, le=800),
        h: int = Query(240, ge=120, le=600),
        q: int = Query(38, ge=20, le=85),
        fmt: str = Query("png"),
):
    safe_word = (word or "Word")[:40]
    safe_meaning = (meaning or "Nghia")[:60]

    if Image is not None and ImageDraw is not None:
        img = Image.new("RGB", (w, h), "#0f172a")
        draw = ImageDraw.Draw(img)
        panel_margin = max(8, min(w, h) // 18)
        panel_top = panel_margin
        panel_bottom = h - panel_margin
        panel_left = panel_margin
        panel_right = w - panel_margin
        draw.rounded_rectangle(
            (panel_left, panel_top, panel_right, panel_bottom),
            radius=max(10, min(w, h) // 14),
            fill="#fdfdfd",
        )

        split_y = panel_top + int((panel_bottom - panel_top) * 0.58)
        draw.line(
            (panel_left + panel_margin, split_y, panel_right - panel_margin, split_y),
            fill="#e5e7eb",
            width=max(1, min(w, h) // 180),
        )

        def _center_x(text: str, font_obj) -> int:
            bbox = draw.textbbox((0, 0), text, font=font_obj)
            tw = max(1, bbox[2] - bbox[0])
            return int(max(panel_left + panel_margin, (w - tw) // 2))

        word_text = safe_word.upper()

        def _fit_font(text: str, prefer: int, min_size: int, max_w: int, max_h: int, bold: bool = True):
            size = prefer
            while size >= min_size:
                f = _pick_flashcard_font(size, bold=bold)
                bbox = draw.textbbox((0, 0), text, font=f)
                tw = max(1, bbox[2] - bbox[0])
                th = max(1, bbox[3] - bbox[1])
                if tw <= max_w and th <= max_h:
                    return f
                size -= 2
            return _pick_flashcard_font(min_size, bold=bold)

        def _wrap_text_to_width(text: str, font_obj, max_w: int, max_lines: int = 2) -> list[str]:
            raw = (text or "").strip()
            if not raw:
                return [""]

            words = raw.split()
            lines: list[str] = []
            cur = ""

            for token in words:
                candidate = token if not cur else f"{cur} {token}"
                bbox = draw.textbbox((0, 0), candidate, font=font_obj)
                tw = max(1, bbox[2] - bbox[0])
                if tw <= max_w:
                    cur = candidate
                    continue

                if cur:
                    lines.append(cur)
                    cur = token
                else:
                    # Single long token; hard cut to keep visibility.
                    chunk = ""
                    for ch in token:
                        c2 = chunk + ch
                        cb = draw.textbbox((0, 0), c2, font=font_obj)
                        if (cb[2] - cb[0]) <= max_w:
                            chunk = c2
                        else:
                            break
                    lines.append(chunk or token)
                    cur = token[len(chunk):].strip()

                if len(lines) >= max_lines:
                    break

            if len(lines) < max_lines and cur:
                lines.append(cur)

            if len(lines) > max_lines:
                lines = lines[:max_lines]

            return lines or [raw]

        top_h = max(1, split_y - panel_top - panel_margin)
        bottom_h = max(1, panel_bottom - split_y - panel_margin)
        usable_w = max(1, panel_right - panel_left - panel_margin * 2)

        word_font = _fit_font(
            word_text,
            prefer=max(52, int(h * 0.30)),
            min_size=max(30, int(h * 0.16)),
            max_w=usable_w,
            max_h=int(top_h * 0.86),
            bold=True,
        )

        # Fit meaning as 1-2 lines for better readability on small displays.
        meaning_font = None
        meaning_lines: list[str] = [safe_meaning]
        for size in range(max(44, int(h * 0.22)), max(20, int(h * 0.10)) - 1, -2):
            f = _pick_flashcard_font(size, bold=True)
            lines = _wrap_text_to_width(safe_meaning, f, usable_w, max_lines=2)
            line_heights = []
            line_widths = []
            for ln in lines:
                bb = draw.textbbox((0, 0), ln, font=f)
                line_heights.append(max(1, bb[3] - bb[1]))
                line_widths.append(max(1, bb[2] - bb[0]))
            block_h = sum(line_heights) + (max(6, h // 40) if len(lines) > 1 else 0)
            if line_widths and max(line_widths) <= usable_w and block_h <= int(bottom_h * 0.82):
                meaning_font = f
                meaning_lines = lines
                break
        if meaning_font is None:
            meaning_font = _pick_flashcard_font(max(20, int(h * 0.10)), bold=True)
            meaning_lines = _wrap_text_to_width(safe_meaning, meaning_font, usable_w, max_lines=2)

        word_bbox = draw.textbbox((0, 0), word_text, font=word_font)
        word_h = max(1, word_bbox[3] - word_bbox[1])
        y_word = panel_top + max(panel_margin, (top_h - word_h) // 2)

        line_gap = max(6, h // 40)
        line_metrics = [draw.textbbox((0, 0), ln, font=meaning_font) for ln in meaning_lines]
        line_heights = [max(1, bb[3] - bb[1]) for bb in line_metrics]
        block_h = sum(line_heights) + (line_gap if len(meaning_lines) > 1 else 0)
        y_meaning = split_y + max(panel_margin // 2, (bottom_h - block_h) // 2)

        draw.text(
            (_center_x(word_text, word_font), y_word),
            word_text,
            fill="#111827",
            font=word_font,
        )
        cursor_y = y_meaning
        for i, ln in enumerate(meaning_lines):
            draw.text(
                (_center_x(ln, meaning_font), cursor_y),
                ln,
                fill="#0f172a",
                font=meaning_font,
            )
            cursor_y += line_heights[i] + (line_gap if i < len(meaning_lines) - 1 else 0)

        output = BytesIO()
        if (fmt or "jpg").lower() == "png":
            img.save(output, format="PNG", optimize=False)
            return Response(content=output.getvalue(), media_type="image/png")

        img.save(output, format="JPEG", quality=q, optimize=True, progressive=False)
        return Response(content=output.getvalue(), media_type="image/jpeg")

    # Fallback when Pillow is unavailable
    svg = f"""
<svg xmlns='http://www.w3.org/2000/svg' width='800' height='480' viewBox='0 0 800 480'>
    <defs>
        <linearGradient id='bg' x1='0' y1='0' x2='1' y2='1'>
            <stop offset='0%' stop-color='#1f2937'/>
            <stop offset='100%' stop-color='#111827'/>
        </linearGradient>
    </defs>
    <rect width='800' height='480' fill='url(#bg)'/>
    <rect x='36' y='36' width='728' height='408' rx='24' fill='#f9fafb' opacity='0.98'/>
    <text x='72' y='230' font-size='72' fill='#111827' font-family='Arial, sans-serif' font-weight='700'>{safe_word}</text>
    <text x='72' y='325' font-size='44' fill='#111827' font-family='Arial, sans-serif' font-weight='700'>{safe_meaning}</text>
</svg>
""".strip()
    return Response(content=svg, media_type="image/svg+xml")


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
