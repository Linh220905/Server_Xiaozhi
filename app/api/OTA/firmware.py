from fastapi import APIRouter, UploadFile, File, Depends, HTTPException
from fastapi.responses import JSONResponse
from app.auth.security import check_admin_role
from pathlib import Path
import shutil
import os

router = APIRouter(prefix="/OTA", tags=["OTA"])

FIRMWARE_DIR = Path("static/firmware")
FIRMWARE_DIR.mkdir(parents=True, exist_ok=True)

@router.post("/upload_firmware", summary="Admin upload firmware file", response_class=JSONResponse)
async def upload_firmware(
    file: UploadFile = File(...),
    current_admin=Depends(check_admin_role)
):
    if not file.filename.endswith(".bin"):
        raise HTTPException(status_code=400, detail="Chỉ chấp nhận file .bin")
    dest = FIRMWARE_DIR / file.filename
    with dest.open("wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
    # TODO: Lưu metadata vào database
    return {"success": True, "filename": file.filename, "url": f"/static/firmware/{file.filename}"}
