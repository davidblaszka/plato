import math
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, status, Query
from pydantic import BaseModel, field_validator
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, update
from typing import Optional

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.models.sub import Sub, SubMembership
from app.models.post import Post, Comment
from app.models.social import PostHeart, CommentHeart
from app.services.notifications import create_notification

router = APIRouter(tags=["posts"])


def calc_hot_score(heart_count: int, created_at: datetime) -> float:
    score = max(heart_count, 1)
    epoch = created_at.timestamp()
    return round(math.log10(score) + epoch / 45000, 7)


# ── Schemas ────────────────────────────────────────────────────────────────

class CreatePostRequest(BaseModel):
    content: str | None = None
    media_urls: list[str] = []

    @field_validator("content")
    @classmethod
    def content_not_empty(cls, v: str | None) -> str | None:
        if v is not None:
            v = v.strip()
            if len(v) == 0:
                raise ValueError("Content cannot be empty")
            if len(v) > 10000:
                raise ValueError("Content cannot exceed 10,000 characters")
        return v


class UpdatePostRequest(BaseModel):
    content: str

    @field_validator("content")
    @classmethod
    def content_valid(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Content cannot be empty")
        return v


class CreateCommentRequest(BaseModel):
    content: str
    parent_id: str | None = None  # None = top-level comment, set = reply

    @field_validator("content")
    @classmethod
    def content_valid(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Content cannot be empty")
        if len(v) > 5000:
            raise ValueError("Comment cannot exceed 5,000 characters")
        return v


class AuthorInfo(BaseModel):
    user_id: str
    username: str
    display_name: str | None
    avatar_url: str | None


class CommentResponse(BaseModel):
    id: str
    post_id: str
    parent_id: str | None
    author: AuthorInfo
    content: str
    is_edited: bool
    heart_count: int = 0
    has_hearted: bool = False
    created_at: str
    replies: list["CommentResponse"] = []


class PostResponse(BaseModel):
    id: str
    sub_id: str
    sub_slug: str
    author: AuthorInfo
    content: str | None
    media_urls: list[str]
    is_edited: bool
    comment_count: int
    heart_count: int = 0
    is_pinned: bool = False
    is_removed: bool = False
    has_hearted: bool = False
    created_at: str
    updated_at: str


# ── Helpers ────────────────────────────────────────────────────────────────

async def require_membership(
    sub: Sub, user: User, db: AsyncSession
) -> SubMembership:
    """Raises 403 if the user is not a member of the sub."""
    result = await db.execute(
        select(SubMembership).where(
            and_(
                SubMembership.sub_id == sub.id,
                SubMembership.user_id == user.id,
            )
        )
    )
    membership = result.scalar_one_or_none()
    if not membership:
        raise HTTPException(
            status_code=403,
            detail="You must be a member of this sub to post",
        )
    return membership


async def get_sub_by_slug(slug: str, db: AsyncSession) -> Sub:
    result = await db.execute(select(Sub).where(Sub.slug == slug))
    sub = result.scalar_one_or_none()
    if not sub:
        raise HTTPException(status_code=404, detail="Sub not found")
    return sub


async def get_post_or_404(post_id: str, db: AsyncSession) -> Post:
    result = await db.execute(select(Post).where(Post.id == post_id))
    post = result.scalar_one_or_none()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    return post


async def get_user_by_id(user_id, db: AsyncSession) -> User:
    result = await db.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()


def format_post(post: Post, author: User, sub_slug: str, has_hearted: bool = False) -> PostResponse:
    return PostResponse(
        id=str(post.id),
        sub_id=str(post.sub_id),
        sub_slug=sub_slug,
        author=AuthorInfo(
            user_id=str(author.id),
            username=author.username,
            display_name=author.display_name,
            avatar_url=author.avatar_url,
        ),
        content=post.content,
        media_urls=post.media_urls or [],
        is_edited=post.is_edited,
        comment_count=post.comment_count,
        heart_count=post.heart_count,
        is_pinned=post.is_pinned,
        is_removed=post.is_removed,
        has_hearted=has_hearted,
        created_at=post.created_at.isoformat(),
        updated_at=post.updated_at.isoformat(),
    )


def format_comment(comment: Comment, author: User, has_hearted: bool = False) -> CommentResponse:
    return CommentResponse(
        id=str(comment.id),
        post_id=str(comment.post_id),
        parent_id=str(comment.parent_id) if comment.parent_id else None,
        author=AuthorInfo(
            user_id=str(author.id),
            username=author.username,
            display_name=author.display_name,
            avatar_url=author.avatar_url,
        ),
        content=comment.content,
        is_edited=comment.is_edited,
        heart_count=getattr(comment, 'heart_count', 0),
        has_hearted=has_hearted,
        created_at=comment.created_at.isoformat(),
    )


# ── Post Routes ────────────────────────────────────────────────────────────

@router.post("/subs/{slug}/posts", response_model=PostResponse, status_code=201)
async def create_post(
    slug: str,
    data: CreatePostRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Create a post in a sub. Must be a member."""
    if not data.content and not data.media_urls:
        raise HTTPException(status_code=400, detail="Post must have content or media")

    sub = await get_sub_by_slug(slug, db)
    await require_membership(sub, current_user, db)

    post = Post(
        sub_id=sub.id,
        author_id=current_user.id,
        content=data.content,
        media_urls=data.media_urls,
    )
    db.add(post)
    await db.flush()
    await db.refresh(post)
    post.hot_score = calc_hot_score(0, post.created_at)
    await db.flush()
    await db.refresh(post)
    return format_post(post, current_user, sub.slug)


@router.get("/subs/{slug}/posts", response_model=list[PostResponse])
async def list_sub_posts(
    slug: str,
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user),
    limit: int = Query(default=20, le=100),
    offset: int = Query(default=0),
    sort: str = Query(default="hot", pattern="^(hot|new)$"),
):
    """Get posts in a sub. Sort by 'hot' (default) or 'new'."""
    sub = await get_sub_by_slug(slug, db)

    # Private subs require membership
    if sub.sub_type == "private":
        if not current_user:
            raise HTTPException(status_code=403, detail="Login required")
        await require_membership(sub, current_user, db)

    stmt = select(Post).where(Post.sub_id == sub.id, Post.is_removed == False)
    if sort == "hot":
        stmt = stmt.order_by(Post.is_pinned.desc(), Post.heart_count.desc(), Post.created_at.desc())
    else:
        stmt = stmt.order_by(Post.is_pinned.desc(), Post.created_at.desc())
    stmt = stmt.limit(limit).offset(offset)

    result = await db.execute(stmt)
    posts = result.scalars().all()

    author_ids = list({p.author_id for p in posts})
    authors_result = await db.execute(select(User).where(User.id.in_(author_ids)))
    authors = {u.id: u for u in authors_result.scalars().all()}

    return [format_post(p, authors[p.author_id], sub.slug) for p in posts if p.author_id in authors]


@router.post("/posts/{post_id}/vote", status_code=200)
async def toggle_heart(
    post_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Toggle upvote on a post. Returns new vote count and whether user has voted."""
    import uuid as _uuid
    try:
        pid = _uuid.UUID(post_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Post not found")

    result = await db.execute(select(Post).where(Post.id == pid))
    post = result.scalar_one_or_none()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    existing = await db.execute(
        select(PostHeart).where(PostHeart.user_id == current_user.id, PostHeart.post_id == pid)
    )
    vote = existing.scalar_one_or_none()

    if vote:
        await db.delete(vote)
        post.heart_count = max(0, post.heart_count - 1)
        has_hearted = False
    else:
        db.add(PostHeart(user_id=current_user.id, post_id=pid))
        post.heart_count = post.heart_count + 1
        has_hearted = True

    post.hot_score = calc_hot_score(post.heart_count, post.created_at)
    await db.flush()
    return {"heart_count": post.heart_count, "has_hearted": has_hearted}


@router.post("/posts/{post_id}/pin", status_code=200)
async def pin_post(
    post_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Pin or unpin a post. Only sub owner can pin."""
    import uuid as _uuid
    from app.models.sub import Sub as SubModel
    try:
        pid = _uuid.UUID(post_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Post not found")

    result = await db.execute(select(Post).where(Post.id == pid))
    post = result.scalar_one_or_none()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    # Check ownership
    sub_result = await db.execute(select(SubModel).where(SubModel.id == post.sub_id))
    sub = sub_result.scalar_one_or_none()
    if not sub or sub.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only sub owner can pin posts")

    post.is_pinned = not post.is_pinned
    await db.flush()
    return {"is_pinned": post.is_pinned}


@router.post("/posts/{post_id}/remove", status_code=200)
async def remove_post(
    post_id: str,
    reason: str = "",
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Remove a post (mod action). Only sub owner can remove."""
    import uuid as _uuid
    from app.models.sub import Sub as SubModel
    try:
        pid = _uuid.UUID(post_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Post not found")

    result = await db.execute(select(Post).where(Post.id == pid))
    post = result.scalar_one_or_none()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    sub_result = await db.execute(select(SubModel).where(SubModel.id == post.sub_id))
    sub = sub_result.scalar_one_or_none()
    if not sub or sub.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only sub owner can remove posts")

    post.is_removed = not post.is_removed
    post.removed_reason = reason if post.is_removed else None
    await db.flush()
    return {"is_removed": post.is_removed}


@router.get("/posts/{post_id}", response_model=PostResponse)
async def get_post(
    post_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user),
):
    """Get a single post by ID."""
    post = await get_post_or_404(post_id, db)
    sub_result = await db.execute(select(Sub).where(Sub.id == post.sub_id))
    sub = sub_result.scalar_one_or_none()

    if sub and sub.sub_type == "private":
        if not current_user:
            raise HTTPException(status_code=403, detail="Login required")
        await require_membership(sub, current_user, db)

    author = await get_user_by_id(post.author_id, db)
    return format_post(post, author, sub.slug if sub else "")


@router.patch("/posts/{post_id}", response_model=PostResponse)
async def update_post(
    post_id: str,
    data: UpdatePostRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Edit your own post."""
    post = await get_post_or_404(post_id, db)

    if post.author_id != current_user.id:
        raise HTTPException(status_code=403, detail="You can only edit your own posts")

    post.content = data.content
    post.is_edited = True

    sub_result = await db.execute(select(Sub).where(Sub.id == post.sub_id))
    sub = sub_result.scalar_one()
    return format_post(post, current_user, sub.slug)


@router.delete("/posts/{post_id}", status_code=204)
async def delete_post(
    post_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete a post. Author or sub moderator/owner can delete."""
    post = await get_post_or_404(post_id, db)

    # Check if user is author
    if post.author_id == current_user.id:
        await db.delete(post)
        return

    # Check if user is mod or owner of the sub
    result = await db.execute(
        select(SubMembership).where(
            and_(
                SubMembership.sub_id == post.sub_id,
                SubMembership.user_id == current_user.id,
                SubMembership.role.in_(["owner", "moderator"]),
            )
        )
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=403, detail="Not authorized to delete this post")

    await db.delete(post)


# ── Comment Routes ─────────────────────────────────────────────────────────

@router.post("/posts/{post_id}/comments", response_model=CommentResponse, status_code=201)
async def create_comment(
    post_id: str,
    data: CreateCommentRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Add a comment or reply to a post. Must be a member of the sub."""
    post = await get_post_or_404(post_id, db)

    # Verify sub membership
    sub_result = await db.execute(select(Sub).where(Sub.id == post.sub_id))
    sub = sub_result.scalar_one()
    await require_membership(sub, current_user, db)

    # If replying, verify parent comment exists and belongs to same post
    parent_id = None
    if data.parent_id:
        parent_result = await db.execute(
            select(Comment).where(
                and_(Comment.id == data.parent_id, Comment.post_id == post.id)
            )
        )
        parent = parent_result.scalar_one_or_none()
        if not parent:
            raise HTTPException(status_code=404, detail="Parent comment not found")
        parent_id = parent.id

    comment = Comment(
        post_id=post.id,
        parent_id=parent_id,
        author_id=current_user.id,
        content=data.content,
    )
    db.add(comment)
    await db.flush()
    await db.refresh(comment)

    # Increment post comment count
    post.comment_count += 1

    # Notify post author (unless they commented on their own post)
    if post.author_id != current_user.id:
        await create_notification(
            db, post.author_id,
            type="post_comment",
            reference_id=post.id,
            reference_type="post",
            actor_id=current_user.id,
        )

    # Notify parent comment author if this is a reply
    if parent_id and parent and parent.author_id != current_user.id and parent.author_id != post.author_id:
        await create_notification(
            db, parent.author_id,
            type="comment_reply",
            reference_id=post.id,
            reference_type="post",
            actor_id=current_user.id,
        )

    return format_comment(comment, current_user)


@router.get("/posts/{post_id}/comments", response_model=list[CommentResponse])
async def list_comments(
    post_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user),
):
    """Get all comments for a post, threaded (replies nested under parents)."""
    post = await get_post_or_404(post_id, db)

    # Check private sub access
    sub_result = await db.execute(select(Sub).where(Sub.id == post.sub_id))
    sub = sub_result.scalar_one()
    if sub.sub_type == "private":
        if not current_user:
            raise HTTPException(status_code=403, detail="Login required")
        await require_membership(sub, current_user, db)

    # Fetch all comments for this post
    result = await db.execute(
        select(Comment)
        .where(Comment.post_id == post.id)
        .order_by(Comment.created_at.asc())
    )
    comments = result.scalars().all()

    # Fetch all authors in one query
    author_ids = list({c.author_id for c in comments})
    authors_result = await db.execute(select(User).where(User.id.in_(author_ids)))
    authors = {u.id: u for u in authors_result.scalars().all()}

    # Fetch which comments current user has hearted
    hearted_ids: set = set()
    if current_user and comments:
        comment_ids = [c.id for c in comments]
        hearts_result = await db.execute(
            select(CommentHeart.comment_id).where(
                CommentHeart.user_id == current_user.id,
                CommentHeart.comment_id.in_(comment_ids),
            )
        )
        hearted_ids = {r for r in hearts_result.scalars().all()}

    # Build threaded structure
    formatted = {
        str(c.id): format_comment(c, authors[c.author_id], has_hearted=c.id in hearted_ids)
        for c in comments if c.author_id in authors
    }

    # Nest replies under their parents
    top_level = []
    for c in comments:
        fc = formatted.get(str(c.id))
        if not fc:
            continue
        if c.parent_id is None:
            top_level.append(fc)
        else:
            parent = formatted.get(str(c.parent_id))
            if parent:
                parent.replies.append(fc)

    return top_level


@router.delete("/comments/{comment_id}", status_code=204)
async def delete_comment(
    comment_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete a comment. Author or sub mod/owner can delete."""
    result = await db.execute(select(Comment).where(Comment.id == comment_id))
    comment = result.scalar_one_or_none()
    if not comment:
        raise HTTPException(status_code=404, detail="Comment not found")

    if comment.author_id == current_user.id:
        await db.delete(comment)
        return

    # Check mod/owner
    post = await get_post_or_404(str(comment.post_id), db)
    mem_result = await db.execute(
        select(SubMembership).where(
            and_(
                SubMembership.sub_id == post.sub_id,
                SubMembership.user_id == current_user.id,
                SubMembership.role.in_(["owner", "moderator"]),
            )
        )
    )
    if not mem_result.scalar_one_or_none():
        raise HTTPException(status_code=403, detail="Not authorized to delete this comment")

    await db.delete(comment)


@router.post("/comments/{comment_id}/heart", status_code=200)
async def toggle_comment_heart(
    comment_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Toggle heart on a comment."""
    import uuid as _uuid
    try:
        cid = _uuid.UUID(comment_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Comment not found")

    result = await db.execute(select(Comment).where(Comment.id == cid))
    comment = result.scalar_one_or_none()
    if not comment:
        raise HTTPException(status_code=404, detail="Comment not found")

    existing = await db.execute(
        select(CommentHeart).where(
            CommentHeart.user_id == current_user.id,
            CommentHeart.comment_id == cid,
        )
    )
    heart = existing.scalar_one_or_none()
    if heart:
        await db.delete(heart)
        comment.heart_count = max(0, comment.heart_count - 1)
        has_hearted = False
    else:
        db.add(CommentHeart(user_id=current_user.id, comment_id=cid))
        comment.heart_count += 1
        has_hearted = True

    await db.flush()
    return {"heart_count": comment.heart_count, "has_hearted": has_hearted}
