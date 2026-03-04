"""
Notification service — creates notifications and pushes real-time
count updates to connected WebSocket clients.
"""
from typing import Dict
from fastapi import WebSocket
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from app.models.notification import Notification
import uuid


# ── WebSocket connection manager ───────────────────────────────────────────

class ConnectionManager:
    """Holds active WebSocket connections keyed by user_id."""

    def __init__(self):
        self.active: Dict[str, WebSocket] = {}

    async def connect(self, user_id: str, websocket: WebSocket):
        await websocket.accept()
        self.active[user_id] = websocket

    def disconnect(self, user_id: str):
        self.active.pop(user_id, None)

    async def push_count(self, user_id: str, count: int):
        """Push unread count to a user if they're connected."""
        ws = self.active.get(user_id)
        if ws:
            try:
                await ws.send_json({"unread_count": count})
            except Exception:
                self.disconnect(user_id)


manager = ConnectionManager()


# ── Notification creation ──────────────────────────────────────────────────

async def create_notification(
    db: AsyncSession,
    user_id,           # recipient
    type: str,
    reference_id=None,
    reference_type: str | None = None,
    actor_id=None,     # who triggered the notification
):
    """Create a notification and push updated count to the recipient."""
    notif = Notification(
        user_id=user_id,
        type=type,
        reference_id=reference_id,
        reference_type=reference_type,
        actor_id=actor_id,
    )
    db.add(notif)
    await db.flush()

    # Count unread and push via WebSocket
    result = await db.execute(
        select(func.count()).where(
            Notification.user_id == user_id,
            Notification.is_read == False,
        )
    )
    unread = result.scalar() or 0
    await manager.push_count(str(user_id), unread)
