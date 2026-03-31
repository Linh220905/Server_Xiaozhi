from fastapi import APIRouter, HTTPException, status, Request, Form
from fastapi.responses import JSONResponse
from datetime import timedelta
from app.auth.schemas import UserCreate, UserLogin, Token
from app.auth.crud import create_user, authenticate_user
from app.auth.security import create_access_token
from app.auth.models import UserCreate as DbUserCreate
from app.api.auth_google import create_session_token
from app.api.session_utils import set_auth_cookie

router = APIRouter(prefix="/auth", tags=["Authentication"])


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(user_data: UserCreate):
    user = create_user(DbUserCreate(username=user_data.username, password=user_data.password))
    if not user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username already registered"
        )
    return {"message": "User created successfully"}

@router.post("/login", response_model=Token)
async def login(
    request: Request,
    username: str | None = Form(default=None),
    password: str | None = Form(default=None),
):
    if username is None or password is None:
        try:
            payload = await request.json()
            username = payload.get("username")
            password = payload.get("password")
        except Exception:
            pass

    if not username or not password:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="username and password are required",
        )

    user = authenticate_user(username, password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    access_token = create_access_token(
        data={
            "sub": user.username,
            "role": getattr(user, "role", "user")
        },
        expires_delta=timedelta(minutes=30)
    )

    session_token = create_session_token(
        email=user.username,
        role=getattr(user, "role", "user")
    )

    response = JSONResponse(content={
        "access_token": access_token,
        "token_type": "bearer"
    })


    set_auth_cookie(response, session_token)

    return response