from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from jose import jwt

from app.core.config import settings

router = APIRouter(tags=["Authentication"])


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


@router.post("/auth/login", response_model=LoginResponse, status_code=status.HTTP_200_OK)
def login(request: LoginRequest):
    """
    Authenticate administrative credentials and generate a JWT access token.
    """
    # Plain comparison for admin unique password (do NOT use passlib/hashers)
    if request.username != settings.admin_username or request.password != settings.admin_password:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password"
        )

    # JWT generation
    expire = datetime.now(timezone.utc) + timedelta(hours=settings.jwt_expire_hours)
    to_encode = {
        "sub": request.username,
        "exp": expire
    }
    access_token = jwt.encode(
        to_encode,
        settings.jwt_secret_key,
        algorithm="HS256"
    )

    return LoginResponse(access_token=access_token)
