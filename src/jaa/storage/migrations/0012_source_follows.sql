-- Boards and other source targets the user follows (#71, ADR 0025).

-- Append-only: the latest row per (source, target) decides; `follow` means `jaa jobs fetch
-- --followed` fetches it. `source` is a connector key and `target` its canonical target
-- (`Connector.target_key`), not foreign keys, because connectors are defined in code.
CREATE TABLE source_follows (
    id          INTEGER PRIMARY KEY,
    source      TEXT NOT NULL,
    target      TEXT NOT NULL,
    action      TEXT NOT NULL CHECK (action IN ('follow', 'unfollow')),
    occurred_at TEXT NOT NULL
);
CREATE INDEX source_follows_target ON source_follows (source, target, id);

CREATE TRIGGER source_follows_immutable BEFORE UPDATE ON source_follows
BEGIN
    SELECT RAISE(ABORT, 'source follows are append-only');
END;
