from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, status, Query
from pydantic import BaseModel, field_validator
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_, and_
from typing import Optional

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.models.connection import Connection
from app.models.profile_post import ProfilePost, ProfilePostComment, ProfilePostCommentHeart
from app.models.social import ProfilePostHeart
from app.services.notifications import create_notification

router = APIRouter(tags=["connections"])


# ── Schemas ────────────────────────────────────────────────────────────────

class UserSummary(BaseModel):
    id: str
    username: str
    display_name: str | None
    avatar_url: str | None
    account_type: str = 'personal'


class ConnectionResponse(BaseModel):
    id: str
    user: UserSummary       # the other person (not the current user)
    status: str
    direction: str          # 'sent' or 'received'
    created_at: str


class ConnectionPostCommentResponse(BaseModel):
    id: str
    author: UserSummary
    content: str
    parent_id: str | None = None
    created_at: str
    heart_count: int = 0
    has_hearted: bool = False
    replies: list["ConnectionPostCommentResponse"] = []


class ConnectionPostResponse(BaseModel):
    id: str
    author: UserSummary
    content: str | None
    media_urls: list[str] = []
    is_edited: bool
    heart_count: int = 0
    has_hearted: bool = False
    comment_count: int = 0
    created_at: str


class CreateConnectionPostRequest(BaseModel):
    content: str | None = None
    media_urls: list[str] = []

    @field_validator("content")
    @classmethod
    def content_valid(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip()
        if len(v) > 10000:
            raise ValueError("Content cannot exceed 10,000 characters")
        return v or None

    def has_content(self) -> bool:
        return bool(self.content) or bool(self.media_urls)


# ── Helpers ────────────────────────────────────────────────────────────────

def format_user(user: User) -> UserSummary:
    return UserSummary(
        id=str(user.id),
        username=user.username,
        display_name=user.display_name,
        avatar_url=user.avatar_url,
        account_type=user.account_type,
    )


async def get_connection(
    user_a_id, user_b_id, db: AsyncSession
) -> Connection | None:
    """Find a connection between two users in either direction."""
    result = await db.execute(
        select(Connection).where(
            or_(
                and_(
                    Connection.requester_id == user_a_id,
                    Connection.addressee_id == user_b_id,
                ),
                and_(
                    Connection.requester_id == user_b_id,
                    Connection.addressee_id == user_a_id,
                ),
            )
        )
    )
    return result.scalar_one_or_none()


async def get_accepted_connection_ids(user_id, db: AsyncSession) -> list:
    """Return list of user IDs that are accepted connections."""
    result = await db.execute(
        select(Connection).where(
            and_(
                or_(
                    Connection.requester_id == user_id,
                    Connection.addressee_id == user_id,
                ),
                Connection.status == "accepted",
            )
        )
    )
    connections = result.scalars().all()
    ids = []
    for c in connections:
        other = c.addressee_id if c.requester_id == user_id else c.requester_id
        ids.append(other)
    return ids


# ── User search ────────────────────────────────────────────────────────────

@router.get("/users/search", response_model=list[UserSummary])
async def search_users(
    q: str = Query(min_length=2, description="Username to search for"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Search for users by username. Excludes yourself."""
    result = await db.execute(
        select(User).where(
            and_(
                User.username.ilike(f"%{q.lower()}%"),
                User.id != current_user.id,
                User.is_active == True,
            )
        ).limit(20)
    )
    users = result.scalars().all()
    return [format_user(u) for u in users]


# ── Connection requests ────────────────────────────────────────────────────

@router.post("/connections/request/{username}", response_model=ConnectionResponse)
async def send_request(
    username: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Send a connection request to another user."""
    result = await db.execute(
        select(User).where(User.username == username.lower())
    )
    addressee = result.scalar_one_or_none()
    if not addressee:
        raise HTTPException(status_code=404, detail="User not found")

    if addressee.id == current_user.id:
        raise HTTPException(status_code=400, detail="You cannot connect with yourself")

    existing = await get_connection(current_user.id, addressee.id, db)
    if existing:
        if existing.status == "accepted":
            raise HTTPException(status_code=409, detail="Already connected")
        if existing.status == "pending":
            raise HTTPException(status_code=409, detail="Request already sent")
        if existing.status == "blocked":
            raise HTTPException(status_code=403, detail="Cannot send request")

    conn = Connection(
        requester_id=current_user.id,
        addressee_id=addressee.id,
        status="pending",
    )
    db.add(conn)
    await db.flush()
    await db.refresh(conn)

    # Notify the recipient
    await create_notification(
        db, addressee.id,
        type="connection_request",
        reference_id=conn.id,
        reference_type="connection",
        actor_id=current_user.id,
    )

    return ConnectionResponse(
        id=str(conn.id),
        user=format_user(addressee),
        status="pending",
        direction="sent",
        created_at=conn.created_at.isoformat(),
    )


@router.post("/connections/accept/{connection_id}", response_model=ConnectionResponse)
async def accept_request(
    connection_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Accept a pending connection request."""
    result = await db.execute(
        select(Connection).where(Connection.id == connection_id)
    )
    conn = result.scalar_one_or_none()

    if not conn:
        raise HTTPException(status_code=404, detail="Request not found")
    if conn.addressee_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not your request to accept")
    if conn.status != "pending":
        raise HTTPException(status_code=400, detail="Request is not pending")

    conn.status = "accepted"
    await db.flush()

    requester_result = await db.execute(select(User).where(User.id == conn.requester_id))
    requester = requester_result.scalar_one()

    # Notify the requester that their request was accepted
    await create_notification(
        db, conn.requester_id,
        type="connection_accepted",
        reference_id=conn.id,
        reference_type="connection",
        actor_id=current_user.id,
    )

    return ConnectionResponse(
        id=str(conn.id),
        user=format_user(requester),
        status="accepted",
        direction="received",
        created_at=conn.created_at.isoformat(),
    )


@router.delete("/connections/decline/{connection_id}", status_code=204)
async def decline_or_remove(
    connection_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Decline a pending request OR remove an existing connection."""
    result = await db.execute(
        select(Connection).where(Connection.id == connection_id)
    )
    conn = result.scalar_one_or_none()
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found")

    is_involved = (
        conn.requester_id == current_user.id or
        conn.addressee_id == current_user.id
    )
    if not is_involved:
        raise HTTPException(status_code=403, detail="Not your connection")

    await db.delete(conn)


@router.get("/connections", response_model=list[ConnectionResponse])
async def list_connections(
    status: Optional[str] = Query(default=None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    List connections for the current user.
    ?status=accepted  → your connections
    ?status=pending   → requests (sent and received)
    """
    query = select(Connection).where(
        or_(
            Connection.requester_id == current_user.id,
            Connection.addressee_id == current_user.id,
        )
    )
    if status:
        query = query.where(Connection.status == status)

    result = await db.execute(query.order_by(Connection.created_at.desc()))
    connections = result.scalars().all()

    # Fetch all involved users in one query
    other_ids = [
        c.addressee_id if c.requester_id == current_user.id else c.requester_id
        for c in connections
    ]
    if not other_ids:
        return []

    users_result = await db.execute(select(User).where(User.id.in_(other_ids)))
    users = {u.id: u for u in users_result.scalars().all()}

    return [
        ConnectionResponse(
            id=str(c.id),
            user=format_user(users[
                c.addressee_id if c.requester_id == current_user.id else c.requester_id
            ]),
            status=c.status,
            direction="sent" if c.requester_id == current_user.id else "received",
            created_at=c.created_at.isoformat(),
        )
        for c in connections
        if (c.addressee_id if c.requester_id == current_user.id else c.requester_id) in users
    ]


# ── Connections feed posts ─────────────────────────────────────────────────

@router.post("/connections/feed/posts", response_model=ConnectionPostResponse, status_code=201)
async def create_connection_post(
    data: CreateConnectionPostRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Post to your connections feed (stored as a profile post)."""
    import math
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

    return ConnectionPostResponse(
        id=str(post.id),
        author=format_user(current_user),
        content=post.content,
        media_urls=post.media_urls or [],
        is_edited=post.is_edited,
        heart_count=0,
        has_hearted=False,
        comment_count=0,
        created_at=post.created_at.isoformat(),
    )


class ConnectionsFeedPage(BaseModel):
    posts: list[ConnectionPostResponse]
    has_more: bool
    next_cursor: str | None


@router.get("/connections/feed", response_model=ConnectionsFeedPage)
async def get_connections_feed(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    limit: int = Query(default=20, le=100),
    before: str | None = Query(default=None),
):
    """
    The private connections feed — posts from you and your mutual connections.
    Chronological. No algorithm. Cursor-paginated via `before`.
    """
    connection_ids = await get_accepted_connection_ids(current_user.id, db)

    # Include your own posts in your feed
    visible_author_ids = connection_ids + [current_user.id]

    stmt = select(ProfilePost).where(ProfilePost.author_id.in_(visible_author_ids))
    if before is not None:
        try:
            stmt = stmt.where(ProfilePost.created_at < datetime.fromisoformat(before))
        except ValueError:
            pass
    stmt = stmt.order_by(ProfilePost.created_at.desc()).limit(limit + 1)

    result = await db.execute(stmt)
    posts = list(result.scalars().all())

    has_more = len(posts) > limit
    if has_more:
        posts = posts[:limit]

    if not posts:
        return ConnectionsFeedPage(posts=[], has_more=False, next_cursor=None)

    author_ids = list({p.author_id for p in posts})
    users_result = await db.execute(select(User).where(User.id.in_(author_ids)))
    users = {u.id: u for u in users_result.scalars().all()}

    # Batch-fetch which posts the current user has hearted
    post_ids = [p.id for p in posts]
    hearted_result = await db.execute(
        select(ProfilePostHeart.post_id).where(
            ProfilePostHeart.user_id == current_user.id,
            ProfilePostHeart.post_id.in_(post_ids),
        )
    )
    hearted_ids = {r for r in hearted_result.scalars().all()}

    formatted = [
        ConnectionPostResponse(
            id=str(p.id),
            author=format_user(users[p.author_id]),
            content=p.content,
            media_urls=p.media_urls or [],
            is_edited=p.is_edited,
            heart_count=p.heart_count,
            has_hearted=p.id in hearted_ids,
            comment_count=p.comment_count,
            created_at=p.created_at.isoformat(),
        )
        for p in posts
        if p.author_id in users
    ]

    next_cursor = posts[-1].created_at.isoformat() if has_more and posts else None
    return ConnectionsFeedPage(posts=formatted, has_more=has_more, next_cursor=next_cursor)


@router.get("/connections/feed/posts/{post_id}", response_model=ConnectionPostResponse)
async def get_connection_post(
    post_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get a single connection feed post."""
    import uuid as _uuid
    try:
        pid = _uuid.UUID(post_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Post not found")

    result = await db.execute(select(ProfilePost).where(ProfilePost.id == pid))
    post = result.scalar_one_or_none()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    # Verify viewer is an accepted connection or the author
    connection_ids = await get_accepted_connection_ids(current_user.id, db)
    if post.author_id != current_user.id and post.author_id not in connection_ids:
        raise HTTPException(status_code=403, detail="Not authorised")

    author_result = await db.execute(select(User).where(User.id == post.author_id))
    author = author_result.scalar_one()

    hearted_result = await db.execute(
        select(ProfilePostHeart).where(
            ProfilePostHeart.user_id == current_user.id,
            ProfilePostHeart.post_id == pid,
        )
    )
    has_hearted = hearted_result.scalar_one_or_none() is not None

    return ConnectionPostResponse(
        id=str(post.id),
        author=format_user(author),
        content=post.content,
        media_urls=post.media_urls or [],
        is_edited=post.is_edited,
        heart_count=post.heart_count,
        has_hearted=has_hearted,
        comment_count=post.comment_count,
        created_at=post.created_at.isoformat(),
    )


@router.post("/connections/feed/posts/{post_id}/vote", status_code=200)
async def toggle_connection_post_heart(
    post_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Toggle heart on a connection feed post."""
    import uuid as _uuid
    try:
        pid = _uuid.UUID(post_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Post not found")

    import math as _math
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
    heart = existing.scalar_one_or_none()

    if heart:
        await db.delete(heart)
        post.heart_count = max(0, post.heart_count - 1)
        has_hearted = False
    else:
        db.add(ProfilePostHeart(user_id=current_user.id, post_id=pid))
        post.heart_count += 1
        has_hearted = True

        if post.author_id != current_user.id:
            await create_notification(
                db, post.author_id,
                type="post_heart",
                reference_id=post.id,
                reference_type="profile_post",
                actor_id=current_user.id,
            )

    post.hot_score = round(
        _math.log10(max(post.heart_count, 1)) + post.created_at.timestamp() / 45000, 7
    )
    await db.flush()
    return {"heart_count": post.heart_count, "has_hearted": has_hearted}


@router.delete("/connections/feed/posts/{post_id}", status_code=204)
async def delete_connection_post(
    post_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete a post from your connections feed."""
    import uuid as _uuid
    try:
        pid = _uuid.UUID(post_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Post not found")
    result = await db.execute(
        select(ProfilePost).where(ProfilePost.id == pid)
    )
    post = result.scalar_one_or_none()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    if post.author_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not your post")
    await db.delete(post)


# ── Connection post comments ───────────────────────────────────────────────

class CreateCommentRequest(BaseModel):
    content: str
    parent_id: str | None = None

    @field_validator("content")
    @classmethod
    def content_valid(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Comment cannot be empty")
        if len(v) > 2000:
            raise ValueError("Comment cannot exceed 2,000 characters")
        return v


async def _get_post_or_404(post_id: str, db: AsyncSession) -> ProfilePost:
    import uuid as _uuid
    try:
        pid = _uuid.UUID(post_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Post not found")
    result = await db.execute(select(ProfilePost).where(ProfilePost.id == pid))
    post = result.scalar_one_or_none()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    return post


@router.get("/connections/feed/posts/{post_id}/comments", response_model=list[ConnectionPostCommentResponse])
async def list_post_comments(
    post_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0),
):
    """List comments on a connection feed post."""
    post = await _get_post_or_404(post_id, db)

    result = await db.execute(
        select(ProfilePostComment)
        .where(ProfilePostComment.post_id == post.id)
        .order_by(ProfilePostComment.created_at.asc())
        .limit(limit)
        .offset(offset)
    )
    comments = result.scalars().all()

    if not comments:
        return []

    author_ids = list({c.author_id for c in comments})
    users_result = await db.execute(select(User).where(User.id.in_(author_ids)))
    users = {u.id: u for u in users_result.scalars().all()}

    comment_ids = [c.id for c in comments]
    hearted_result = await db.execute(
        select(ProfilePostCommentHeart.comment_id).where(
            ProfilePostCommentHeart.user_id == current_user.id,
            ProfilePostCommentHeart.comment_id.in_(comment_ids),
        )
    )
    hearted_ids = set(hearted_result.scalars().all())

    formatted = {
        str(c.id): ConnectionPostCommentResponse(
            id=str(c.id),
            author=format_user(users[c.author_id]),
            content=c.content,
            parent_id=str(c.parent_id) if c.parent_id else None,
            created_at=c.created_at.isoformat(),
            heart_count=c.heart_count,
            has_hearted=c.id in hearted_ids,
        )
        for c in comments
        if c.author_id in users
    }

    top_level: list[ConnectionPostCommentResponse] = []
    for c in comments:
        fc = formatted.get(str(c.id))
        if not fc:
            continue
        if c.parent_id is None:
            top_level.append(fc)
        else:
            parent = formatted.get(str(c.parent_id))
            if parent:
                parent.replies.append(fc)

    return top_level


@router.post("/connections/feed/posts/{post_id}/comments", response_model=ConnectionPostCommentResponse, status_code=201)
async def add_post_comment(
    post_id: str,
    data: CreateCommentRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Add a comment to a connection feed post."""
    import uuid as _uuid
    post = await _get_post_or_404(post_id, db)

    parent = None
    parent_id = None
    if data.parent_id:
        try:
            parent_id = _uuid.UUID(data.parent_id)
        except ValueError:
            raise HTTPException(status_code=404, detail="Parent comment not found")
        parent_result = await db.execute(
            select(ProfilePostComment).where(
                ProfilePostComment.id == parent_id,
                ProfilePostComment.post_id == post.id,
            )
        )
        parent = parent_result.scalar_one_or_none()
        if not parent:
            raise HTTPException(status_code=404, detail="Parent comment not found")
        if parent.parent_id is not None:
            raise HTTPException(status_code=400, detail="Replies can only be one level deep")

    comment = ProfilePostComment(
        post_id=post.id,
        parent_id=parent_id,
        author_id=current_user.id,
        content=data.content,
    )
    db.add(comment)
    post.comment_count += 1
    await db.flush()
    await db.refresh(comment)

    if post.author_id != current_user.id:
        await create_notification(
            db, post.author_id,
            type="post_comment",
            reference_id=post.id,
            reference_type="profile_post",
            actor_id=current_user.id,
        )

    if parent and parent.author_id != current_user.id and parent.author_id != post.author_id:
        await create_notification(
            db, parent.author_id,
            type="comment_reply",
            reference_id=post.id,
            reference_type="profile_post",
            actor_id=current_user.id,
        )

    return ConnectionPostCommentResponse(
        id=str(comment.id),
        author=format_user(current_user),
        content=comment.content,
        parent_id=str(comment.parent_id) if comment.parent_id else None,
        created_at=comment.created_at.isoformat(),
        heart_count=comment.heart_count,
        has_hearted=False,
    )


@router.delete("/connections/feed/posts/{post_id}/comments/{comment_id}", status_code=204)
async def delete_post_comment(
    post_id: str,
    comment_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete your own comment."""
    import uuid as _uuid
    try:
        cid = _uuid.UUID(comment_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Comment not found")

    post = await _get_post_or_404(post_id, db)

    result = await db.execute(
        select(ProfilePostComment).where(
            ProfilePostComment.id == cid,
            ProfilePostComment.post_id == post.id,
        )
    )
    comment = result.scalar_one_or_none()
    if not comment:
        raise HTTPException(status_code=404, detail="Comment not found")
    if comment.author_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not your comment")

    await db.delete(comment)
    post.comment_count = max(0, post.comment_count - 1)
    await db.flush()


@router.post("/connections/feed/posts/{post_id}/comments/{comment_id}/heart", status_code=200)
async def toggle_connection_comment_heart(
    post_id: str,
    comment_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Toggle heart on a connection post comment."""
    import uuid as _uuid
    try:
        cid = _uuid.UUID(comment_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Comment not found")

    post = await _get_post_or_404(post_id, db)

    result = await db.execute(
        select(ProfilePostComment).where(
            ProfilePostComment.id == cid,
            ProfilePostComment.post_id == post.id,
        )
    )
    comment = result.scalar_one_or_none()
    if not comment:
        raise HTTPException(status_code=404, detail="Comment not found")

    existing = await db.execute(
        select(ProfilePostCommentHeart).where(
            ProfilePostCommentHeart.user_id == current_user.id,
            ProfilePostCommentHeart.comment_id == cid,
        )
    )
    heart = existing.scalar_one_or_none()

    if heart:
        await db.delete(heart)
        comment.heart_count = max(0, comment.heart_count - 1)
        has_hearted = False
    else:
        db.add(ProfilePostCommentHeart(user_id=current_user.id, comment_id=cid))
        comment.heart_count += 1
        has_hearted = True

        if comment.author_id != current_user.id:
            await create_notification(
                db,
                comment.author_id,
                type="comment_heart",
                reference_id=comment.id,
                reference_type="profile_post_comment",
                actor_id=current_user.id,
            )

    await db.flush()
    return {"heart_count": comment.heart_count, "has_hearted": has_hearted}
