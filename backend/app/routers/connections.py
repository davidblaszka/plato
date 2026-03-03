from fastapi import APIRouter, Depends, HTTPException, status, Query
from pydantic import BaseModel, field_validator
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_, and_
from typing import Optional

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.models.connection import Connection, ConnectionPost
from app.services.notifications import create_notification

router = APIRouter(tags=["connections"])


# ── Schemas ────────────────────────────────────────────────────────────────

class UserSummary(BaseModel):
    id: str
    username: str
    display_name: str | None
    avatar_url: str | None


class ConnectionResponse(BaseModel):
    id: str
    user: UserSummary       # the other person (not the current user)
    status: str
    direction: str          # 'sent' or 'received'
    created_at: str


class ConnectionPostResponse(BaseModel):
    id: str
    author: UserSummary
    content: str
    media_urls: list[str] = []
    is_edited: bool
    created_at: str


class CreateConnectionPostRequest(BaseModel):
    content: str
    media_urls: list[str] = []

    @field_validator("content")
    @classmethod
    def content_valid(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Content cannot be empty")
        if len(v) > 10000:
            raise ValueError("Content cannot exceed 10,000 characters")
        return v


# ── Helpers ────────────────────────────────────────────────────────────────

def format_user(user: User) -> UserSummary:
    return UserSummary(
        id=str(user.id),
        username=user.username,
        display_name=user.display_name,
        avatar_url=user.avatar_url,
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
    """Post to your connections feed. Only your mutual connections will see this."""
    post = ConnectionPost(
        author_id=current_user.id,
        content=data.content,
        media_urls=data.media_urls,
    )
    db.add(post)
    await db.flush()
    await db.refresh(post)

    return ConnectionPostResponse(
        id=str(post.id),
        author=format_user(current_user),
        content=post.content,
        media_urls=post.media_urls or [],
        is_edited=post.is_edited,
        created_at=post.created_at.isoformat(),
    )


@router.get("/connections/feed", response_model=list[ConnectionPostResponse])
async def get_connections_feed(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    limit: int = Query(default=20, le=100),
    offset: int = Query(default=0),
):
    """
    The private connections feed — posts from you and your mutual connections.
    Chronological. No algorithm.
    """
    connection_ids = await get_accepted_connection_ids(current_user.id, db)

    # Include your own posts in your feed
    visible_author_ids = connection_ids + [current_user.id]

    result = await db.execute(
        select(ConnectionPost)
        .where(ConnectionPost.author_id.in_(visible_author_ids))
        .order_by(ConnectionPost.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    posts = result.scalars().all()

    if not posts:
        return []

    author_ids = list({p.author_id for p in posts})
    users_result = await db.execute(select(User).where(User.id.in_(author_ids)))
    users = {u.id: u for u in users_result.scalars().all()}

    return [
        ConnectionPostResponse(
            id=str(p.id),
            author=format_user(users[p.author_id]),
            content=p.content,
            media_urls=p.media_urls or [],
            is_edited=p.is_edited,
            created_at=p.created_at.isoformat(),
        )
        for p in posts
        if p.author_id in users
    ]


@router.delete("/connections/feed/posts/{post_id}", status_code=204)
async def delete_connection_post(
    post_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete a post from your connections feed."""
    result = await db.execute(
        select(ConnectionPost).where(ConnectionPost.id == post_id)
    )
    post = result.scalar_one_or_none()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    if post.author_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not your post")
    await db.delete(post)
