-- Migration 012: E2EE messaging support
-- Phase 4 — adds fields to support client-side encryption relay model.
-- Existing messages remain readable (is_encrypted=FALSE, legacy plaintext).

-- 1. public_key already exists on users table (added in init.sql).
--    Ensure the column exists (safe no-op if already present).
ALTER TABLE users ADD COLUMN IF NOT EXISTS public_key TEXT;

-- 2. Mark whether a message carries client-side ciphertext or legacy
--    server-encrypted content. Defaults to FALSE so existing rows are unaffected.
ALTER TABLE messages ADD COLUMN IF NOT EXISTS is_encrypted BOOLEAN NOT NULL DEFAULT FALSE;

-- 3. Per-recipient copy support for group E2EE messages.
--    recipient_id = NULL means the message is a broadcast (legacy or direct).
--    recipient_id != NULL means this row is the copy for that specific recipient.
ALTER TABLE messages ADD COLUMN IF NOT EXISTS recipient_id UUID REFERENCES users(id) ON DELETE CASCADE;

-- 4. Index so fetching a recipient's messages in a conversation is fast.
CREATE INDEX IF NOT EXISTS idx_messages_recipient
    ON messages(recipient_id, conversation_id);
