import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.routers import auth, subs, posts, feed, connections, notifications, messages, media, users, search, public_accounts
from app.models import profile_post  # noqa: F401 - registers table
from app.models import social  # noqa: F401 - registers PostVote, ProfilePostVote, PublicAccountFollow
# Import all models so Base.metadata.create_all sees every table
from app.models import user, sub, post, connection, message, notification  # noqa: F401
from app.core.config import settings
from app.core.database import engine, Base

logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Auto-create any tables that don't exist yet (safe - won't drop existing)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database tables verified")
    yield

app = FastAPI(
    lifespan=lifespan,
    title="Plato API",
    description="Privacy-first social platform API",
    version="0.1.0",
    docs_url="/docs" if settings.environment == "development" else None,
    redoc_url="/redoc" if settings.environment == "development" else None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(subs.router)
app.include_router(posts.router)
app.include_router(feed.router)
app.include_router(connections.router)
app.include_router(notifications.router)
app.include_router(messages.router)
app.include_router(media.router)
app.include_router(users.router)
app.include_router(search.router)
app.include_router(public_accounts.router)


@app.get("/health")
async def health():
    return {"status": "ok", "environment": settings.environment}
