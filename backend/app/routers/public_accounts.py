import uuid
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete
from pydantic import BaseModel

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.models.social import PublicAccountFollow
from app.models.profile_post import ProfilePost

router = APIRouter(prefix="/public-accounts", tags=["public-accounts"])


class FollowStatusResponse(BaseModel):
    is_following: bool
    follower_count: int


class ConvertAccountRequest(BaseModel):
    account_type: str  # "public" or "personal"


# ── Follow / Unfollow ──────────────────────────────────────────────────────

@router.post("/{user_id}/follow", response_model=FollowStatusResponse)
async def follow_public_account(
    user_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        uid = uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="User not found")

    if uid == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot follow yourself")

    # Verify target is a public account
    result = await db.execute(select(User).where(User.id == uid))
    target = result.scalar_one_or_none()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if target.account_type != "public":
        raise HTTPException(status_code=400, detail="Can only follow public accounts")

    # Check not already following
    existing = await db.execute(
        select(PublicAccountFollow).where(
            PublicAccountFollow.follower_id == current_user.id,
            PublicAccountFollow.followed_id == uid,
        )
    )
    if not existing.scalar_one_or_none():
        db.add(PublicAccountFollow(follower_id=current_user.id, followed_id=uid))
        await db.flush()

    count_result = await db.execute(
        select(PublicAccountFollow).where(PublicAccountFollow.followed_id == uid)
    )
    count = len(count_result.scalars().all())
    return FollowStatusResponse(is_following=True, follower_count=count)


@router.delete("/{user_id}/follow", response_model=FollowStatusResponse)
async def unfollow_public_account(
    user_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        uid = uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="User not found")

    await db.execute(
        delete(PublicAccountFollow).where(
            PublicAccountFollow.follower_id == current_user.id,
            PublicAccountFollow.followed_id == uid,
        )
    )
    await db.flush()

    count_result = await db.execute(
        select(PublicAccountFollow).where(PublicAccountFollow.followed_id == uid)
    )
    count = len(count_result.scalars().all())
    return FollowStatusResponse(is_following=False, follower_count=count)


@router.get("/{user_id}/follow-status", response_model=FollowStatusResponse)
async def get_follow_status(
    user_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        uid = uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="User not found")

    existing = await db.execute(
        select(PublicAccountFollow).where(
            PublicAccountFollow.follower_id == current_user.id,
            PublicAccountFollow.followed_id == uid,
        )
    )
    is_following = existing.scalar_one_or_none() is not None

    count_result = await db.execute(
        select(PublicAccountFollow).where(PublicAccountFollow.followed_id == uid)
    )
    count = len(count_result.scalars().all())
    return FollowStatusResponse(is_following=is_following, follower_count=count)


# ── Account type management ────────────────────────────────────────────────

@router.post("/convert", status_code=200)
async def convert_account_type(
    data: ConvertAccountRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if data.account_type not in ("public", "personal"):
        raise HTTPException(status_code=400, detail="account_type must be 'public' or 'personal'")

    result = await db.execute(select(User).where(User.id == current_user.id))
    user = result.scalar_one()
    user.account_type = data.account_type
    await db.flush()
    return {"account_type": user.account_type}


# ── Public accounts feed ───────────────────────────────────────────────────

class PublicFeedPost(BaseModel):
    id: str
    author_id: str
    author_username: str
    author_display_name: str | None
    author_avatar_url: str | None
    author_is_verified: bool
    content: str
    media_urls: list[str]
    heart_count: int
    is_edited: bool
    created_at: str


@router.get("/feed", response_model=list[PublicFeedPost])
async def get_public_feed(
    sort: str = "new",
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Get IDs of public accounts the current user follows
    follows_result = await db.execute(
        select(PublicAccountFollow.followed_id).where(
            PublicAccountFollow.follower_id == current_user.id
        )
    )
    followed_ids = [row[0] for row in follows_result.all()]

    if not followed_ids:
        return []

    stmt = (
        select(ProfilePost, User)
        .join(User, ProfilePost.author_id == User.id)
        .where(ProfilePost.author_id.in_(followed_ids))
    )

    if sort == "top":
        stmt = stmt.order_by(ProfilePost.heart_count.desc(), ProfilePost.created_at.desc())
    else:
        stmt = stmt.order_by(ProfilePost.created_at.desc())

    stmt = stmt.limit(100)
    result = await db.execute(stmt)

    return [
        PublicFeedPost(
            id=str(p.id),
            author_id=str(u.id),
            author_username=u.username,
            author_display_name=u.display_name,
            author_avatar_url=u.avatar_url,
            author_is_verified=u.is_verified,
            content=p.content,
            media_urls=p.media_urls or [],
            heart_count=p.heart_count,
            is_edited=p.is_edited,
            created_at=p.created_at.isoformat(),
        )
        for p, u in result.all()
    ]
