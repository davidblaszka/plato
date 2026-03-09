from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.models.sub import Sub, SubMembership
from app.models.post import Post
from app.routers.posts import PostResponse, format_post

router = APIRouter(tags=["feed"])


@router.get("/feed", response_model=list[PostResponse])
async def get_home_feed(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    limit: int = Query(default=20, le=100),
    offset: int = Query(default=0),
    sort: str = Query(default="hot", pattern="^(hot|new)$"),
):
    """
    Home feed — posts from all subs the current user is a member of.
    Sort by 'hot' (default) or 'new'.
    """
    # Get all sub IDs the user belongs to
    memberships_result = await db.execute(
        select(SubMembership.sub_id).where(SubMembership.user_id == current_user.id)
    )
    sub_ids = [row[0] for row in memberships_result.all()]

    if not sub_ids:
        return []

    stmt = select(Post).where(Post.sub_id.in_(sub_ids))
    if sort == "hot":
        stmt = stmt.order_by(Post.heart_count.desc(), Post.created_at.desc())
    else:
        stmt = stmt.order_by(Post.created_at.desc())
    posts_result = await db.execute(stmt.limit(limit).offset(offset))
    posts = posts_result.scalars().all()

    if not posts:
        return []

    # Fetch authors and subs in bulk
    author_ids = list({p.author_id for p in posts})
    post_sub_ids = list({p.sub_id for p in posts})

    authors_result = await db.execute(select(User).where(User.id.in_(author_ids)))
    authors = {u.id: u for u in authors_result.scalars().all()}

    subs_result = await db.execute(select(Sub).where(Sub.id.in_(post_sub_ids)))
    subs = {s.id: s for s in subs_result.scalars().all()}

    return [
        format_post(p, authors[p.author_id], subs[p.sub_id].slug)
        for p in posts
        if p.author_id in authors and p.sub_id in subs
    ]
