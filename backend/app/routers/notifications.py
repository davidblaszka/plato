from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, update
from jose import JWTError, jwt
from typing import Optional

from app.core.database import get_db
from app.core.security import get_current_user
from app.core.config import settings
from app.models.user import User
from app.models.sub import Sub
from app.models.notification import Notification
from app.services.notifications import manager

router = APIRouter(tags=["notifications"])


# ── Schemas ────────────────────────────────────────────────────────────────

NOTIFICATION_LABELS = {
    "connection_request": "sent you a connection request",
    "connection_accepted": "accepted your connection request",
    "post_comment": "commented on your post",
    "comment_reply": "replied to your comment",
    "sub_join": "joined your sub",
    "sub_join_request": "requested to join your sub",
    "sub_invite": "invited you to join a circle",
    "post_upvote": "upvoted your post",
    "post_heart": "liked your post",
    "comment_heart": "liked your comment",
    "new_follower": "followed you",
    "mention": "mentioned you in a comment",
}

NOTIFICATION_ICONS = {
    "connection_request": "person_add",
    "connection_accepted": "people",
    "post_comment": "chat_bubble",
    "comment_reply": "reply",
    "sub_join": "group_add",
    "sub_join_request": "person_search",
    "sub_invite": "mail",
    "post_upvote": "arrow_upward",
    "post_heart": "favorite",
    "comment_heart": "favorite",
    "new_follower": "person",
}


class NotificationResponse(BaseModel):
    id: str
    type: str
    label: str
    icon: str
    actor_id: str | None
    actor_username: str | None
    actor_display_name: str | None
    actor_avatar_url: str | None
    reference_id: str | None
    reference_type: str | None
    reference_slug: str | None = None   # slug for sub references
    reference_name: str | None = None   # display name for sub references
    is_read: bool
    actioned: bool
    created_at: str


class UnreadCountResponse(BaseModel):
    unread_count: int


# ── REST endpoints ─────────────────────────────────────────────────────────

@router.get("/notifications", response_model=list[NotificationResponse])
async def list_notifications(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    limit: int = Query(default=50, le=100),
    unread_only: bool = Query(default=False),
):
    query = select(Notification).where(Notification.user_id == current_user.id)
    if unread_only:
        query = query.where(Notification.is_read == False)
    query = query.order_by(Notification.created_at.desc()).limit(limit)

    result = await db.execute(query)
    notifs = result.scalars().all()

    # Batch-fetch actors
    actor_ids = list({n.actor_id for n in notifs if n.actor_id})
    actors: dict = {}
    if actor_ids:
        actors_result = await db.execute(select(User).where(User.id.in_(actor_ids)))
        actors = {u.id: u for u in actors_result.scalars().all()}

    # Batch-fetch sub slugs for sub-type notifications
    sub_ref_ids = list({n.reference_id for n in notifs if n.reference_type == "sub" and n.reference_id})
    sub_slugs: dict = {}
    sub_names: dict = {}
    if sub_ref_ids:
        subs_result = await db.execute(select(Sub).where(Sub.id.in_(sub_ref_ids)))
        subs = subs_result.scalars().all()
        sub_slugs = {s.id: s.slug for s in subs}
        sub_names = {s.id: s.name for s in subs}

    def _label(n, name_or_slug: str | None) -> str:
        if name_or_slug:
            if n.type == 'sub_invite':
                return f"invited you to join {name_or_slug}"
            if n.type == 'sub_join_request':
                return f"requested to join {name_or_slug}"
            if n.type == 'sub_join':
                return f"joined {name_or_slug}"
        return NOTIFICATION_LABELS.get(n.type, n.type)

    return [
        NotificationResponse(
            id=str(n.id),
            type=n.type,
            label=_label(
                n,
                (sub_names.get(n.reference_id) or sub_slugs.get(n.reference_id))
                if n.reference_type == "sub" and n.reference_id else None,
            ),
            icon=NOTIFICATION_ICONS.get(n.type, "notifications"),
            actor_id=str(n.actor_id) if n.actor_id else None,
            actor_username=actors[n.actor_id].username if n.actor_id and n.actor_id in actors else None,
            actor_display_name=actors[n.actor_id].display_name if n.actor_id and n.actor_id in actors else None,
            actor_avatar_url=actors[n.actor_id].avatar_url if n.actor_id and n.actor_id in actors else None,
            reference_id=str(n.reference_id) if n.reference_id else None,
            reference_type=n.reference_type,
            reference_slug=sub_slugs.get(n.reference_id) if n.reference_type == "sub" and n.reference_id else None,
            reference_name=sub_names.get(n.reference_id) if n.reference_type == "sub" and n.reference_id else None,
            is_read=n.is_read,
            actioned=n.actioned,
            created_at=n.created_at.isoformat(),
        )
        for n in notifs
    ]


@router.get("/notifications/unread-count", response_model=UnreadCountResponse)
async def unread_count(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(func.count()).where(
            Notification.user_id == current_user.id,
            Notification.is_read == False,
        )
    )
    return UnreadCountResponse(unread_count=result.scalar() or 0)


@router.post("/notifications/read-all", response_model=UnreadCountResponse)
async def mark_all_read(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await db.execute(
        update(Notification)
        .where(
            Notification.user_id == current_user.id,
            Notification.is_read == False,
        )
        .values(is_read=True)
    )
    return UnreadCountResponse(unread_count=0)


@router.post("/notifications/{notification_id}/read")
async def mark_read(
    notification_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(Notification).where(
            Notification.id == notification_id,
            Notification.user_id == current_user.id,
        )
    )
    notif = result.scalar_one_or_none()
    if notif:
        notif.is_read = True
    return {"ok": True}


# ── WebSocket ──────────────────────────────────────────────────────────────

@router.websocket("/ws/notifications")
async def notifications_websocket(
    websocket: WebSocket,
    token: Optional[str] = Query(default=None),
):
    if not token:
        await websocket.close(code=4001)
        return

    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
        user_id: str = payload.get("sub")
        if not user_id:
            await websocket.close(code=4001)
            return
    except JWTError:
        await websocket.close(code=4001)
        return

    await manager.connect(user_id, websocket)

    try:
        from app.core.database import AsyncSessionLocal
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(func.count()).where(
                    Notification.user_id == user_id,
                    Notification.is_read == False,
                )
            )
            count = result.scalar() or 0
            await websocket.send_json({"unread_count": count})

        while True:
            await websocket.receive_text()

    except WebSocketDisconnect:
        manager.disconnect(user_id)
