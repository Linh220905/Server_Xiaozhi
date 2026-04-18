from app.server_logging import get_logger
import uuid
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Request

from app.config import config
from app.robots.crud import get_robot_by_mac, create_robot, generate_otp, update_robot_status
from app.robots.models import RobotCreate

logger = get_logger(__name__)
router = APIRouter(tags=["ota"])


def _ensure_robot(mac: str):
    robot = get_robot_by_mac(mac)
    if robot is None:
        robot_id = f"nexus-{mac[-8:].replace(':', '').lower()}"
        new_robot = RobotCreate(
            mac_address=mac,
            robot_id=robot_id,
            name=f"Nexus {mac[-5:]}",
        )
        try:
            robot = create_robot(new_robot)
            logger.info("Auto-registered new robot MAC=%s  id=%s", mac, robot_id)
        except ValueError:
            robot = get_robot_by_mac(mac)
    return robot


@router.api_route("/nexus/ota/", methods=["GET", "POST"])
@router.api_route("/nexus/ota", methods=["GET", "POST"])
@router.api_route("/api/nexus/ota/", methods=["GET", "POST"])
@router.api_route("/api/nexus/ota", methods=["GET", "POST"])
@router.api_route("/api/v1/nexus/ota/", methods=["GET", "POST"])
@router.api_route("/api/v1/nexus/ota", methods=["GET", "POST"])
async def ota_bootstrap(request: Request) -> dict:
    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    request_scheme = (request.url.scheme or "http").lower()
    http_scheme = forwarded_proto or request_scheme
    ws_scheme = "wss" if http_scheme == "https" else "ws"

    host = request.headers.get("x-forwarded-host", "").split(",")[0].strip()
    if not host:
        host = request.headers.get("host", "").strip()
    if not host:
        client_host = request.client.host if request.client else ""
        host = f"{client_host}:{config.server.port}" if client_host else f"127.0.0.1:{config.server.port}"

    mac = request.headers.get("device-id", "").strip()

    public_ws_url = os.getenv("NEXUS_WS_URL", "").strip()
    public_http_base = os.getenv("NEXUS_HTTP_BASE_URL", "").strip().rstrip("/")

    ws_url = public_ws_url or f"{ws_scheme}://{host}/"
    http_base = public_http_base or f"{http_scheme}://{host}"
    now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)

    # Lấy firmware mới nhất trong static/firmware
    firmware_dir = os.path.join(os.path.dirname(__file__), '../../static/firmware')
    firmware_dir = os.path.abspath(firmware_dir)
    firmware_file = None
    firmware_version = "1.0.0"
    for f in sorted(os.listdir(firmware_dir), reverse=True):
        if f.endswith('.bin'):
            firmware_file = f
            try:
                firmware_version = f.split('_')[0]
            except Exception:
                firmware_version = "1.0.0"
            break
    firmware_url = f"{http_base}/static/firmware/{firmware_file}" if firmware_file else ""
    response: dict = {
        "websocket": {
            "url": ws_url,
            "token": "",
            "version": 1,
        },
        "server_time": {
            "timestamp": now_ms,
            "timezone_offset": 420,
        },
        "firmware": {
            "version": firmware_version,
            "url": firmware_url,
            "force": 0,
        },
    }

    logger.info("OTA bootstrap for mac=%s -> websocket.url=%s firmware.url=%s", mac or "?", ws_url, firmware_url)

    if mac:
        robot = _ensure_robot(mac)
        update_robot_status(mac, True)

        if robot and not robot.owner_username:
            otp = generate_otp(mac, ttl_minutes=10)
            challenge = uuid.uuid4().hex

            logger.info("Device %s chưa có owner → OTP=%s", mac, otp)

            response["activation"] = {
                "code": otp,
                "message": "Nhập mã này trên web để kích hoạt thiết bị",
                "challenge": challenge,
                "timeout_ms": 30000,
            }
        else:
            logger.info("Device %s đã có owner=%s → bỏ qua activation",
                        mac, robot.owner_username if robot else "?")

    return response
