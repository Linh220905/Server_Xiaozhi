from fastapi import APIRouter, HTTPException, Depends, BackgroundTasks
from typing import List, Optional
from datetime import datetime

from ..auth.security import get_current_active_user
from ..auth.models import TokenData
from ..api.auth_google import require_admin, require_viewer
from ..robots.models import RobotCreate, RobotUpdate, RobotInDB, RobotConfigUpdate, RobotConfigInDB
from ..robots.crud import (
    get_robot_by_mac, create_robot, update_robot, delete_robot,
    get_all_robots, update_robot_status, get_robot_config,
    update_robot_config, reset_robot_config,
    generate_otp, claim_robot_by_otp,
)
from pydantic import BaseModel

router = APIRouter(prefix="/robots", tags=["robots"])


@router.get("/", response_model=List[RobotInDB])
async def list_robots(session: dict = Depends(require_viewer)) -> List[RobotInDB]:
    if session.get("role") == "admin":
        return get_all_robots()
    return get_all_robots(owner_username=session.get("email"))


@router.get("/{mac_address}", response_model=RobotInDB)
async def get_robot(mac_address: str, session: dict = Depends(require_viewer)) -> RobotInDB:
    robot = get_robot_by_mac(mac_address)
    if not robot:
        raise HTTPException(status_code=404, detail="Robot not found")
    return robot


@router.post("/", response_model=RobotInDB)
async def create_new_robot(robot: RobotCreate, session: dict = Depends(require_admin)) -> RobotInDB:
    existing_robot = get_robot_by_mac(robot.mac_address)
    if existing_robot:
        raise HTTPException(status_code=400, detail="Robot with this MAC address already exists")

    created_robot = create_robot(robot, owner_username=session.get("email"))
    if not created_robot:
        raise HTTPException(status_code=400, detail="Failed to create robot")
    return created_robot


@router.put("/{mac_address}", response_model=RobotInDB)
async def update_existing_robot(
    mac_address: str,
    robot_update: RobotUpdate,
    session: dict = Depends(require_viewer)
) -> RobotInDB:
    robot = get_robot_by_mac(mac_address)
    if not robot:
        raise HTTPException(status_code=404, detail="Robot not found")

    if session.get("role") != "admin":
        if robot.owner_username != session.get("email"):
            raise HTTPException(status_code=403, detail="You can only edit your own robots")

    updated_robot = update_robot(mac_address, robot_update)
    if not updated_robot:
        raise HTTPException(status_code=400, detail="Failed to update robot")
    return updated_robot


@router.delete("/{mac_address}")
async def delete_existing_robot(mac_address: str, session: dict = Depends(require_admin)) -> dict:
    robot = get_robot_by_mac(mac_address)
    if not robot:
        raise HTTPException(status_code=404, detail="Robot not found")

    deleted = delete_robot(mac_address)
    if not deleted:
        raise HTTPException(status_code=400, detail="Failed to delete robot")
    return {"message": "Robot deleted successfully"}


@router.patch("/{mac_address}/status")
async def update_robot_status_endpoint(
    mac_address: str,
    is_online: bool,
    session: dict = Depends(require_admin)
) -> dict:
    robot = get_robot_by_mac(mac_address)
    if not robot:
        raise HTTPException(status_code=404, detail="Robot not found")

    updated = update_robot_status(mac_address, is_online)
    if not updated:
        raise HTTPException(status_code=400, detail="Failed to update robot status")
    return {"message": f"Robot status updated to {'online' if is_online else 'offline'}"}


@router.get("/{mac_address}/config", response_model=RobotConfigInDB)
async def get_robot_configuration(
    mac_address: str,
    session: dict = Depends(require_viewer)
) -> RobotConfigInDB:
    config = get_robot_config(mac_address)
    if not config:
        raise HTTPException(status_code=404, detail="Robot configuration not found")
    return config


@router.put("/{mac_address}/config", response_model=RobotConfigInDB)
async def update_robot_configuration(
    mac_address: str,
    config_update: RobotConfigUpdate,
    session: dict = Depends(require_viewer)
) -> RobotConfigInDB:
    robot = get_robot_by_mac(mac_address)
    if not robot:
        raise HTTPException(status_code=404, detail="Robot not found")

    if session.get("role") != "admin":
        if robot.owner_username != session.get("email"):
            raise HTTPException(status_code=403, detail="You can only edit your own robots")

    updated_config = update_robot_config(mac_address, config_update)
    if not updated_config:
        raise HTTPException(status_code=400, detail="Failed to update robot configuration")
    return updated_config


@router.post("/{mac_address}/config/reset")
async def reset_robot_configuration(
    mac_address: str,
    session: dict = Depends(require_admin)
) -> dict:
    config = get_robot_config(mac_address)
    if not config:
        raise HTTPException(status_code=404, detail="Robot configuration not found")

    reset = reset_robot_config(mac_address)
    if not reset:
        raise HTTPException(status_code=400, detail="Failed to reset robot configuration")
    return {"message": "Robot configuration reset to default"}


class ClaimRequest(BaseModel):
    otp: str

class ClaimResponse(BaseModel):
    ok: bool
    message: str
    attempts_left: int = -1
    robot: Optional[RobotInDB] = None


ERROR_MESSAGES = {
    "not_found": "Mã OTP không đúng. Vui lòng kiểm tra lại.",
    "locked": "OTP đã bị khoá do nhập sai quá nhiều lần. Vui lòng liên hệ admin.",
    "expired": "OTP đã hết hạn. Vui lòng liên hệ admin để tạo OTP mới.",
}


@router.post("/claim", response_model=ClaimResponse)
async def claim_robot_endpoint(
    body: ClaimRequest,
    session: dict = Depends(require_viewer),
):
    result = claim_robot_by_otp(body.otp, session.get("email", ""))

    if not result["ok"]:
        error = result.get("error", "not_found")
        attempts_left = result.get("attempts_left", -1)
        msg = ERROR_MESSAGES.get(error, "OTP không đúng hoặc đã hết hạn")
        if error == "wrong" and attempts_left > 0:
            msg = f"OTP không đúng. Còn {attempts_left} lần thử."
        return ClaimResponse(ok=False, message=msg, attempts_left=attempts_left)

    return ClaimResponse(
        ok=True,
        message="Claim robot thành công!",
        attempts_left=result.get("attempts_left", -1),
        robot=result.get("robot"),
    )


@router.post("/{mac_address}/otp")
async def regenerate_otp_endpoint(
    mac_address: str,
    session: dict = Depends(require_admin),
):
    otp = generate_otp(mac_address)
    if not otp:
        raise HTTPException(status_code=404, detail="Robot not found")
    return {"otp": otp, "message": f"New OTP generated for {mac_address}"}
