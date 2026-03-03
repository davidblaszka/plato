-- Migration: add connection_posts table
-- Run this manually or via your migration tool

CREATE TABLE IF NOT EXISTS connection_posts (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    author_id   UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    content     TEXT NOT NULL,
    is_edited   BOOLEAN NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_connection_posts_author
    ON connection_posts(author_id, created_at DESC);

CREATE TRIGGER trg_connection_posts_updated_at
    BEFORE UPDATE ON connection_posts
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();
