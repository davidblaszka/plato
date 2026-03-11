from datetime import datetime
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.models.sub import Sub, SubMembership
from app.models.post import Post
from app.models.social import PostHeart
from app.routers.posts import PostResponse, format_post

router = APIRouter(tags=["feed"])


class FeedPage(BaseModel):
    posts: list[PostResponse]
    has_more: bool
    next_cursor: str | None


@router.get("/feed", response_model=FeedPage)
async def get_home_feed(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    limit: int = Query(default=20, le=100),
    before: str | None = Query(default=None),
    sort: str = Query(default="hot", pattern="^(hot|new)$"),
):
    """
    Home feed — posts from all subs the current user is a member of.
    Sort by 'hot' (default) or 'new'. Cursor-paginated via `before`.
    """
    memberships_result = await db.execute(
        select(SubMembership.sub_id).where(SubMembership.user_id == current_user.id)
    )
    sub_ids = [row[0] for row in memberships_result.all()]

    if not sub_ids:
        return FeedPage(posts=[], has_more=False, next_cursor=None)

    stmt = select(Post).where(Post.sub_id.in_(sub_ids))
    if sort == "hot":
        if before is not None:
            try:
                stmt = stmt.where(Post.hot_score < float(before))
            except ValueError:
                pass
        stmt = stmt.order_by(Post.hot_score.desc(), Post.created_at.desc())
    else:
        if before is not None:
            try:
                stmt = stmt.where(Post.created_at < datetime.fromisoformat(before))
            except ValueError:
                pass
        stmt = stmt.order_by(Post.created_at.desc())

    posts_result = await db.execute(stmt.limit(limit + 1))
    posts = list(posts_result.scalars().all())

    has_more = len(posts) > limit
    if has_more:
        posts = posts[:limit]

    if not posts:
        return FeedPage(posts=[], has_more=False, next_cursor=None)

    # Fetch authors and subs in bulk
    author_ids = list({p.author_id for p in posts})
    post_sub_ids = list({p.sub_id for p in posts})

    authors_result = await db.execute(select(User).where(User.id.in_(author_ids)))
    authors = {u.id: u for u in authors_result.scalars().all()}

    subs_result = await db.execute(select(Sub).where(Sub.id.in_(post_sub_ids)))
    subs = {s.id: s for s in subs_result.scalars().all()}

    # Batch-fetch which posts the current user has hearted
    post_ids = [p.id for p in posts]
    hearted_result = await db.execute(
        select(PostHeart.post_id).where(
            PostHeart.user_id == current_user.id,
            PostHeart.post_id.in_(post_ids),
        )
    )
    hearted_ids = {r for r in hearted_result.scalars().all()}

    formatted = [
        format_post(p, authors[p.author_id], subs[p.sub_id].slug, has_hearted=p.id in hearted_ids,
                    sub_name=subs[p.sub_id].name, sub_avatar_url=subs[p.sub_id].avatar_url)
        for p in posts
        if p.author_id in authors and p.sub_id in subs
    ]

    last = posts[-1] if posts else None
    next_cursor = None
    if has_more and last:
        next_cursor = str(last.hot_score) if sort == "hot" else last.created_at.isoformat()

    return FeedPage(posts=formatted, has_more=has_more, next_cursor=next_cursor)
