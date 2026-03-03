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
    """List public subs. Optionally filter by search term."""
    query = select(Sub).where(Sub.sub_type == "public")

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
        raise HTTPException(
            status_code=403, detail="This is a private sub. Request an invitation to join."
        )

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
