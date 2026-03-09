import math
import uuid
import base64
import logging
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel, field_validator

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.models.profile_post import ProfilePost, ProfilePostComment
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
    content: str | None
    media_urls: list[str]
    is_edited: bool
    heart_count: int = 0
    has_hearted: bool = False
    comment_count: int = 0
    created_at: str


class CreateProfilePostRequest(BaseModel):
    content: str | None = None
    media_urls: list[str] = []

    @field_validator("content")
    @classmethod
    def content_valid(cls, v):
        if v is None:
            return v
        v = v.strip()
        if len(v) > 10000:
            raise ValueError("Content too long")
        return v or None  # normalise empty string to None

    def has_content(self) -> bool:
        return bool(self.content) or bool(self.media_urls)


def parse_uuid(user_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(user_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=404, detail="User not found")


def format_profile_post(post: ProfilePost, author: User, has_hearted: bool = False) -> ProfilePostResponse:
    return ProfilePostResponse(
        id=str(post.id),
        author_id=str(author.id),
        author_username=author.username,
        author_display_name=author.display_name,
        author_avatar_url=author.avatar_url,
        content=post.content,
        media_urls=post.media_urls or [],
        is_edited=post.is_edited,
        heart_count=post.heart_count,
        has_hearted=has_hearted,
        comment_count=post.comment_count,
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
    from app.models.social import ProfilePostHeart
    user = await get_user_or_404(user_id, db)
    uid = parse_uuid(user_id)
    result = await db.execute(
        select(ProfilePost)
        .where(ProfilePost.author_id == uid)
        .order_by(ProfilePost.created_at.desc())
        .limit(50)
    )
    posts = result.scalars().all()
    if not posts:
        return []

    post_ids = [p.id for p in posts]
    hearted_result = await db.execute(
        select(ProfilePostHeart.post_id).where(
            ProfilePostHeart.user_id == current_user.id,
            ProfilePostHeart.post_id.in_(post_ids),
        )
    )
    hearted_ids = set(hearted_result.scalars().all())

    return [format_profile_post(p, user, has_hearted=p.id in hearted_ids) for p in posts]


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
    if not data.has_content():
        raise HTTPException(status_code=422, detail="Post must have text or at least one image")
    post = ProfilePost(
        author_id=current_user.id,
        content=data.content,
        media_urls=data.media_urls,
    )
    db.add(post)
    await db.flush()
    await db.refresh(post)
    post.hot_score = round(math.log10(1) + post.created_at.timestamp() / 45000, 7)
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
    from app.models.social import ProfilePostHeart
    pid = parse_uuid(post_id)

    result = await db.execute(select(ProfilePost).where(ProfilePost.id == pid))
    post = result.scalar_one_or_none()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    existing = await db.execute(
        select(ProfilePostHeart).where(
            ProfilePostHeart.user_id == current_user.id,
            ProfilePostHeart.post_id == pid,
        )
    )
    vote = existing.scalar_one_or_none()
    if vote:
        await db.delete(vote)
        post.heart_count = max(0, post.heart_count - 1)
        has_hearted = False
    else:
        db.add(ProfilePostHeart(user_id=current_user.id, post_id=pid))
        post.heart_count += 1
        has_hearted = True

    post.hot_score = round(
        math.log10(max(post.heart_count, 1)) + post.created_at.timestamp() / 45000, 7
    )
    await db.flush()
    return {"heart_count": post.heart_count, "has_hearted": has_hearted}


# ── Profile post comments ──────────────────────────────────────────────────

class ProfilePostCommentResponse(BaseModel):
    id: str
    author_id: str
    author_username: str
    author_display_name: str | None
    author_avatar_url: str | None
    content: str
    created_at: str


class CreateProfilePostCommentRequest(BaseModel):
    content: str

    @field_validator("content")
    @classmethod
    def content_valid(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Comment cannot be empty")
        if len(v) > 2000:
            raise ValueError("Comment cannot exceed 2,000 characters")
        return v


def format_profile_post_comment(comment: ProfilePostComment, author: User) -> ProfilePostCommentResponse:
    return ProfilePostCommentResponse(
        id=str(comment.id),
        author_id=str(author.id),
        author_username=author.username,
        author_display_name=author.display_name,
        author_avatar_url=author.avatar_url,
        content=comment.content,
        created_at=comment.created_at.isoformat(),
    )


@router.get("/profile-posts/{post_id}/comments", response_model=list[ProfilePostCommentResponse])
async def list_profile_post_comments(
    post_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    pid = parse_uuid(post_id)
    result = await db.execute(
        select(ProfilePost).where(ProfilePost.id == pid)
    )
    post = result.scalar_one_or_none()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    comments_result = await db.execute(
        select(ProfilePostComment)
        .where(ProfilePostComment.post_id == pid)
        .order_by(ProfilePostComment.created_at.asc())
        .limit(200)
    )
    comments = comments_result.scalars().all()
    if not comments:
        return []

    author_ids = list({c.author_id for c in comments})
    authors_result = await db.execute(select(User).where(User.id.in_(author_ids)))
    authors = {u.id: u for u in authors_result.scalars().all()}

    return [format_profile_post_comment(c, authors[c.author_id]) for c in comments if c.author_id in authors]


@router.post("/profile-posts/{post_id}/comments", response_model=ProfilePostCommentResponse, status_code=201)
async def add_profile_post_comment(
    post_id: str,
    data: CreateProfilePostCommentRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    pid = parse_uuid(post_id)
    result = await db.execute(select(ProfilePost).where(ProfilePost.id == pid))
    post = result.scalar_one_or_none()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    comment = ProfilePostComment(
        post_id=pid,
        author_id=current_user.id,
        content=data.content,
    )
    db.add(comment)
    post.comment_count += 1
    await db.flush()
    await db.refresh(comment)
    return format_profile_post_comment(comment, current_user)


@router.delete("/profile-posts/{post_id}/comments/{comment_id}", status_code=204)
async def delete_profile_post_comment(
    post_id: str,
    comment_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    pid = parse_uuid(post_id)
    cid = parse_uuid(comment_id)

    result = await db.execute(
        select(ProfilePostComment).where(
            ProfilePostComment.id == cid,
            ProfilePostComment.post_id == pid,
        )
    )
    comment = result.scalar_one_or_none()
    if not comment:
        raise HTTPException(status_code=404, detail="Comment not found")
    if comment.author_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not your comment")

    post_result = await db.execute(select(ProfilePost).where(ProfilePost.id == pid))
    post = post_result.scalar_one_or_none()
    if post:
        post.comment_count = max(0, post.comment_count - 1)

    await db.delete(comment)
    await db.flush()


# ── E2EE public key endpoints ───────────────────────────────────────────────

class PublicKeyResponse(BaseModel):
    user_id: str
    public_key: str   # base64-encoded X25519 public key (44 chars = 32 bytes)


class UpdatePublicKeyRequest(BaseModel):
    public_key: str

    @field_validator("public_key")
    @classmethod
    def validate_public_key(cls, v: str) -> str:
        try:
            decoded = base64.b64decode(v)
            if len(decoded) != 32:
                raise ValueError("Public key must be 32 bytes")
        except Exception:
            raise ValueError("Invalid base64 public key — must be 32 bytes base64-encoded")
        return v


@router.get("/{user_id}/public-key", response_model=PublicKeyResponse)
async def get_public_key(
    user_id: str,
    db: AsyncSession = Depends(get_db),
    # No auth required — public keys are not secret
):
    """Return a user's X25519 public key for E2EE key exchange.

    Returns 404 if the user has not yet generated their E2EE keypair
    (i.e. they haven't logged in with an E2EE-capable app version yet).
    """
    user = await get_user_or_404(user_id, db)
    if not user.public_key:
        raise HTTPException(status_code=404, detail="User has not set up E2EE keys yet")
    return PublicKeyResponse(user_id=str(user.id), public_key=user.public_key)


@router.patch("/{user_id}/public-key", response_model=PublicKeyResponse)
async def upload_public_key(
    user_id: str,
    data: UpdatePublicKeyRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Upload or rotate the caller's X25519 public key.

    Only the authenticated user can upload their own key.
    """
    uid = parse_uuid(user_id)
    if uid != current_user.id:
        raise HTTPException(status_code=403, detail="Can only upload your own public key")

    current_user.public_key = data.public_key
    await db.flush()
    return PublicKeyResponse(user_id=str(current_user.id), public_key=current_user.public_key)
