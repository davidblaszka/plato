-- Sub join requests table
CREATE TABLE IF NOT EXISTS sub_join_requests (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    sub_id UUID NOT NULL REFERENCES subs(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(sub_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_sub_join_requests_sub ON sub_join_requests(sub_id);
CREATE INDEX IF NOT EXISTS idx_sub_join_requests_user ON sub_join_requests(user_id);
