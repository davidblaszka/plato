import re
from fastapi import APIRouter, Depends, HTTPException, status, Query
from pydantic import BaseModel, field_validator
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_
from typing import Optional

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.models.sub import Sub, SubMembership
from app.services.notifications import create_notification

router = APIRouter(prefix="/subs", tags=["subs"])


# ── Schemas ────────────────────────────────────────────────────────────────

class CreateSubRequest(BaseModel):
    name: str
    description: str | None = None
    sub_type: str = "public"
    join_policy: str = "open"

    @field_validator("name")
    @classmethod
    def name_valid(cls, v: str) -> str:
        v = v.strip()
        if len(v) < 3 or len(v) > 100:
            raise ValueError("Name must be between 3 and 100 characters")
        return v

    @field_validator("sub_type")
    @classmethod
    def type_valid(cls, v: str) -> str:
        if v not in ("public", "private"):
            raise ValueError("sub_type must be 'public' or 'private'")
        return v

    @field_validator("join_policy")
    @classmethod
    def policy_valid(cls, v: str) -> str:
        if v not in ("open", "approval", "invite"):
            raise ValueError("join_policy must be 'open', 'approval', or 'invite'")
        return v


class SubResponse(BaseModel):
    id: str
    name: str
    slug: str
    description: str | None
    sub_type: str
    join_policy: str
    owner_id: str
    avatar_url: str | None
    member_count: int
    created_at: str
    # Viewer-specific fields (None if not authenticated)
    is_member: bool = False
    member_role: str | None = None


class MemberResponse(BaseModel):
    user_id: str
    username: str
    display_name: str | None
    avatar_url: str | None
    role: str
    joined_at: str


# ── Helpers ────────────────────────────────────────────────────────────────

def slugify(name: str) -> str:
    """Convert a sub name to a URL-safe slug. e.g. 'PNW Running Club' -> 'pnw-running-club'"""
    slug = name.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = re.sub(r"-+", "-", slug)
    return slug.strip("-")


async def get_sub_or_404(slug: str, db: AsyncSession) -> Sub:
    result = await db.execute(select(Sub).where(Sub.slug == slug))
    sub = result.scalar_one_or_none()
    if not sub:
        raise HTTPException(status_code=404, detail="Sub not found")
    return sub


async def get_membership(
    sub_id, user_id, db: AsyncSession
) -> SubMembership | None:
    result = await db.execute(
        select(SubMembership).where(
            and_(SubMembership.sub_id == sub_id, SubMembership.user_id == user_id)
        )
    )
    return result.scalar_one_or_none()


def format_sub(sub: Sub, membership: SubMembership | None = None) -> SubResponse:
    return SubResponse(
        id=str(sub.id),
        name=sub.name,
        slug=sub.slug,
        description=sub.description,
        sub_type=sub.sub_type,
        join_policy=sub.join_policy,
        owner_id=str(sub.owner_id),
        avatar_url=sub.avatar_url,
        member_count=sub.member_count,
        created_at=sub.created_at.isoformat(),
        is_member=membership is not None,
        member_role=membership.role if membership else None,
    )


# ── Routes ─────────────────────────────────────────────────────────────────

@router.post("", response_model=SubResponse, status_code=status.HTTP_201_CREATED)
async def create_sub(
    data: CreateSubRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Create a new sub. The creator automatically becomes the owner and first member."""
    base_slug = slugify(data.name)
    slug = base_slug

    # If slug is taken, append a number: pnw-running-2, pnw-running-3, etc.
    counter = 2
    while True:
        result = await db.execute(select(Sub).where(Sub.slug == slug))
        if not result.scalar_one_or_none():
            break
        slug = f"{base_slug}-{counter}"
        counter += 1

    sub = Sub(
        name=data.name,
        slug=slug,
        description=data.description,
        sub_type=data.sub_type,
        join_policy=data.join_policy,
        owner_id=current_user.id,
        member_count=1,
    )
    db.add(sub)
    await db.flush()  # get sub.id before creating membership

    # Creator is automatically the owner-member
    membership = SubMembership(
        sub_id=sub.id,
        user_id=current_user.id,
        role="owner",
    )
    db.add(membership)

    return format_sub(sub, membership)


@router.get("", response_model=list[SubResponse])
async def list_subs(
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user),
    search: str = Query(default="", description="Search by name or description"),
    limit: int = Query(default=20, le=100),
    offset: int = Query(default=0),
):
    """List subs. Private subs appear in search unless invite-only."""
    query = select(Sub).where(Sub.join_policy != "invite")

    if search:
        term = f"%{search.lower()}%"
        query = query.where(
            Sub.name.ilike(term) | Sub.description.ilike(term)
        )

    query = query.order_by(Sub.member_count.desc()).limit(limit).offset(offset)
    result = await db.execute(query)
    subs = result.scalars().all()

    # Fetch current user's memberships for these subs in one query
    sub_ids = [s.id for s in subs]
    memberships: dict = {}
    if current_user and sub_ids:
        mem_result = await db.execute(
            select(SubMembership).where(
                and_(
                    SubMembership.sub_id.in_(sub_ids),
                    SubMembership.user_id == current_user.id,
                )
            )
        )
        memberships = {m.sub_id: m for m in mem_result.scalars().all()}

    return [format_sub(s, memberships.get(s.id)) for s in subs]


@router.get("/mine", response_model=list[SubResponse])
async def list_my_subs(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Returns all subs the current user is a member of."""
    result = await db.execute(
        select(Sub, SubMembership)
        .join(SubMembership, Sub.id == SubMembership.sub_id)
        .where(SubMembership.user_id == current_user.id)
        .order_by(Sub.name)
    )
    rows = result.all()
    return [format_sub(sub, membership) for sub, membership in rows]


@router.get("/{slug}", response_model=SubResponse)
async def get_sub(
    slug: str,
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user),
):
    """Get a single sub by slug. Private subs require membership."""
    sub = await get_sub_or_404(slug, db)

    membership = None
    if current_user:
        membership = await get_membership(sub.id, current_user.id, db)

    if sub.sub_type == "private" and not membership:
        if sub.join_policy == "invite":
            raise HTTPException(
                status_code=403, detail="This sub requires an invitation to join."
            )
        # Private approval sub — allow viewing so user can request to join

    return format_sub(sub, membership)


@router.post("/{slug}/join", response_model=SubResponse)
async def join_sub(
    slug: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Join a public sub with an open join policy."""
    sub = await get_sub_or_404(slug, db)

    if sub.sub_type == "private":
        raise HTTPException(status_code=403, detail="Private subs require an invitation")

    if sub.join_policy == "invite":
        raise HTTPException(status_code=403, detail="This sub requires an invitation to join")

    if sub.join_policy == "approval":
        raise HTTPException(status_code=403, detail="This sub requires owner approval. Use request-join endpoint.")

    existing = await get_membership(sub.id, current_user.id, db)
    if existing:
        raise HTTPException(status_code=409, detail="You are already a member")

    membership = SubMembership(
        sub_id=sub.id,
        user_id=current_user.id,
        role="member",
    )
    db.add(membership)

    # Increment member count
    sub.member_count += 1

    # Notify sub owner (unless owner is joining their own sub)
    if sub.owner_id != current_user.id:
        await create_notification(
            db, sub.owner_id,
            type="sub_join",
            reference_id=sub.id,
            reference_type="sub",
            actor_id=current_user.id,
        )

    return format_sub(sub, membership)


@router.delete("/{slug}/leave", status_code=status.HTTP_204_NO_CONTENT)
async def leave_sub(
    slug: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Leave a sub. Owners must transfer ownership before leaving."""
    sub = await get_sub_or_404(slug, db)

    membership = await get_membership(sub.id, current_user.id, db)
    if not membership:
        raise HTTPException(status_code=404, detail="You are not a member of this sub")

    if membership.role == "owner":
        raise HTTPException(
            status_code=400,
            detail="Owners cannot leave their sub. Transfer ownership first or delete the sub.",
        )

    await db.delete(membership)
    sub.member_count = max(0, sub.member_count - 1)


@router.get("/{slug}/members", response_model=list[MemberResponse])
async def list_members(
    slug: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0),
):
    """List members of a sub. Requires membership for private subs."""
    sub = await get_sub_or_404(slug, db)

    if sub.sub_type == "private":
        membership = await get_membership(sub.id, current_user.id, db)
        if not membership:
            raise HTTPException(status_code=403, detail="Members only")

    result = await db.execute(
        select(SubMembership, User)
        .join(User, SubMembership.user_id == User.id)
        .where(SubMembership.sub_id == sub.id)
        .order_by(SubMembership.joined_at.asc())
        .limit(limit)
        .offset(offset)
    )
    rows = result.all()

    return [
        MemberResponse(
            user_id=str(user.id),
            username=user.username,
            display_name=user.display_name,
            avatar_url=user.avatar_url,
            role=membership.role,
            joined_at=membership.joined_at.isoformat(),
        )
        for membership, user in rows
    ]


@router.delete("/{slug}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_sub(
    slug: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete a sub. Only the owner can do this."""
    sub = await get_sub_or_404(slug, db)

    if sub.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only the owner can delete a sub")

    await db.delete(sub)


# ── Sub management endpoints ───────────────────────────────────────────────

class UpdateSubRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    sub_type: str | None = None
    join_policy: str | None = None

    @field_validator("sub_type")
    @classmethod
    def type_valid(cls, v):
        if v is not None and v not in ("public", "private"):
            raise ValueError("sub_type must be 'public' or 'private'")
        return v

    @field_validator("join_policy")
    @classmethod
    def policy_valid(cls, v):
        if v is not None and v not in ("open", "approval", "invite"):
            raise ValueError("join_policy must be 'open', 'approval', or 'invite'")
        return v


class JoinRequestResponse(BaseModel):
    id: str
    user_id: str
    username: str
    display_name: str | None
    avatar_url: str | None
    status: str
    created_at: str


@router.patch("/{slug}", response_model=SubResponse)
async def update_sub(
    slug: str,
    data: UpdateSubRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Update sub settings. Owner only."""
    sub = await get_sub_or_404(slug, db)
    if sub.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only the owner can edit this sub")

    if data.name is not None:
        sub.name = data.name.strip()
    if data.description is not None:
        sub.description = data.description.strip() or None
    if data.sub_type is not None:
        sub.sub_type = data.sub_type
    if data.join_policy is not None:
        sub.join_policy = data.join_policy

    await db.flush()
    membership = await get_membership(sub.id, current_user.id, db)
    return format_sub(sub, membership)


@router.post("/{slug}/request-join", status_code=201)
async def request_join(
    slug: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Request to join a sub with approval join policy or private sub."""
    from app.models.sub import SubJoinRequest

    sub = await get_sub_or_404(slug, db)

    existing_membership = await get_membership(sub.id, current_user.id, db)
    if existing_membership:
        raise HTTPException(status_code=409, detail="Already a member")

    existing_request = await db.execute(
        select(SubJoinRequest).where(
            SubJoinRequest.sub_id == sub.id,
            SubJoinRequest.user_id == current_user.id,
            SubJoinRequest.status == "pending",
        )
    )
    if existing_request.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Request already pending")

    req = SubJoinRequest(sub_id=sub.id, user_id=current_user.id, status="pending")
    db.add(req)
    await db.flush()
    return {"status": "pending", "request_id": str(req.id)}


@router.get("/{slug}/join-requests", response_model=list[JoinRequestResponse])
async def list_join_requests(
    slug: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List pending join requests. Owner only."""
    from app.models.sub import SubJoinRequest

    sub = await get_sub_or_404(slug, db)
    if sub.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Owner only")

    result = await db.execute(
        select(SubJoinRequest, User)
        .join(User, SubJoinRequest.user_id == User.id)
        .where(SubJoinRequest.sub_id == sub.id, SubJoinRequest.status == "pending")
        .order_by(SubJoinRequest.created_at.asc())
    )
    rows = result.all()

    return [
        JoinRequestResponse(
            id=str(req.id),
            user_id=str(user.id),
            username=user.username,
            display_name=user.display_name,
            avatar_url=user.avatar_url,
            status=req.status,
            created_at=req.created_at.isoformat(),
        )
        for req, user in rows
    ]


@router.post("/{slug}/join-requests/{request_id}/approve", status_code=200)
async def approve_join_request(
    slug: str,
    request_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Approve a join request. Owner only."""
    from app.models.sub import SubJoinRequest
    import uuid as _uuid

    sub = await get_sub_or_404(slug, db)
    if sub.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Owner only")

    result = await db.execute(
        select(SubJoinRequest).where(SubJoinRequest.id == _uuid.UUID(request_id))
    )
    req = result.scalar_one_or_none()
    if not req:
        raise HTTPException(status_code=404, detail="Request not found")

    req.status = "approved"
    membership = SubMembership(sub_id=sub.id, user_id=req.user_id, role="member")
    db.add(membership)
    sub.member_count += 1
    await db.flush()
    return {"status": "approved"}


@router.post("/{slug}/join-requests/{request_id}/reject", status_code=200)
async def reject_join_request(
    slug: str,
    request_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Reject a join request. Owner only."""
    from app.models.sub import SubJoinRequest
    import uuid as _uuid

    sub = await get_sub_or_404(slug, db)
    if sub.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Owner only")

    result = await db.execute(
        select(SubJoinRequest).where(SubJoinRequest.id == _uuid.UUID(request_id))
    )
    req = result.scalar_one_or_none()
    if not req:
        raise HTTPException(status_code=404, detail="Request not found")

    req.status = "rejected"
    await db.flush()
    return {"status": "rejected"}


@router.post("/{slug}/invite", status_code=200)
async def invite_to_sub(
    slug: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Directly add a user to the sub (owner only). Used for invite-only subs."""
    username = data.get("username", "").strip().lower()
    if not username:
        raise HTTPException(status_code=400, detail="username is required")

    sub = await get_sub_or_404(slug, db)
    if sub.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Owner only")

    result = await db.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    existing = await get_membership(sub.id, user.id, db)
    if existing:
        raise HTTPException(status_code=409, detail="User is already a member")

    membership = SubMembership(sub_id=sub.id, user_id=user.id, role="member")
    db.add(membership)
    sub.member_count += 1
    await db.flush()
    return {"status": "added", "username": username}


@router.get("/{slug}/my-join-request")
async def get_my_join_request(
    slug: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Check if current user has a pending join request for this sub."""
    from app.models.sub import SubJoinRequest

    sub = await get_sub_or_404(slug, db)
    result = await db.execute(
        select(SubJoinRequest).where(
            SubJoinRequest.sub_id == sub.id,
            SubJoinRequest.user_id == current_user.id,
        )
    )
    req = result.scalar_one_or_none()
    return {"status": req.status if req else None}
