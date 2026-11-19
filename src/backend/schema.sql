
-- 1. Cameras table
CREATE TABLE IF NOT EXISTS cameras (
    id TEXT PRIMARY KEY,
    role TEXT NOT NULL
);

-- 2. Events table
CREATE TABLE IF NOT EXISTS events (
    id BIGSERIAL PRIMARY KEY,
    camera_id TEXT NOT NULL REFERENCES cameras(id),
    track_id INT,
    event_type TEXT NOT NULL,
    ts TIMESTAMPTZ NOT NULL,
    table_id TEXT,
    meta JSONB DEFAULT '{}'
);

-- 3. Events indexes
CREATE INDEX IF NOT EXISTS idx_events_ts
ON events(ts);

CREATE INDEX IF NOT EXISTS idx_events_type
ON events(event_type);

CREATE INDEX IF NOT EXISTS idx_events_table_id
ON events(table_id);

-- 4. Daily statistics table
CREATE TABLE IF NOT EXISTS daily_stats (
    day DATE PRIMARY KEY,
    total_entered INT DEFAULT 0,
    total_exited INT DEFAULT 0,
    peak_occupancy INT DEFAULT 0
);

-- 5. Insert project cameras
INSERT INTO cameras (id, role)
VALUES
    ('entrance', 'entrance'),
    ('exit', 'exit'),
    ('wide', 'wide')
ON CONFLICT (id) DO NOTHING;

-- 6. Restaurant tables
CREATE TABLE IF NOT EXISTS restaurant_tables (
    id TEXT PRIMARY KEY,
    camera_id TEXT NOT NULL REFERENCES cameras(id),
    polygon JSONB NOT NULL,
    source TEXT DEFAULT 'automatic',
    active BOOLEAN DEFAULT TRUE,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- 7. Current table status
CREATE TABLE IF NOT EXISTS table_status (
    table_id TEXT PRIMARY KEY
        REFERENCES restaurant_tables(id),
    status TEXT NOT NULL
        CHECK (status IN ('occupied', 'free')),
    since TIMESTAMPTZ,
    people INT DEFAULT 0,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- 8. Table sessions
CREATE TABLE IF NOT EXISTS table_sessions (
    id BIGSERIAL PRIMARY KEY,
    table_id TEXT NOT NULL REFERENCES restaurant_tables(id),
    started_at TIMESTAMPTZ NOT NULL,
    ended_at TIMESTAMPTZ,
    max_people INT DEFAULT 0
);

-- 9. Room counts
CREATE TABLE IF NOT EXISTS room_counts (
    id BIGSERIAL PRIMARY KEY,
    camera_id TEXT NOT NULL REFERENCES cameras(id),
    ts TIMESTAMPTZ NOT NULL,
    people INT NOT NULL
);

-- 10. Additional indexes
CREATE INDEX IF NOT EXISTS idx_room_counts_ts
ON room_counts(ts);

CREATE INDEX IF NOT EXISTS idx_table_sessions_table_id
ON table_sessions(table_id);

-- 11. Prevent duplicate events
CREATE UNIQUE INDEX IF NOT EXISTS idx_events_dedup
ON events (
    camera_id,
    event_type,
    ts,
    (COALESCE(track_id, -1)),
    (COALESCE(table_id, ''))
);
