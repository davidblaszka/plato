-- Add client_id to messages to link per-recipient E2EE group copies

ALTER TABLE messages
    ADD COLUMN IF NOT EXISTS client_id UUID;

CREATE INDEX IF NOT EXISTS idx_messages_client_id
    ON messages(conversation_id, sender_id, client_id);
