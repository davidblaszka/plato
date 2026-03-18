# Plato — Backend

FastAPI + PostgreSQL backend for the Plato social platform.

## Stack

| Layer | Technology |
|-------|-----------|
| API | FastAPI (Python 3.12) |
| Database | PostgreSQL 16 |
| Auth | JWT (python-jose) |
| Real-time | WebSockets (native FastAPI) |
| Media storage | Cloudflare R2 (S3-compatible) |
| Encryption | AES-256 (server-side, E2EE planned) |
| Container | Docker Compose |

## Quick start

```bash
# 1. Clone and enter the directory
git clone https://github.com/your-org/plato.git
cd plato

# 2. Copy env and fill in your values
cp backend/.env.example backend/.env
# Edit backend/.env — at minimum set SECRET_KEY and R2 credentials

# 3. Start everything
docker compose up --build

# 4. API is live at http://localhost:8000
# 5. Interactive docs at http://localhost:8000/docs
```

The database schema is created automatically on first startup via SQLAlchemy `create_all`. Migrations for new columns are in `backend/migrations/` and must be run manually:

```bash
docker exec -i plato_db psql -U plato -d plato < backend/migrations/006_phase2.sql
docker exec -i plato_db psql -U plato -d plato < backend/migrations/007_notification_actor.sql
```

## Project structure

```
backend/
  app/
    core/          # Config, database session, JWT security
    models/        # SQLAlchemy models
    routers/       # FastAPI route handlers
    services/      # Encryption, media storage, WebSocket managers
  migrations/      # SQL migration files (run in order)
  requirements.txt
  Dockerfile
docker-compose.yml
```

## API overview

| Prefix | Description |
|--------|-------------|
| `/auth` | Register, login, me, refresh |
| `/subs` | Communities — create, join, post |
| `/posts` | Sub posts, comments, upvotes, pin, remove |
| `/feed` | Algorithmic sub feed |
| `/connections` | Mutual-follow connections + connections feed |
| `/public-accounts` | One-way follow, public account feed |
| `/messages` | E2E-encrypted DMs and group chats |
| `/notifications` | Real-time notification feed |
| `/users` | Profiles, profile posts, connect/disconnect |
| `/search` | Full-text search across users, subs, posts |
| `/media` | Presigned R2 upload URLs |

WebSocket endpoints:
- `ws://localhost:8000/ws/notifications?token=<jwt>` — real-time notification count
- `ws://localhost:8000/ws/messages?token=<jwt>` — real-time message delivery

## Environment variables

See `backend/.env.example` for all required variables with descriptions.

## Roadmap

- [x] Phase 1 — Auth, subs, posts, threaded comments, connections
- [x] Phase 2 — Public accounts, upvotes, ranking, message requests, search, sub moderation
- [x] Phase 3 — Server-side message encryption (Fernet/AES), real-time notifications via WebSocket, sub invite actioning, production deployment (Hetzner VPS, Nginx, SSL/TLS)
- [ ] Phase 4 — Client-side E2EE (Matrix protocol), data export, CSAM scanning, spam detection
- [ ] Phase 5 — ActivityPub federation
- [ ] Phase 6 — CDN, push notifications (APNs/FCM), Elasticsearch, video uploads
