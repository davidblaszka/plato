-- Add parent_id to profile_post_comments to support threaded replies

ALTER TABLE profile_post_comments
    ADD COLUMN IF NOT EXISTS parent_id UUID REFERENCES profile_post_comments(id) ON DELETE CASCADE;

CREATE INDEX IF NOT EXISTS idx_profile_post_comments_parent
    ON profile_post_comments(parent_id);
