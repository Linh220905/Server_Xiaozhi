from fastapi import Depends, HTTPException
from app.auth.dependencies import get_current_active_admin

def require_admin(current_admin=Depends(get_current_active_admin)):
    if not current_admin:
        raise HTTPException(status_code=403, detail="Admin quyền hạn cần thiết.")
    return current_admin
