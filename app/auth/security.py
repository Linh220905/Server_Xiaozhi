"""
Security utilities for authentication and authorization.
"""
from datetime import datetime, timedelta
from typing import Optional
from jose import jwt, JWTError
import bcrypt
import logging
from fastapi import HTTPException, status, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from .models import TokenData, UserRole
from ..config import config


# Secret key for JWT encoding/decoding (ưu tiên từ biến môi trường)
import os
SECRET_KEY = os.environ.get("JWT_SECRET", "changeme-please-set-JWT_SECRET")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30


security = HTTPBearer()
logger = logging.getLogger(__name__)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a plain password against its hash.

    Supports bcrypt hashes and legacy plaintext passwords.
    """
    if not hashed_password:
        return False

    try:
        return bcrypt.checkpw(plain_password.encode('utf-8'), hashed_password.encode('utf-8'))
    except (ValueError, TypeError):
        # Legacy compatibility: old records may store plaintext or invalid hash format.
        logger.warning("Encountered non-bcrypt password format; using legacy plaintext fallback")
        return plain_password == hashed_password


def get_password_hash(password: str) -> str:
    """Hash a password."""
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """Create a JWT access token."""
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=15)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt


from fastapi import Request
async def get_current_user(request: Request, credentials: HTTPAuthorizationCredentials = Depends(security)) -> TokenData:
    """Get current user from token or session cookie."""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    token = None
    # Ưu tiên Bearer token
    if credentials and credentials.credentials:
        token = credentials.credentials
    # Nếu không có Bearer, lấy từ cookie nexus_session
    if not token:
        token = request.cookies.get("nexus_session")
    if not token:
        raise credentials_exception
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: Optional[str] = payload.get("sub") or payload.get("email")
        role: Optional[str] = payload.get("role")
        if username is None:
            raise credentials_exception
        token_data = TokenData(username=username, role=role)
    except JWTError:
        raise credentials_exception
    return token_data


async def get_current_active_user(current_user: TokenData = Depends(get_current_user)):
    """Get current active user (can be extended with additional checks)."""
    # In a real implementation, you would fetch user details from the database
    # and check if the user is active, not suspended, etc.
    return current_user


def check_admin_role(current_user: TokenData = Depends(get_current_user)):
    """Check if current user has admin role."""
    # Nếu token không có role, mặc định không phải admin
    role = getattr(current_user, "role", None)
    if role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Operation not allowed, admin role required"
        )
    return current_user