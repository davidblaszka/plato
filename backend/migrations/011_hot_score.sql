-- Migration 011: Hot ranking score for posts and profile posts

-- Add hot_score column to sub posts
ALTER TABLE posts ADD COLUMN IF NOT EXISTS hot_score FLOAT NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS idx_posts_hot_score ON posts(hot_score DESC);

-- Backfill existing sub posts (PostgreSQL log() = log base 10)
UPDATE posts
SET hot_score = round(
    (log(GREATEST(heart_count, 1)) + extract(epoch from created_at) / 45000)::numeric,
    7
)::float;

-- Add hot_score column to profile posts
ALTER TABLE profile_posts ADD COLUMN IF NOT EXISTS hot_score FLOAT NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS idx_profile_posts_hot_score ON profile_posts(hot_score DESC);

-- Backfill existing profile posts
UPDATE profile_posts
SET hot_score = round(
    (log(GREATEST(heart_count, 1)) + extract(epoch from created_at) / 45000)::numeric,
    7
)::float;
