-- Add media_urls to connection_posts
ALTER TABLE connection_posts
    ADD COLUMN IF NOT EXISTS media_urls TEXT[] NOT NULL DEFAULT '{}';
