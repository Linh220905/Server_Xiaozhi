from pydantic import BaseModel
from typing import Optional
from enum import Enum


class UserRole(str, Enum):
    ADMIN = "admin"
    USER = "user"


class UserBase(BaseModel):
    username: str
    role: UserRole = UserRole.USER


class UserCreate(UserBase):
    password: str


class UserUpdate(BaseModel):
    password: Optional[str] = None
    role: Optional[UserRole] = None


class UserInDB(UserBase):
    id: int
    password_hash: str
    created_at: str
    updated_at: str


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class TokenData(BaseModel):
    username: Optional[str] = None
    role: Optional[str] = None