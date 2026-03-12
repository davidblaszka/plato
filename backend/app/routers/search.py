from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_, and_

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.models.sub import Sub
from app.models.post import Post

router = APIRouter(prefix="/search", tags=["search"])


def _score_user(user: User, term: str) -> float:
    score = 0.0
    username = user.username.lower()
    display = (user.display_name or "").lower()
    if username == term or display == term:
        score += 100
    if username.startswith(term) or display.startswith(term):
        score += 50
    if term in username or term in display:
        score += 20
    if user.account_type == "public":
        score += 5  # slight boost; no follower_count field
    return score


def _score_sub(sub: Sub, term: str) -> float:
    score = 0.0
    name = sub.name.lower()
    if name == term:
        score += 100
    if name.startswith(term):
        score += 50
    if term in name:
        score += 20
    score += min(sub.member_count or 0, 1000) * 0.005
    return score


def _score_post(post: Post, term: str) -> float:
    score = 0.0
    content = (post.content or "").lower()
    if content.startswith(term):
        score += 30
    score += (post.hot_score or 0) * 0.1
    score += min(post.heart_count or 0, 100) * 0.1
    return score


@router.get("")
async def search(
    q: str = Query(..., min_length=1, max_length=100),
    limit: int = Query(20, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    term = q.strip().lower()
    if len(term) < 2:
        return {"results": []}

    pattern = f"%{term}%"
    results = []

    # ── Users ────────────────────────────────────────────────────────────────
    user_rows = await db.execute(
        select(User)
        .where(
            User.is_active == True,
            or_(
                User.username.ilike(pattern),
                User.display_name.ilike(pattern),
            ),
        )
        .limit(5)
    )
    for user in user_rows.scalars().all():
        results.append({
            "type": "user",
            "id": str(user.id),
            "title": user.display_name or user.username,
            "subtitle": f"@{user.username}",
            "avatar_url": user.avatar_url,
            "is_verified": user.is_verified,
            "score": _score_user(user, term),
            "route": f"/profile/{user.id}",
        })

    # ── Circles (public only) ─────────────────────────────────────────────────
    sub_rows = await db.execute(
        select(Sub)
        .where(
            Sub.sub_type == "public",
            or_(
                Sub.name.ilike(pattern),
                Sub.slug.ilike(pattern),
                Sub.description.ilike(pattern),
            ),
        )
        .limit(5)
    )
    for sub in sub_rows.scalars().all():
        results.append({
            "type": "circle",
            "id": str(sub.id),
            "title": sub.name,
            "subtitle": f"{sub.member_count} members",
            "avatar_url": sub.avatar_url,
            "score": _score_sub(sub, term),
            "route": f"/subs/{sub.slug}",
        })

    # ── Posts (public circles + public-account profile posts) ────────────────
    post_rows = await db.execute(
        select(Post, User, Sub)
        .join(User, Post.author_id == User.id)
        .outerjoin(Sub, Post.sub_id == Sub.id)
        .where(
            Post.is_removed == False,
            Post.content.ilike(pattern),
            or_(
                and_(Post.sub_id.is_not(None), Sub.sub_type == "public"),
                and_(Post.sub_id.is_(None), User.account_type == "public"),
            ),
        )
        .order_by(Post.hot_score.desc())
        .limit(10)
    )
    for post, user, sub in post_rows.all():
        context = f" in {sub.name}" if sub else ""
        results.append({
            "type": "post",
            "id": str(post.id),
            "title": (post.content or "")[:120],
            "subtitle": f"@{user.username}{context}",
            "avatar_url": user.avatar_url,
            "score": _score_post(post, term),
            "route": f"/posts/{post.id}",
        })

    results.sort(key=lambda r: r["score"], reverse=True)
    return {"results": results[:limit]}
