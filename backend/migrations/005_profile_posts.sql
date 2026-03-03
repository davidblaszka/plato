-- Profile posts: content posted directly to a user's profile (like Instagram)
CREATE TABLE IF NOT EXISTS profile_posts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    author_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    content TEXT NOT NULL CHECK (char_length(content) <= 10000),
    media_urls TEXT[] NOT NULL DEFAULT '{}',
    is_edited BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_profile_posts_author ON profile_posts(author_id, created_at DESC);
