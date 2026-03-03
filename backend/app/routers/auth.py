import hashlib
import re
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, field_validator
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.core.database import get_db
from app.core.security import (
    hash_password, verify_password, create_access_token, get_current_user
)
from app.models.user import User

router = APIRouter(prefix="/auth", tags=["auth"])


# ── Schemas ────────────────────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    username: str
    email: str
    password: str
    display_name: str | None = None

    @field_validator("username")
    @classmethod
    def username_valid(cls, v: str) -> str:
        v = v.strip().lower()
        if not re.match(r"^[a-z0-9_]{3,30}$", v):
            raise ValueError(
                "Username must be 3-30 characters: letters, numbers, underscores only"
            )
        return v

    @field_validator("password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        if len(v.encode("utf-8")) > 72:
            raise ValueError("Password must be 72 characters or fewer")
        return v

    @field_validator("email")
    @classmethod
    def email_valid(cls, v: str) -> str:
        v = v.strip().lower()
        if "@" not in v or "." not in v.split("@")[-1]:
            raise ValueError("Invalid email address")
        return v


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserResponse(BaseModel):
    id: str
    username: str
    display_name: str | None
    bio: str | None
    avatar_url: str | None
    created_at: str

    model_config = {"from_attributes": True}


# ── Helpers ────────────────────────────────────────────────────────────────

def hash_email(email: str) -> str:
    """One-way hash. We never store the plaintext email."""
    return hashlib.sha256(email.lower().encode()).hexdigest()


# ── Routes ─────────────────────────────────────────────────────────────────

@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def register(data: RegisterRequest, db: AsyncSession = Depends(get_db)):
    # Check username taken
    result = await db.execute(select(User).where(User.username == data.username))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Username already taken")

    # Check email taken (via hash)
    email_hash = hash_email(data.email)
    result = await db.execute(select(User).where(User.email_hash == email_hash))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Email already registered")

    user = User(
        username=data.username,
        email_hash=email_hash,
        password_hash=hash_password(data.password),
        display_name=data.display_name or data.username,
    )
    db.add(user)
    await db.flush()  # get the ID before commit

    token = create_access_token(str(user.id))
    return TokenResponse(access_token=token)


@router.post("/login", response_model=TokenResponse)
async def login(data: LoginRequest, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(User).where(User.username == data.username.lower())
    )
    user = result.scalar_one_or_none()

    if not user or not verify_password(data.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
        )

    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account is deactivated")

    return TokenResponse(access_token=create_access_token(str(user.id)))


@router.get("/me", response_model=UserResponse)
async def get_me(current_user: User = Depends(get_current_user)):
    """Returns the authenticated user's profile. Use this to verify tokens work."""
    return UserResponse(
        id=str(current_user.id),
        username=current_user.username,
        display_name=current_user.display_name,
        bio=current_user.bio,
        avatar_url=current_user.avatar_url,
        created_at=current_user.created_at.isoformat(),
    )


class UpdateProfileRequest(BaseModel):
    display_name: str | None = None
    bio: str | None = None
    avatar_url: str | None = None


@router.patch("/me", response_model=UserResponse)
async def update_profile(
    data: UpdateProfileRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Update the authenticated user's profile."""
    if data.display_name is not None:
        current_user.display_name = data.display_name.strip() or None
    if data.bio is not None:
        current_user.bio = data.bio.strip() or None
    if data.avatar_url is not None:
        current_user.avatar_url = data.avatar_url

    await db.flush()

    return UserResponse(
        id=str(current_user.id),
        username=current_user.username,
        display_name=current_user.display_name,
        bio=current_user.bio,
        avatar_url=current_user.avatar_url,
        created_at=current_user.created_at.isoformat(),
    )
