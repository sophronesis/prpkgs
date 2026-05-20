-- prpkgs database schema
-- one row per (PR, package) pair. a single PR can introduce multiple packages
-- (e.g. "foo: init at 1.0, bar: init at 2.0") so name alone is not unique.

CREATE TABLE IF NOT EXISTS pending_packages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    pr_number     INTEGER NOT NULL,
    name          TEXT NOT NULL,
    attr_path     TEXT NOT NULL DEFAULT '',
    version       TEXT,
    author        TEXT NOT NULL,
    pr_url        TEXT NOT NULL,
    pr_title      TEXT NOT NULL,
    pr_body       TEXT,
    state         TEXT NOT NULL DEFAULT 'open',
    labels        TEXT,                       -- json array
    draft         INTEGER NOT NULL DEFAULT 0,
    merge_ready   INTEGER NOT NULL DEFAULT 0, -- 2.status: merge-bot eligible
    head_rev      TEXT,                       -- full SHA of PR head commit
    nar_hash      TEXT,                       -- SRI sha256 of unpacked tarball
    pr_created_at TEXT NOT NULL,
    pr_updated_at TEXT,
    discovered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_synced   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(pr_number, attr_path)
);

CREATE INDEX IF NOT EXISTS idx_pending_name ON pending_packages(name);
CREATE INDEX IF NOT EXISTS idx_pending_attr_path ON pending_packages(attr_path);
CREATE INDEX IF NOT EXISTS idx_pending_pr_number ON pending_packages(pr_number);
CREATE INDEX IF NOT EXISTS idx_pending_merge_ready ON pending_packages(merge_ready);
CREATE INDEX IF NOT EXISTS idx_pending_state ON pending_packages(state);
CREATE INDEX IF NOT EXISTS idx_pending_head_rev ON pending_packages(head_rev);

CREATE VIRTUAL TABLE IF NOT EXISTS pending_packages_fts USING fts5(
    name,
    attr_path,
    pr_title,
    pr_body,
    content='pending_packages',
    content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS pending_packages_ai AFTER INSERT ON pending_packages BEGIN
    INSERT INTO pending_packages_fts(rowid, name, attr_path, pr_title, pr_body)
    VALUES (new.id, new.name, new.attr_path, new.pr_title, new.pr_body);
END;

CREATE TRIGGER IF NOT EXISTS pending_packages_ad AFTER DELETE ON pending_packages BEGIN
    INSERT INTO pending_packages_fts(pending_packages_fts, rowid, name, attr_path, pr_title, pr_body)
    VALUES ('delete', old.id, old.name, old.attr_path, old.pr_title, old.pr_body);
END;

CREATE TRIGGER IF NOT EXISTS pending_packages_au AFTER UPDATE ON pending_packages BEGIN
    INSERT INTO pending_packages_fts(pending_packages_fts, rowid, name, attr_path, pr_title, pr_body)
    VALUES ('delete', old.id, old.name, old.attr_path, old.pr_title, old.pr_body);
    INSERT INTO pending_packages_fts(rowid, name, attr_path, pr_title, pr_body)
    VALUES (new.id, new.name, new.attr_path, new.pr_title, new.pr_body);
END;

-- shared hash cache: key by full commit SHA so we never re-prefetch the same
-- tree, even across different PRs that happen to share a commit.
CREATE TABLE IF NOT EXISTS prefetch_cache (
    rev          TEXT PRIMARY KEY,
    nar_hash     TEXT NOT NULL,
    computed_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sync_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    prs_seen        INTEGER NOT NULL DEFAULT 0,
    packages_seen   INTEGER NOT NULL DEFAULT 0,
    errors          INTEGER NOT NULL DEFAULT 0,
    note            TEXT
);
