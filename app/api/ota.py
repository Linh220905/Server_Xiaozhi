import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Request

from app.robots.crud import get_robot_by_mac, create_robot, generate_otp
from app.robots.models import RobotCreate

logger = logging.getLogger(__name__)
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
    host = request.headers.get("host", "127.0.0.1:8000")
    mac = request.headers.get("device-id", "").strip()

    ws_url = f"ws://{host}"
    now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)

    # Lấy firmware mới nhất trong static/firmware
    import os
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
    firmware_url = f"http://{host}/static/firmware/{firmware_file}" if firmware_file else ""
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

    if mac:
        robot = _ensure_robot(mac)

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
