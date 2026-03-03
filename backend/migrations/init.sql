-- Plato: Initial Schema
-- Run automatically by Postgres on first container start

CREATE EXTENSION IF NOT EXISTS "pgcrypto";  -- for gen_random_uuid()

-- ── USERS ──────────────────────────────────────────────────────────────────
CREATE TABLE users (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    username        TEXT NOT NULL UNIQUE,
    email_hash      TEXT NOT NULL UNIQUE,       -- hashed, never stored plaintext
    password_hash   TEXT NOT NULL,
    display_name    TEXT,
    bio             TEXT,
    avatar_url      TEXT,
    public_key      TEXT,                       -- reserved for E2EE (Matrix) later
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_users_username ON users(username);

-- ── CONNECTIONS (mutual follow) ────────────────────────────────────────────
CREATE TABLE connections (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    requester_id    UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    addressee_id    UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    status          TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'accepted', 'blocked')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (requester_id, addressee_id)
);

CREATE INDEX idx_connections_addressee ON connections(addressee_id);
CREATE INDEX idx_connections_status ON connections(status);

-- ── SUBS ───────────────────────────────────────────────────────────────────
CREATE TABLE subs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT NOT NULL,
    slug            TEXT NOT NULL UNIQUE,       -- url-safe name e.g. "pnw-running"
    description     TEXT,
    sub_type        TEXT NOT NULL DEFAULT 'public'
                        CHECK (sub_type IN ('public', 'private', 'connections')),
    join_policy     TEXT NOT NULL DEFAULT 'open'
                        CHECK (join_policy IN ('open', 'approval', 'invite')),
    owner_id        UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    avatar_url      TEXT,
    member_count    INTEGER NOT NULL DEFAULT 1,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_subs_slug ON subs(slug);
CREATE INDEX idx_subs_type ON subs(sub_type);

-- ── SUB MEMBERSHIPS ────────────────────────────────────────────────────────
CREATE TABLE sub_memberships (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    sub_id          UUID NOT NULL REFERENCES subs(id) ON DELETE CASCADE,
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role            TEXT NOT NULL DEFAULT 'member'
                        CHECK (role IN ('owner', 'moderator', 'member')),
    joined_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (sub_id, user_id)
);

CREATE INDEX idx_memberships_user ON sub_memberships(user_id);

-- ── POSTS ──────────────────────────────────────────────────────────────────
CREATE TABLE posts (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    sub_id          UUID NOT NULL REFERENCES subs(id) ON DELETE CASCADE,
    author_id       UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    content         TEXT,
    media_urls      TEXT[] DEFAULT '{}',        -- array of image/video URLs
    is_edited       BOOLEAN NOT NULL DEFAULT FALSE,
    comment_count   INTEGER NOT NULL DEFAULT 0,
    -- reserved for federation (Phase 4+)
    ap_id           TEXT UNIQUE,                -- ActivityPub object ID
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_posts_sub ON posts(sub_id, created_at DESC);
CREATE INDEX idx_posts_author ON posts(author_id);

-- ── COMMENTS ───────────────────────────────────────────────────────────────
CREATE TABLE comments (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    post_id         UUID NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    parent_id       UUID REFERENCES comments(id) ON DELETE CASCADE,  -- null = top-level
    author_id       UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    content         TEXT NOT NULL,
    is_edited       BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_comments_post ON comments(post_id, created_at ASC);

-- ── NOTIFICATIONS ──────────────────────────────────────────────────────────
CREATE TABLE notifications (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    type            TEXT NOT NULL,              -- 'connection_request', 'post_comment', etc.
    reference_id    UUID,                       -- ID of the related object
    reference_type  TEXT,                       -- 'post', 'comment', 'connection', etc.
    is_read         BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_notifications_user ON notifications(user_id, is_read, created_at DESC);

-- ── updated_at auto-update trigger ────────────────────────────────────────
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_users_updated_at
    BEFORE UPDATE ON users FOR EACH ROW EXECUTE FUNCTION update_updated_at();
CREATE TRIGGER trg_subs_updated_at
    BEFORE UPDATE ON subs FOR EACH ROW EXECUTE FUNCTION update_updated_at();
CREATE TRIGGER trg_posts_updated_at
    BEFORE UPDATE ON posts FOR EACH ROW EXECUTE FUNCTION update_updated_at();
CREATE TRIGGER trg_comments_updated_at
    BEFORE UPDATE ON comments FOR EACH ROW EXECUTE FUNCTION update_updated_at();
