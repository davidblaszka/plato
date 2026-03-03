import uuid
import logging
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel, field_validator

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.models.profile_post import ProfilePost
from app.models.social import PublicAccountFollow
from app.models.connection import Connection
from sqlalchemy import func

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/users", tags=["users"])


class UserProfileResponse(BaseModel):
    id: str
    username: str
    display_name: str | None
    bio: str | None
    avatar_url: str | None
    account_type: str
    is_verified: bool
    follower_count: int       # public account followers
    connection_count: int     # mutual connections (personal accounts)
    post_count: int
    connection_status: str | None   # null | pending | accepted (viewer's relationship)
    created_at: str


class ProfilePostResponse(BaseModel):
    id: str
    author_id: str
    author_username: str
    author_display_name: str | None
    author_avatar_url: str | None
    content: str
    media_urls: list[str]
    is_edited: bool
    created_at: str


class CreateProfilePostRequest(BaseModel):
    content: str
    media_urls: list[str] = []

    @field_validator("content")
    @classmethod
    def content_not_empty(cls, v):
        v = v.strip()
        if not v:
            raise ValueError("Content cannot be empty")
        if len(v) > 10000:
            raise ValueError("Content too long")
        return v


def parse_uuid(user_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(user_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=404, detail="User not found")


def format_profile_post(post: ProfilePost, author: User) -> ProfilePostResponse:
    return ProfilePostResponse(
        id=str(post.id),
        author_id=str(author.id),
        author_username=author.username,
        author_display_name=author.display_name,
        author_avatar_url=author.avatar_url,
        content=post.content,
        media_urls=post.media_urls or [],
        is_edited=post.is_edited,
        created_at=post.created_at.isoformat(),
    )


async def get_user_or_404(user_id: str, db: AsyncSession) -> User:
    uid = parse_uuid(user_id)
    result = await db.execute(select(User).where(User.id == uid))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


@router.get("/{user_id}", response_model=UserProfileResponse)
async def get_user_profile(
    user_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    user = await get_user_or_404(user_id, db)
    uid = user.id  # use the resolved UUID from the fetched user
    # Follower count (public accounts)
    follower_result = await db.execute(
        select(func.count()).where(PublicAccountFollow.followed_id == uid)
    )
    follower_count = follower_result.scalar() or 0

    # Connection count (accepted mutual connections)
    conn_result = await db.execute(
        select(func.count()).where(
            Connection.status == "accepted",
            (Connection.requester_id == uid) | (Connection.addressee_id == uid)
        )
    )
    connection_count = conn_result.scalar() or 0

    # Post count
    post_result = await db.execute(
        select(func.count()).where(ProfilePost.author_id == uid)
    )
    post_count = post_result.scalar() or 0

    # Connection status between viewer and this profile
    conn_status_result = await db.execute(
        select(Connection).where(
            ((Connection.requester_id == current_user.id) & (Connection.addressee_id == uid)) |
            ((Connection.requester_id == uid) & (Connection.addressee_id == current_user.id))
        )
    )
    conn_status_row = conn_status_result.scalar_one_or_none()
    connection_status = conn_status_row.status if conn_status_row else None

    return UserProfileResponse(
        id=str(user.id),
        username=user.username,
        display_name=user.display_name,
        bio=user.bio,
        avatar_url=user.avatar_url,
        account_type=user.account_type,
        is_verified=user.is_verified,
        follower_count=follower_count,
        connection_count=connection_count,
        post_count=post_count,
        connection_status=connection_status,
        created_at=user.created_at.isoformat(),
    )


@router.get("/{user_id}/profile-posts", response_model=list[ProfilePostResponse])
async def get_profile_posts(
    user_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    user = await get_user_or_404(user_id, db)
    uid = parse_uuid(user_id)
    result = await db.execute(
        select(ProfilePost)
        .where(ProfilePost.author_id == uid)
        .order_by(ProfilePost.created_at.desc())
        .limit(50)
    )
    posts = result.scalars().all()
    return [format_profile_post(p, user) for p in posts]


@router.post("/{user_id}/profile-posts", response_model=ProfilePostResponse, status_code=201)
async def create_profile_post(
    user_id: str,
    data: CreateProfilePostRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    uid = parse_uuid(user_id)
    if uid != current_user.id:
        raise HTTPException(status_code=403, detail="Cannot post to another user's profile")
    post = ProfilePost(
        author_id=current_user.id,
        content=data.content,
        media_urls=data.media_urls,
    )
    db.add(post)
    await db.flush()
    await db.refresh(post)
    return format_profile_post(post, current_user)


@router.delete("/{user_id}/profile-posts/{post_id}", status_code=204)
async def delete_profile_post(
    user_id: str,
    post_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    uid = parse_uuid(user_id)
    pid = parse_uuid(post_id)
    if uid != current_user.id:
        raise HTTPException(status_code=403, detail="Cannot delete another user's post")
    result = await db.execute(
        select(ProfilePost).where(ProfilePost.id == pid, ProfilePost.author_id == uid)
    )
    post = result.scalar_one_or_none()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    await db.delete(post)


# ── Connection actions from profile ───────────────────────────────────────

@router.post("/{user_id}/connect", status_code=201)
async def send_connection_request(
    user_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    uid = parse_uuid(user_id)
    if uid == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot connect with yourself")

    # Check not already connected/pending
    existing = await db.execute(
        select(Connection).where(
            ((Connection.requester_id == current_user.id) & (Connection.addressee_id == uid)) |
            ((Connection.requester_id == uid) & (Connection.addressee_id == current_user.id))
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Connection already exists")

    conn = Connection(requester_id=current_user.id, addressee_id=uid, status="pending")
    db.add(conn)
    await db.flush()
    return {"status": "pending"}


@router.delete("/{user_id}/connect", status_code=200)
async def remove_connection(
    user_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    uid = parse_uuid(user_id)
    result = await db.execute(
        select(Connection).where(
            ((Connection.requester_id == current_user.id) & (Connection.addressee_id == uid)) |
            ((Connection.requester_id == uid) & (Connection.addressee_id == current_user.id))
        )
    )
    conn = result.scalar_one_or_none()
    if conn:
        await db.delete(conn)
    return {"status": None}


# ── Profile post vote ──────────────────────────────────────────────────────

@router.post("/profile-posts/{post_id}/vote", status_code=200)
async def toggle_profile_post_vote(
    post_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from app.models.social import ProfilePostVote
    pid = parse_uuid(post_id)

    result = await db.execute(select(ProfilePost).where(ProfilePost.id == pid))
    post = result.scalar_one_or_none()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    existing = await db.execute(
        select(ProfilePostVote).where(
            ProfilePostVote.user_id == current_user.id,
            ProfilePostVote.post_id == pid,
        )
    )
    vote = existing.scalar_one_or_none()
    if vote:
        await db.delete(vote)
        post.upvote_count = max(0, post.upvote_count - 1)
        has_voted = False
    else:
        db.add(ProfilePostVote(user_id=current_user.id, post_id=pid))
        post.upvote_count += 1
        has_voted = True

    await db.flush()
    return {"upvote_count": post.upvote_count, "has_voted": has_voted}
