from fastapi import APIRouter, UploadFile, File, Form, Depends
from fastapi.responses import JSONResponse
from datetime import datetime
import os
from app.auth.security import check_admin_role

router = APIRouter(prefix="/OTA", tags=["OTA"])

FIRMWARE_DIR = "static/firmware"
os.makedirs(FIRMWARE_DIR, exist_ok=True)

@router.post("/upload", summary="Admin upload firmware")
async def upload_firmware(
    file: UploadFile = File(...),
    version: str = Form(...),
    note: str = Form(""),
    #user=Depends(check_admin_role)
):
    filename = f"{version}_{datetime.now().strftime('%Y%m%d%H%M%S')}_{file.filename}"
    path = os.path.join(FIRMWARE_DIR, filename)
    with open(path, "wb") as f:
        f.write(await file.read())
    # TODO: Save metadata to DB
    return JSONResponse({
        "success": True,
        "filename": filename,
        "url": f"/static/firmware/{filename}",
        "version": version,
        "note": note
    })
