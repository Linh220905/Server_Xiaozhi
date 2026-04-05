from app.server_logging import get_logger
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.robots.crud import get_robot_by_mac

logger = get_logger(__name__)
router = APIRouter(tags=["ota-activate"])


@router.api_route("/nexus/ota/activate", methods=["POST"])
@router.api_route("/nexus/ota/activate/", methods=["POST"])
async def activate_robot(request: Request):
    mac = request.headers.get("device-id", "").strip()

    if not mac:
        try:
            body = await request.json()
            mac = body.get("mac_address", "").strip()
        except Exception:
            pass

    if not mac:
        return JSONResponse(
            status_code=400,
            content={"error": "Missing Device-Id header or mac_address in body"},
        )

    robot = get_robot_by_mac(mac)
    if not robot:
        return JSONResponse(
            status_code=404,
            content={"error": "Device not registered"},
        )

    if robot.owner_username:
        logger.info("Device %s activated – owner=%s", mac, robot.owner_username)
        return JSONResponse(
            status_code=200,
            content={
                "result": "success",
                "message": f"Thiết bị đã được kích hoạt bởi {robot.owner_username}",
            },
        )

    logger.debug("Device %s polling activate – still waiting for claim", mac)
    return JSONResponse(
        status_code=202,
        content={
            "result": "waiting",
            "message": "Đang chờ người dùng nhập mã kích hoạt trên web",
        },
    )
