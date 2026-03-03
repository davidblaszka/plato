from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_, func
from pydantic import BaseModel

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.models.sub import Sub
from app.models.post import Post

router = APIRouter(prefix="/search", tags=["search"])


class UserResult(BaseModel):
    id: str
    username: str
    display_name: str | None
    bio: str | None
    avatar_url: str | None
    account_type: str
    is_verified: bool


class SubResult(BaseModel):
    id: str
    name: str
    slug: str
    description: str | None
    avatar_url: str | None
    sub_type: str
    member_count: int


class PostResult(BaseModel):
    id: str
    content: str | None
    sub_slug: str
    author_username: str
    author_display_name: str | None
    author_avatar_url: str | None
    heart_count: int
    comment_count: int
    created_at: str


class SearchResults(BaseModel):
    users: list[UserResult]
    subs: list[SubResult]
    posts: list[PostResult]
    query: str


@router.get("", response_model=SearchResults)
async def search(
    q: str = Query(..., min_length=1, max_length=100),
    type: str = Query("all", pattern="^(all|users|subs|posts)$"),
    limit: int = Query(10, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    q = q.strip()
    pattern = f"%{q}%"

    users: list[UserResult] = []
    subs: list[SubResult] = []
    posts: list[PostResult] = []

    if type in ("all", "users"):
        result = await db.execute(
            select(User)
            .where(
                User.is_active == True,
                or_(
                    User.username.ilike(pattern),
                    User.display_name.ilike(pattern),
                )
            )
            .order_by(
                # Public accounts first, then by username
                User.account_type.desc(),
                User.username
            )
            .limit(limit)
        )
        users = [
            UserResult(
                id=str(u.id),
                username=u.username,
                display_name=u.display_name,
                bio=u.bio,
                avatar_url=u.avatar_url,
                account_type=u.account_type,
                is_verified=u.is_verified,
            )
            for u in result.scalars().all()
        ]

    if type in ("all", "subs"):
        result = await db.execute(
            select(Sub)
            .where(
                or_(
                    Sub.name.ilike(pattern),
                    Sub.description.ilike(pattern),
                    Sub.slug.ilike(pattern),
                )
            )
            .order_by(Sub.member_count.desc())
            .limit(limit)
        )
        subs = [
            SubResult(
                id=str(s.id),
                name=s.name,
                slug=s.slug,
                description=s.description,
                avatar_url=s.avatar_url,
                sub_type=s.sub_type,
                member_count=s.member_count,
            )
            for s in result.scalars().all()
        ]

    if type in ("all", "posts"):
        # Join posts with users and subs for context
        from app.models.sub import Sub as SubModel
        stmt = (
            select(Post, User, SubModel)
            .join(User, Post.author_id == User.id)
            .join(SubModel, Post.sub_id == SubModel.id)
            .where(
                Post.is_removed == False,
                Post.content.ilike(pattern),
            )
            .order_by(Post.heart_count.desc(), Post.created_at.desc())
            .limit(limit)
        )
        result = await db.execute(stmt)
        posts = [
            PostResult(
                id=str(p.id),
                content=p.content,
                sub_slug=s.slug,
                author_username=u.username,
                author_display_name=u.display_name,
                author_avatar_url=u.avatar_url,
                heart_count=p.heart_count,
                comment_count=p.comment_count,
                created_at=p.created_at.isoformat(),
            )
            for p, u, s in result.all()
        ]

    return SearchResults(users=users, subs=subs, posts=posts, query=q)
