from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect, Query
from pydantic import BaseModel, field_validator
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, func, or_
from jose import JWTError, jwt
from typing import Optional

from app.core.database import get_db, AsyncSessionLocal
from app.core.security import get_current_user
from app.core.config import settings
from app.models.user import User
from app.models.message import Conversation, ConversationParticipant, Message
from app.services.encryption import encrypt_message, decrypt_message
from app.services.messaging import message_manager

router = APIRouter(prefix="/messages", tags=["messages"])


# ── Schemas ────────────────────────────────────────────────────────────────

class ParticipantInfo(BaseModel):
    user_id: str
    username: str
    display_name: str | None
    avatar_url: str | None


class ConversationResponse(BaseModel):
    id: str
    type: str
    status: str  # active | request
    name: str | None
    participants: list[ParticipantInfo]
    last_message: str | None
    last_message_at: str | None
    unread_count: int
    created_by: str | None = None  # user_id of who initiated the conversation
    is_request: bool = False        # True when conversation is a pending request
    initiated_by: str | None = None  # user_id of initiator when is_request=True


class MessageResponse(BaseModel):
    id: str
    conversation_id: str
    sender: ParticipantInfo
    content: str          # ciphertext for E2EE messages; plaintext for legacy
    is_encrypted: bool    # True = client must decrypt; False = ready to display
    is_edited: bool
    client_id: str | None = None
    created_at: str


class CreateDirectRequest(BaseModel):
    username: str         # who to DM
    message: str
    is_encrypted: bool = False  # True when client sends E2EE ciphertext

    @field_validator("message")
    @classmethod
    def message_valid(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Message cannot be empty")
        if len(v) > 10000:
            raise ValueError("Message too long")
        return v


class CreateGroupRequest(BaseModel):
    name: str
    usernames: list[str]  # participants (excluding yourself)
    message: str
    is_encrypted: bool = False

    @field_validator("name")
    @classmethod
    def name_valid(cls, v: str) -> str:
        v = v.strip()
        if len(v) < 1 or len(v) > 100:
            raise ValueError("Group name required")
        return v

    @field_validator("usernames")
    @classmethod
    def users_valid(cls, v: list) -> list:
        if len(v) < 1:
            raise ValueError("Add at least one participant")
        if len(v) > 49:
            raise ValueError("Groups support up to 50 members")
        return v


class SendMessageRequest(BaseModel):
    content: str
    is_encrypted: bool = False  # True = content is E2EE ciphertext; relay as-is

    @field_validator("content")
    @classmethod
    def content_valid(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Message cannot be empty")
        if len(v) > 20000:
            raise ValueError("Message too long")
        return v


class SendEncryptedMessageRequest(BaseModel):
    """Per-recipient E2EE payload — used for group message fan-out."""
    recipient_id: str
    client_id: str | None = None  # UUID that links all per-recipient copies
    content: str   # base64(nonce + ciphertext) encrypted for this recipient only

    @field_validator("content")
    @classmethod
    def content_valid(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Content cannot be empty")
        return v


class EditEncryptedMessageRequest(BaseModel):
    """Per-recipient E2EE edit payload (group fan-out)."""
    recipient_id: str
    client_id: str
    content: str

    @field_validator("content")
    @classmethod
    def content_valid(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Content cannot be empty")
        return v


class DeleteEncryptedMessageRequest(BaseModel):
    """Per-recipient E2EE delete payload (group fan-out)."""
    recipient_id: str
    client_id: str


class UpdateConversationRequest(BaseModel):
    title: str | None = None


class EditMessageRequest(BaseModel):
    content: str
    is_encrypted: bool = False  # True = content is E2EE ciphertext; relay as-is

    @field_validator("content")
    @classmethod
    def content_valid(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Message cannot be empty")
        if len(v) > 20000:
            raise ValueError("Message too long")
        return v


class MessageSyncItem(BaseModel):
    """Lightweight sync item — id + timestamp only, no content."""
    id: str
    sent_at: str


# ── Helpers ────────────────────────────────────────────────────────────────

def format_participant(user: User) -> ParticipantInfo:
    return ParticipantInfo(
        user_id=str(user.id),
        username=user.username,
        display_name=user.display_name,
        avatar_url=user.avatar_url,
    )


def format_message(msg: Message, sender: User) -> MessageResponse:
    """Return message content.
    - is_encrypted=True:  relay ciphertext as-is; client decrypts.
    - is_encrypted=False: Fernet-decrypt for backward compat with legacy messages.
    """
    if msg.is_encrypted:
        content = msg.content_encrypted  # pass-through — server never decrypts
    else:
        content = decrypt_message(msg.content_encrypted)  # legacy Fernet decrypt

    return MessageResponse(
        id=str(msg.id),
        conversation_id=str(msg.conversation_id),
        sender=format_participant(sender),
        content=content,
        is_encrypted=msg.is_encrypted,
        is_edited=msg.is_edited,
        client_id=str(msg.client_id) if msg.client_id else None,
        created_at=msg.created_at.isoformat(),
    )


async def get_participants(conversation_id, db: AsyncSession) -> list[User]:
    result = await db.execute(
        select(User)
        .join(ConversationParticipant, User.id == ConversationParticipant.user_id)
        .where(ConversationParticipant.conversation_id == conversation_id)
    )
    return result.scalars().all()


async def require_participant(conversation_id, user_id, db: AsyncSession):
    result = await db.execute(
        select(ConversationParticipant).where(
            and_(
                ConversationParticipant.conversation_id == conversation_id,
                ConversationParticipant.user_id == user_id,
                ConversationParticipant.deleted_at.is_(None),
            )
        )
    )
    p = result.scalar_one_or_none()
    if not p:
        raise HTTPException(status_code=403, detail="Not a participant")
    return p


async def get_last_message(conversation_id, db: AsyncSession):
    result = await db.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def get_unread_count(conversation_id, participant: ConversationParticipant, db: AsyncSession) -> int:
    query = select(func.count()).where(
        Message.conversation_id == conversation_id
    )
    if participant.last_read_at:
        query = query.where(Message.created_at > participant.last_read_at)
    result = await db.execute(query)
    return result.scalar() or 0


async def format_conversation(
    conv: Conversation,
    current_user_id,
    db: AsyncSession,
) -> ConversationResponse:
    participants = await get_participants(conv.id, db)
    last_msg = await get_last_message(conv.id, db)

    participant_record = await db.execute(
        select(ConversationParticipant).where(
            and_(
                ConversationParticipant.conversation_id == conv.id,
                ConversationParticipant.user_id == current_user_id,
            )
        )
    )
    p = participant_record.scalar_one_or_none()
    unread = await get_unread_count(conv.id, p, db) if p else 0

    # For direct chats, use the other person's name
    name = conv.name
    if conv.type == "direct" and not name:
        other = next((u for u in participants if u.id != current_user_id), None)
        name = other.display_name or other.username if other else "Unknown"

    # Last message preview — decrypt only for legacy messages
    last_message_text = None
    if last_msg:
        if last_msg.is_encrypted:
            last_message_text = "🔒 Encrypted message"
        else:
            last_message_text = decrypt_message(last_msg.content_encrypted)

    is_request = conv.status == 'request'
    return ConversationResponse(
        id=str(conv.id),
        type=conv.type,
        status=conv.status if hasattr(conv, 'status') else 'active',
        name=name,
        participants=[format_participant(u) for u in participants],
        last_message=last_message_text,
        last_message_at=last_msg.created_at.isoformat() if last_msg else None,
        unread_count=unread,
        created_by=str(conv.created_by) if conv.created_by else None,
        is_request=is_request,
        initiated_by=str(conv.created_by) if is_request and conv.created_by else None,
    )


# ── Routes ─────────────────────────────────────────────────────────────────

@router.post("/direct", response_model=ConversationResponse, status_code=201)
async def create_direct(
    data: CreateDirectRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Start a DM with any user. Reuses existing conversation if one exists."""
    result = await db.execute(
        select(User).where(User.username == data.username.lower())
    )
    other = result.scalar_one_or_none()
    if not other:
        raise HTTPException(status_code=404, detail="User not found")
    if other.id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot DM yourself")

    # Check if a direct conversation already exists between these two
    existing = await db.execute(
        select(Conversation)
        .join(ConversationParticipant, Conversation.id == ConversationParticipant.conversation_id)
        .where(
            and_(
                Conversation.type == "direct",
                ConversationParticipant.user_id == current_user.id,
            )
        )
    )
    for conv in existing.scalars().all():
        parts = await get_participants(conv.id, db)
        part_ids = {p.id for p in parts}
        if other.id in part_ids and len(parts) == 2:
            # Conversation exists — revive soft-deleted participants if needed.
            part_rows = await db.execute(
                select(ConversationParticipant).where(
                    and_(
                        ConversationParticipant.conversation_id == conv.id,
                        ConversationParticipant.user_id.in_([current_user.id, other.id]),
                    )
                )
            )
            for p in part_rows.scalars().all():
                if p.deleted_at is not None:
                    p.deleted_at = None

            # Refresh conversation status based on connection state.
            from app.models.connection import Connection
            connection_result = await db.execute(
                select(Connection).where(
                    or_(
                        and_(Connection.requester_id == current_user.id, Connection.addressee_id == other.id),
                        and_(Connection.requester_id == other.id, Connection.addressee_id == current_user.id),
                    ),
                    Connection.status == "accepted",
                )
            )
            is_connection = connection_result.scalar_one_or_none() is not None
            conv.status = "active" if is_connection else "request"
            if conv.status == "request":
                conv.created_by = current_user.id

            await db.flush()

            # Conversation exists — just send the message
            if conv.status == "request":
                stored = encrypt_message(data.message)
                is_enc = False
            else:
                stored = data.message if data.is_encrypted else encrypt_message(data.message)
                is_enc = data.is_encrypted
            msg = Message(
                conversation_id=conv.id,
                sender_id=current_user.id,
                content_encrypted=stored,
                is_encrypted=is_enc,
            )
            db.add(msg)
            await db.flush()
            await _push_message(conv.id, msg, current_user, db)
            return await format_conversation(conv, current_user.id, db)

    # Create new conversation — check if they're accepted connections
    from app.models.connection import Connection
    connection_result = await db.execute(
        select(Connection).where(
            or_(
                and_(Connection.requester_id == current_user.id, Connection.addressee_id == other.id),
                and_(Connection.requester_id == other.id, Connection.addressee_id == current_user.id),
            ),
            Connection.status == "accepted",
        )
    )
    is_connection = connection_result.scalar_one_or_none() is not None
    conv_status = "active" if is_connection else "request"

    conv = Conversation(type="direct", status=conv_status, created_by=current_user.id)
    db.add(conv)
    await db.flush()

    for uid in [current_user.id, other.id]:
        db.add(ConversationParticipant(conversation_id=conv.id, user_id=uid))

    # Request messages are always Fernet-encrypted (not E2EE) so the recipient
    # can read the preview before accepting — key exchange hasn't happened yet.
    if conv_status == "request":
        stored = encrypt_message(data.message)
        is_enc = False
    else:
        stored = data.message if data.is_encrypted else encrypt_message(data.message)
        is_enc = data.is_encrypted
    msg = Message(
        conversation_id=conv.id,
        sender_id=current_user.id,
        content_encrypted=stored,
        is_encrypted=is_enc,
    )
    db.add(msg)
    await db.flush()
    await db.refresh(conv)
    await _push_message(conv.id, msg, current_user, db)

    return await format_conversation(conv, current_user.id, db)


@router.post("/group", response_model=ConversationResponse, status_code=201)
async def create_group(
    data: CreateGroupRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Create a group conversation."""
    users_result = await db.execute(
        select(User).where(User.username.in_([u.lower() for u in data.usernames]))
    )
    other_users = users_result.scalars().all()

    if len(other_users) != len(data.usernames):
        raise HTTPException(status_code=404, detail="One or more users not found")

    conv = Conversation(
        type="group",
        name=data.name,
        created_by=current_user.id,
    )
    db.add(conv)
    await db.flush()

    all_users = [current_user] + list(other_users)
    for user in all_users:
        db.add(ConversationParticipant(conversation_id=conv.id, user_id=user.id))

    stored = data.message if data.is_encrypted else encrypt_message(data.message)
    msg = Message(
        conversation_id=conv.id,
        sender_id=current_user.id,
        content_encrypted=stored,
        is_encrypted=data.is_encrypted,
    )
    db.add(msg)
    await db.flush()
    await db.refresh(conv)
    await _push_message(conv.id, msg, current_user, db)

    return await format_conversation(conv, current_user.id, db)


@router.get("", response_model=list[ConversationResponse])
async def list_conversations(
    status: str = Query(default="active", pattern="^(active|request|all)$"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List conversations. status=active (default), request, or all.

    active  — returns active conversations + outgoing pending requests
    request — returns only incoming pending requests (not initiated by current user)
    all     — returns everything
    """
    stmt = (
        select(Conversation)
        .join(ConversationParticipant, Conversation.id == ConversationParticipant.conversation_id)
        .where(
            ConversationParticipant.user_id == current_user.id,
            ConversationParticipant.deleted_at.is_(None),
        )
    )
    if status == "active":
        stmt = stmt.where(
            or_(
                Conversation.status == "active",
                and_(
                    Conversation.status == "request",
                    Conversation.created_by == current_user.id,
                ),
            )
        )
    elif status == "request":
        stmt = stmt.where(
            and_(
                Conversation.status == "request",
                Conversation.created_by != current_user.id,
            )
        )
    # status == "all": no additional filter
    stmt = stmt.order_by(Conversation.updated_at.desc())

    result = await db.execute(stmt)
    convs = result.scalars().all()
    formatted = []
    for conv in convs:
        formatted.append(await format_conversation(conv, current_user.id, db))
    return formatted


@router.post("/{conversation_id}/accept", response_model=ConversationResponse)
async def accept_message_request(
    conversation_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Accept a message request — moves it to the active inbox."""
    import uuid as _uuid
    try:
        cid = _uuid.UUID(conversation_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Conversation not found")

    result = await db.execute(select(Conversation).where(Conversation.id == cid))
    conv = result.scalar_one_or_none()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    if conv.created_by == current_user.id:
        raise HTTPException(status_code=403, detail="Cannot accept your own request")

    conv.status = "active"
    await db.flush()
    return await format_conversation(conv, current_user.id, db)


@router.delete("/{conversation_id}/decline", status_code=204)
async def decline_message_request(
    conversation_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Decline (delete) a message request."""
    import uuid as _uuid
    try:
        cid = _uuid.UUID(conversation_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Conversation not found")

    result = await db.execute(select(Conversation).where(Conversation.id == cid))
    conv = result.scalar_one_or_none()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    if conv.created_by == current_user.id:
        raise HTTPException(status_code=403, detail="Cannot decline your own message")

    await db.delete(conv)


@router.delete("/{conversation_id}/cancel", status_code=204)
async def cancel_message_request(
    conversation_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Cancel a pending message request the current user initiated."""
    import uuid as _uuid
    try:
        cid = _uuid.UUID(conversation_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Conversation not found")

    result = await db.execute(select(Conversation).where(Conversation.id == cid))
    conv = result.scalar_one_or_none()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    if conv.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Cannot cancel another user's request")

    if conv.status != "request":
        raise HTTPException(status_code=400, detail="Conversation is already active")

    await db.delete(conv)


@router.patch("/{conversation_id}", response_model=ConversationResponse)
async def update_conversation(
    conversation_id: str,
    data: UpdateConversationRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Rename a group conversation. Any participant can update the title."""
    import uuid as _uuid
    try:
        cid = _uuid.UUID(conversation_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Conversation not found")

    result = await db.execute(select(Conversation).where(Conversation.id == cid))
    conv = result.scalar_one_or_none()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    await require_participant(conversation_id, current_user.id, db)

    if data.title is not None:
        conv.name = data.title.strip() or conv.name
    await db.flush()
    return await format_conversation(conv, current_user.id, db)


@router.delete("/{conversation_id}", status_code=204)
async def delete_or_leave_conversation(
    conversation_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete (1:1) or leave (group) a conversation.

    1:1  — soft-deletes the current user's participant row so the other
           person's conversation is unaffected.
    Group — removes the current user from participants entirely.
           If nobody remains, the conversation is hard-deleted.
    """
    import uuid as _uuid
    from datetime import datetime, timezone
    try:
        cid = _uuid.UUID(conversation_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Conversation not found")

    result = await db.execute(select(Conversation).where(Conversation.id == cid))
    conv = result.scalar_one_or_none()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    # Find participant record — include soft-deleted so we can return a clean error
    p_result = await db.execute(
        select(ConversationParticipant).where(
            and_(
                ConversationParticipant.conversation_id == cid,
                ConversationParticipant.user_id == current_user.id,
            )
        )
    )
    participant = p_result.scalar_one_or_none()
    if not participant:
        raise HTTPException(status_code=403, detail="Not a participant")

    if conv.type == "direct":
        # Soft-delete: hide from inbox, leave the other person's view intact
        participant.deleted_at = datetime.now(timezone.utc)
        await db.flush()
    else:
        # Group: hard-remove from participants
        await db.delete(participant)
        await db.flush()

        # Hard-delete the conversation if nobody is left
        remaining = await db.execute(
            select(func.count()).where(
                and_(
                    ConversationParticipant.conversation_id == cid,
                    ConversationParticipant.deleted_at.is_(None),
                )
            )
        )
        if (remaining.scalar() or 0) == 0:
            await db.delete(conv)
            await db.flush()
        else:
            # Notify remaining participants
            payload = {
                "type": "participant_left",
                "conversation_id": str(conversation_id),
                "user_id": str(current_user.id),
                "username": current_user.username,
            }
            parts_result = await db.execute(
                select(ConversationParticipant).where(
                    and_(
                        ConversationParticipant.conversation_id == cid,
                        ConversationParticipant.deleted_at.is_(None),
                    )
                )
            )
            for p in parts_result.scalars().all():
                await message_manager.send_to_user(str(p.user_id), payload)


@router.get("/{conversation_id}/messages", response_model=list[MessageResponse])
async def list_messages(
    conversation_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    limit: int = Query(default=50, le=200),
    before: Optional[str] = Query(default=None),
):
    """Get messages in a conversation. Also marks them as read."""
    participant = await require_participant(conversation_id, current_user.id, db)

    query = select(Message).where(
        and_(
            Message.conversation_id == conversation_id,
            # Include messages where recipient_id matches current user OR is NULL
            # (NULL = broadcast / direct / legacy message for all participants)
            or_(
                Message.recipient_id == current_user.id,
                Message.recipient_id.is_(None),
            )
        )
    )
    if before:
        query = query.where(Message.created_at < before)
    query = query.order_by(Message.created_at.desc()).limit(limit)

    result = await db.execute(query)
    messages = result.scalars().all()
    messages = list(reversed(messages))  # oldest first for display

    # Fetch senders
    sender_ids = list({m.sender_id for m in messages})
    senders_result = await db.execute(select(User).where(User.id.in_(sender_ids)))
    senders = {u.id: u for u in senders_result.scalars().all()}

    # Mark as read
    from sqlalchemy import update
    from datetime import datetime, timezone
    await db.execute(
        update(ConversationParticipant)
        .where(
            and_(
                ConversationParticipant.conversation_id == conversation_id,
                ConversationParticipant.user_id == current_user.id,
            )
        )
        .values(last_read_at=datetime.now(timezone.utc))
    )

    return [
        format_message(m, senders[m.sender_id])
        for m in messages
        if m.sender_id in senders
    ]


@router.post("/{conversation_id}/messages", response_model=MessageResponse, status_code=201)
async def send_message(
    conversation_id: str,
    data: SendMessageRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Send a message to a conversation.

    is_encrypted=False (default): server Fernet-encrypts content before storage.
    is_encrypted=True: content is already E2EE ciphertext; stored and relayed as-is.
    """
    await require_participant(conversation_id, current_user.id, db)

    # Store raw ciphertext for E2EE; Fernet-encrypt for legacy clients
    stored = data.content if data.is_encrypted else encrypt_message(data.content)

    msg = Message(
        conversation_id=conversation_id,
        sender_id=current_user.id,
        content_encrypted=stored,
        is_encrypted=data.is_encrypted,
    )
    db.add(msg)
    await db.flush()
    await db.refresh(msg)

    # Update conversation updated_at for sort order
    conv_result = await db.execute(
        select(Conversation).where(Conversation.id == conversation_id)
    )
    conv = conv_result.scalar_one()
    conv.updated_at = msg.created_at

    # Mark sender as read
    from sqlalchemy import update
    from datetime import datetime, timezone
    await db.execute(
        update(ConversationParticipant)
        .where(
            and_(
                ConversationParticipant.conversation_id == conversation_id,
                ConversationParticipant.user_id == current_user.id,
            )
        )
        .values(last_read_at=datetime.now(timezone.utc))
    )

    await _push_message(conversation_id, msg, current_user, db)

    return MessageResponse(
        id=str(msg.id),
        conversation_id=str(msg.conversation_id),
        sender=format_participant(current_user),
        content=data.content,  # return the original (plaintext or ciphertext)
        is_encrypted=data.is_encrypted,
        is_edited=False,
        client_id=str(msg.client_id) if msg.client_id else None,
        created_at=msg.created_at.isoformat(),
    )


@router.patch("/{message_id}/edit", response_model=MessageResponse)
async def edit_message(
    message_id: str,
    data: EditMessageRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Edit a message you sent."""
    import uuid as _uuid
    try:
        mid = _uuid.UUID(message_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Message not found")

    result = await db.execute(select(Message).where(Message.id == mid))
    msg = result.scalar_one_or_none()
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")
    if msg.sender_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not your message")

    if msg.is_encrypted != data.is_encrypted:
        raise HTTPException(
            status_code=400,
            detail="is_encrypted must match existing message",
        )

    stored = data.content if data.is_encrypted else encrypt_message(data.content)
    msg.content_encrypted = stored
    msg.is_edited = True
    await db.flush()

    await _broadcast_message_edited(msg.conversation_id, msg, current_user, db)

    return MessageResponse(
        id=str(msg.id),
        conversation_id=str(msg.conversation_id),
        sender=format_participant(current_user),
        content=data.content,
        is_encrypted=data.is_encrypted,
        is_edited=True,
        client_id=str(msg.client_id) if msg.client_id else None,
        created_at=msg.created_at.isoformat(),
    )


@router.delete("/{message_id}", status_code=204)
async def delete_message(
    message_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete a message you sent."""
    import uuid as _uuid
    try:
        mid = _uuid.UUID(message_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Message not found")

    result = await db.execute(select(Message).where(Message.id == mid))
    msg = result.scalar_one_or_none()
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")
    if msg.sender_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not your message")

    conv_id = msg.conversation_id
    client_id = msg.client_id
    await db.delete(msg)
    await db.flush()

    await _broadcast_message_deleted(conv_id, mid, current_user.id, db, client_id=client_id)


@router.post("/{conversation_id}/encrypted", response_model=MessageResponse, status_code=201)
async def send_encrypted_message(
    conversation_id: str,
    data: SendEncryptedMessageRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Send a per-recipient E2EE message copy (used for group message fan-out).

    The client encrypts the message separately for each group participant and
    POSTs one request per recipient. Each Message row stores the ciphertext
    intended only for that recipient (recipient_id is set).
    """
    import uuid as _uuid
    await require_participant(conversation_id, current_user.id, db)

    try:
        recipient_uuid = _uuid.UUID(data.recipient_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid recipient_id")

    # Verify recipient is a participant
    await require_participant(conversation_id, recipient_uuid, db)

    client_uuid = None
    if data.client_id:
        try:
            client_uuid = _uuid.UUID(data.client_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid client_id")

    msg = Message(
        conversation_id=conversation_id,
        sender_id=current_user.id,
        content_encrypted=data.content,
        is_encrypted=True,
        recipient_id=recipient_uuid,
        client_id=client_uuid,
    )
    db.add(msg)
    await db.flush()
    await db.refresh(msg)

    # Update conversation updated_at
    conv_result = await db.execute(
        select(Conversation).where(Conversation.id == conversation_id)
    )
    conv = conv_result.scalar_one()
    conv.updated_at = msg.created_at

    # Push only to the intended recipient
    payload = _build_ws_payload(conversation_id, msg, current_user, data.content, is_encrypted=True)
    await message_manager.send_to_user(data.recipient_id, payload)

    return MessageResponse(
        id=str(msg.id),
        conversation_id=str(msg.conversation_id),
        sender=format_participant(current_user),
        content=data.content,
        is_encrypted=True,
        is_edited=False,
        client_id=str(msg.client_id) if msg.client_id else None,
        created_at=msg.created_at.isoformat(),
    )


@router.patch("/{conversation_id}/encrypted/edit", response_model=MessageResponse)
async def edit_encrypted_message(
    conversation_id: str,
    data: EditEncryptedMessageRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Edit a per-recipient E2EE message copy (group fan-out)."""
    import uuid as _uuid
    await require_participant(conversation_id, current_user.id, db)

    try:
        recipient_uuid = _uuid.UUID(data.recipient_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid recipient_id")

    try:
        client_uuid = _uuid.UUID(data.client_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid client_id")

    # Verify recipient is a participant
    await require_participant(conversation_id, recipient_uuid, db)

    result = await db.execute(
        select(Message).where(
            Message.conversation_id == conversation_id,
            Message.sender_id == current_user.id,
            Message.recipient_id == recipient_uuid,
            Message.client_id == client_uuid,
        )
    )
    msg = result.scalar_one_or_none()
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")

    msg.content_encrypted = data.content
    msg.is_edited = True
    await db.flush()

    # Push only to the intended recipient
    payload = _build_ws_payload(conversation_id, msg, current_user, data.content, is_encrypted=True, is_edited=True)
    payload["type"] = "message_edited"
    await message_manager.send_to_user(data.recipient_id, payload)

    return MessageResponse(
        id=str(msg.id),
        conversation_id=str(msg.conversation_id),
        sender=format_participant(current_user),
        content=data.content,
        is_encrypted=True,
        is_edited=True,
        client_id=str(msg.client_id) if msg.client_id else None,
        created_at=msg.created_at.isoformat(),
    )


@router.delete("/{conversation_id}/encrypted/delete", status_code=204)
async def delete_encrypted_message(
    conversation_id: str,
    data: DeleteEncryptedMessageRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete a per-recipient E2EE message copy (group fan-out)."""
    import uuid as _uuid
    await require_participant(conversation_id, current_user.id, db)

    try:
        recipient_uuid = _uuid.UUID(data.recipient_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid recipient_id")

    try:
        client_uuid = _uuid.UUID(data.client_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid client_id")

    # Verify recipient is a participant
    await require_participant(conversation_id, recipient_uuid, db)

    result = await db.execute(
        select(Message).where(
            Message.conversation_id == conversation_id,
            Message.sender_id == current_user.id,
            Message.recipient_id == recipient_uuid,
            Message.client_id == client_uuid,
        )
    )
    msg = result.scalar_one_or_none()
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")

    msg_id = msg.id
    await db.delete(msg)
    await db.flush()

    payload = {
        "type": "message_deleted",
        "conversation_id": str(conversation_id),
        "message_id": str(msg_id),
    }
    await message_manager.send_to_user(data.recipient_id, payload)


@router.delete("/{message_id}", status_code=204)
async def delete_message(
    message_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete a message. Only the sender can delete their own messages."""
    import uuid as _uuid
    try:
        mid = _uuid.UUID(message_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Message not found")

    result = await db.execute(select(Message).where(Message.id == mid))
    msg = result.scalar_one_or_none()
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")
    if msg.sender_id != current_user.id:
        raise HTTPException(status_code=403, detail="Cannot delete another user's message")

    conversation_id = str(msg.conversation_id)
    await db.delete(msg)
    await db.flush()

    # Broadcast deletion event to all other participants
    parts_result = await db.execute(
        select(ConversationParticipant).where(
            ConversationParticipant.conversation_id == msg.conversation_id
        )
    )
    participants = parts_result.scalars().all()
    payload = {
        "type": "message_deleted",
        "conversation_id": conversation_id,
        "message_id": message_id,
    }
    sender_id = str(current_user.id)
    for p in participants:
        if str(p.user_id) != sender_id:
            await message_manager.send_to_user(str(p.user_id), payload)


@router.get("/{conversation_id}/sync", response_model=list[MessageSyncItem])
async def sync_messages(
    conversation_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    since: Optional[str] = Query(default=None),  # ISO timestamp — only return newer
):
    """Return message IDs + timestamps only — used by client to detect missed messages.

    Client compares this list against local SQLite to find gaps, then fetches
    full message content only for missing IDs.
    """
    await require_participant(conversation_id, current_user.id, db)

    query = select(Message.id, Message.created_at).where(
        and_(
            Message.conversation_id == conversation_id,
            or_(
                Message.recipient_id == current_user.id,
                Message.recipient_id.is_(None),
            )
        )
    )
    if since:
        query = query.where(Message.created_at > since)
    query = query.order_by(Message.created_at.asc()).limit(500)

    result = await db.execute(query)
    rows = result.all()
    return [MessageSyncItem(id=str(r.id), sent_at=r.created_at.isoformat()) for r in rows]


# ── WebSocket ──────────────────────────────────────────────────────────────

@router.websocket("/ws")
async def messages_websocket(
    websocket: WebSocket,
    token: Optional[str] = Query(default=None),
):
    """
    WebSocket for real-time message delivery.
    Client connects once and receives all incoming messages across all conversations.
    Payload: {"type": "message", "conversation_id": "...", "message": {...}}
    """
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

    await message_manager.connect(user_id, websocket)
    try:
        while True:
            await websocket.receive_text()  # keep alive
    except WebSocketDisconnect:
        message_manager.disconnect(user_id)


# ── Internal helpers ────────────────────────────────────────────────────────

def _build_ws_payload(
    conversation_id,
    msg: Message,
    sender: User,
    content: str,
    is_encrypted: bool,
    is_edited: bool = False,
) -> dict:
    return {
        "type": "message",
        "conversation_id": str(conversation_id),
        "message": {
            "id": str(msg.id),
            "conversation_id": str(conversation_id),
            "sender": {
                "user_id": str(sender.id),
                "username": sender.username,
                "display_name": sender.display_name,
                "avatar_url": sender.avatar_url,
            },
            "content": content,          # ciphertext for E2EE; plaintext for legacy
            "is_encrypted": is_encrypted,
            "is_edited": is_edited,
            "client_id": str(msg.client_id) if msg.client_id else None,
            "created_at": msg.created_at.isoformat(),
        }
    }


async def _push_message(conversation_id, msg: Message, sender: User, db: AsyncSession):
    """Push a new message to all participants who are online."""
    result = await db.execute(
        select(ConversationParticipant).where(
            ConversationParticipant.conversation_id == conversation_id
        )
    )
    participants = result.scalars().all()

    # For E2EE: relay ciphertext as-is.
    # For legacy: decrypt so non-updated clients can still read in real-time.
    if msg.is_encrypted:
        content = msg.content_encrypted
    else:
        content = decrypt_message(msg.content_encrypted)

    payload = _build_ws_payload(conversation_id, msg, sender, content, msg.is_encrypted, msg.is_edited)

    sender_id = str(sender.id)
    for p in participants:
        # Skip sender — they already have the message from the HTTP response
        if str(p.user_id) != sender_id:
            await message_manager.send_to_user(str(p.user_id), payload)


async def _broadcast_message_deleted(conversation_id, message_id, sender_id, db: AsyncSession, client_id=None):
    result = await db.execute(
        select(ConversationParticipant).where(
            ConversationParticipant.conversation_id == conversation_id
        )
    )
    participants = result.scalars().all()
    payload = {
        "type": "message_deleted",
        "conversation_id": str(conversation_id),
        "message_id": str(message_id),
    }
    if client_id is not None:
        payload["client_id"] = str(client_id)
    for p in participants:
        if str(p.user_id) == str(sender_id):
            continue
        await message_manager.send_to_user(str(p.user_id), payload)


async def _broadcast_message_edited(
    conversation_id,
    msg: Message,
    sender: User,
    db: AsyncSession,
):
    # For E2EE: relay ciphertext as-is.
    # For legacy: decrypt so non-updated clients can still read in real-time.
    if msg.is_encrypted:
        content = msg.content_encrypted
    else:
        content = decrypt_message(msg.content_encrypted)

    payload = _build_ws_payload(
        conversation_id,
        msg,
        sender,
        content,
        msg.is_encrypted,
        True,
    )
    payload["type"] = "message_edited"

    result = await db.execute(
        select(ConversationParticipant).where(
            ConversationParticipant.conversation_id == conversation_id
        )
    )
    participants = result.scalars().all()
    for p in participants:
        if str(p.user_id) == str(sender.id):
            continue
        await message_manager.send_to_user(str(p.user_id), payload)
