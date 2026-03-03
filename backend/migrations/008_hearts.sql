-- Rename upvotes to hearts, add comment hearts
ALTER TABLE posts RENAME COLUMN upvote_count TO heart_count;
ALTER TABLE profile_posts RENAME COLUMN upvote_count TO heart_count;
ALTER TABLE post_votes RENAME TO post_hearts;
ALTER TABLE profile_post_votes RENAME TO profile_post_hearts;

CREATE TABLE IF NOT EXISTS comment_hearts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    comment_id UUID NOT NULL REFERENCES comments(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(user_id, comment_id)
);

ALTER TABLE comments ADD COLUMN IF NOT EXISTS heart_count INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_comment_hearts_comment ON comment_hearts(comment_id);
CREATE INDEX IF NOT EXISTS idx_comment_hearts_user ON comment_hearts(user_id);
