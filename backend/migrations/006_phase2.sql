-- Phase 2: Public accounts, ranking, message requests, search indexes, sub moderation

-- 1. Public accounts: add account_type + is_verified to users
ALTER TABLE users ADD COLUMN IF NOT EXISTS account_type VARCHAR(20) NOT NULL DEFAULT 'personal';
ALTER TABLE users ADD COLUMN IF NOT EXISTS is_verified BOOLEAN NOT NULL DEFAULT FALSE;

-- Public account followers (one-way, not mutual)
CREATE TABLE IF NOT EXISTS public_account_follows (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    follower_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    followed_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(follower_id, followed_id)
);
CREATE INDEX IF NOT EXISTS idx_paf_follower ON public_account_follows(follower_id);
CREATE INDEX IF NOT EXISTS idx_paf_followed ON public_account_follows(followed_id);

-- 2. Ranking: upvotes on posts and profile posts
ALTER TABLE posts ADD COLUMN IF NOT EXISTS upvote_count INT NOT NULL DEFAULT 0;
ALTER TABLE profile_posts ADD COLUMN IF NOT EXISTS upvote_count INT NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS post_votes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    post_id UUID NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(user_id, post_id)
);
CREATE INDEX IF NOT EXISTS idx_post_votes_post ON post_votes(post_id);

CREATE TABLE IF NOT EXISTS profile_post_votes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    post_id UUID NOT NULL REFERENCES profile_posts(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(user_id, post_id)
);

-- 3. Message requests: add status to conversations
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS status VARCHAR(20) NOT NULL DEFAULT 'active';
-- Mark existing conversations as active
UPDATE conversations SET status = 'active' WHERE status IS NULL OR status = '';

-- 4. Sub moderation: pinned and removed flags on posts
ALTER TABLE posts ADD COLUMN IF NOT EXISTS is_pinned BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE posts ADD COLUMN IF NOT EXISTS is_removed BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE posts ADD COLUMN IF NOT EXISTS removed_reason VARCHAR(500);

-- 5. Search indexes: GIN indexes on text columns for fast ILIKE search
CREATE INDEX IF NOT EXISTS idx_users_username_search ON users USING gin(to_tsvector('english', username));
CREATE INDEX IF NOT EXISTS idx_users_display_search ON users USING gin(to_tsvector('english', coalesce(display_name, '')));
CREATE INDEX IF NOT EXISTS idx_subs_name_search ON subs USING gin(to_tsvector('english', name));
CREATE INDEX IF NOT EXISTS idx_subs_desc_search ON subs USING gin(to_tsvector('english', coalesce(description, '')));
CREATE INDEX IF NOT EXISTS idx_posts_content_search ON posts USING gin(to_tsvector('english', coalesce(content, '')));
