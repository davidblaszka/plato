-- Soft-delete support for conversation participants
-- Allows a user to hide a 1:1 conversation from their inbox without
-- affecting the other participant's view.

ALTER TABLE conversation_participants
    ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP WITH TIME ZONE;
