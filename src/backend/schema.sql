
-- 1. Cameras table
CREATE TABLE IF NOT EXISTS cameras (
    id TEXT PRIMARY KEY,
    role TEXT NOT NULL
);

-- 2. Events table
CREATE TABLE IF NOT EXISTS events (
    id BIGSERIAL PRIMARY KEY,
    camera_id TEXT REFERENCES cameras(id),
    track_id INT,
    event_type TEXT NOT NULL,
    ts TIMESTAMPTZ NOT NULL,
    meta JSONB DEFAULT '{}'
);

-- 3. Indexes to make searching faster
CREATE INDEX IF NOT EXISTS idx_events_ts
ON events(ts);

CREATE INDEX IF NOT EXISTS idx_events_type
ON events(event_type);

-- 4. Daily statistics table
CREATE TABLE IF NOT EXISTS daily_stats (
    day DATE PRIMARY KEY,
    total_entered INT DEFAULT 0,
    total_exited INT DEFAULT 0,
    peak_occupancy INT DEFAULT 0
);

-- 5. Insert our project cameras
INSERT INTO cameras (id, role)
VALUES
    ('entrance', 'entrance'),
    ('exit', 'exit'),
    ('wide', 'wide')
ON CONFLICT (id) DO NOTHING;
