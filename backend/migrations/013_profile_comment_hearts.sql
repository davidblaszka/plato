-- Add heart_count to profile_post_comments and create heart join table

ALTER TABLE profile_post_comments
    ADD COLUMN IF NOT EXISTS heart_count INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS profile_post_comment_hearts (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    comment_id  UUID NOT NULL REFERENCES profile_post_comments(id) ON DELETE CASCADE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_profile_post_comment_heart UNIQUE (user_id, comment_id)
);
