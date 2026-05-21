"""SQLite storage and search for prpkgs."""

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from .models import PendingPackage

DEFAULT_DB_PATH = Path.home() / ".local/share/prpkgs/prpkgs.db"
SCHEMA_PATH = Path(__file__).parent.parent / "db" / "schema.sql"


class Database:
    """SQLite database with FTS5 for the pending-packages index."""

    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn: sqlite3.Connection | None = None

    def connect(self) -> sqlite3.Connection:
        if self.conn is None:
            self.conn = sqlite3.connect(self.db_path)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA foreign_keys = ON")
            self._init_schema()
            self._migrate()
        return self.conn

    def _init_schema(self) -> None:
        schema = SCHEMA_PATH.read_text()
        self.conn.executescript(schema)
        self.conn.commit()

    def _migrate(self) -> None:
        """Add columns added after the initial schema landed in older dbs."""
        cols = {row["name"] for row in self.conn.execute("PRAGMA table_info(pending_packages)")}
        if "head_rev" not in cols:
            self.conn.execute("ALTER TABLE pending_packages ADD COLUMN head_rev TEXT")
        if "nar_hash" not in cols:
            self.conn.execute("ALTER TABLE pending_packages ADD COLUMN nar_hash TEXT")
        self.conn.commit()

    def close(self) -> None:
        if self.conn:
            self.conn.close()
            self.conn = None

    def __enter__(self) -> "Database":
        self.connect()
        return self

    def __exit__(self, *args) -> None:
        self.close()

    # writes

    def upsert(self, pkg: PendingPackage) -> int:
        """Insert or update a (pr_number, attr_path) row, return its id.

        If a known PR has a new head_rev compared to what we already stored,
        the previous nar_hash is wiped so the next prefetch picks the new tree.
        """
        now = datetime.now().isoformat(timespec="seconds")
        existing = self.conn.execute(
            "SELECT head_rev, nar_hash FROM pending_packages WHERE pr_number = ? AND attr_path = ?",
            (pkg.pr_number, pkg.attr_path or ""),
        ).fetchone()

        nar_hash = pkg.nar_hash
        if existing is not None:
            prev_rev = existing["head_rev"]
            prev_hash = existing["nar_hash"]
            if pkg.head_rev and prev_rev and pkg.head_rev != prev_rev:
                # PR moved; invalidate the cached hash for this row
                nar_hash = None
            elif prev_hash and not nar_hash:
                # carry forward the existing hash if the new row didn't supply one
                nar_hash = prev_hash

        cursor = self.conn.execute(
            """
            INSERT INTO pending_packages (
                pr_number, name, attr_path, version, author,
                pr_url, pr_title, pr_body, state, labels,
                draft, merge_ready, head_rev, nar_hash,
                pr_created_at, pr_updated_at, last_synced
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(pr_number, attr_path) DO UPDATE SET
                name = excluded.name,
                version = excluded.version,
                author = excluded.author,
                pr_url = excluded.pr_url,
                pr_title = excluded.pr_title,
                pr_body = excluded.pr_body,
                state = excluded.state,
                labels = excluded.labels,
                draft = excluded.draft,
                merge_ready = excluded.merge_ready,
                head_rev = excluded.head_rev,
                nar_hash = excluded.nar_hash,
                pr_updated_at = excluded.pr_updated_at,
                last_synced = excluded.last_synced
            RETURNING id
            """,
            (
                pkg.pr_number,
                pkg.name,
                pkg.attr_path or "",
                pkg.version,
                pkg.author,
                pkg.pr_url,
                pkg.pr_title,
                pkg.pr_body,
                pkg.state,
                json.dumps(pkg.labels),
                1 if pkg.draft else 0,
                1 if pkg.merge_ready else 0,
                pkg.head_rev,
                nar_hash,
                pkg.pr_created_at,
                pkg.pr_updated_at,
                now,
            ),
        )
        row = cursor.fetchone()
        self.conn.commit()
        return row["id"]

    def set_head_rev(self, pr_number: int, rev: str) -> None:
        """Record the head SHA of a PR.

        Clears any cached nar_hash on rows whose stored rev differs.
        """
        self.conn.execute(
            """
            UPDATE pending_packages
            SET nar_hash = NULL
            WHERE pr_number = ? AND head_rev IS NOT NULL AND head_rev <> ?
            """,
            (pr_number, rev),
        )
        self.conn.execute(
            "UPDATE pending_packages SET head_rev = ? WHERE pr_number = ?",
            (rev, pr_number),
        )
        self.conn.commit()

    def set_nar_hash_for_rev(self, rev: str, nar_hash: str) -> int:
        """Stamp a known narHash onto every row that points at this rev."""
        cursor = self.conn.execute(
            "UPDATE pending_packages SET nar_hash = ? WHERE head_rev = ?",
            (nar_hash, rev),
        )
        self.conn.commit()
        return cursor.rowcount

    def delete_pr(self, pr_number: int) -> int:
        cursor = self.conn.execute("DELETE FROM pending_packages WHERE pr_number = ?", (pr_number,))
        self.conn.commit()
        return cursor.rowcount

    def prune_not_seen_since(self, cutoff_iso: str) -> int:
        cursor = self.conn.execute(
            "DELETE FROM pending_packages WHERE last_synced < ?", (cutoff_iso,)
        )
        self.conn.commit()
        return cursor.rowcount

    def clear(self) -> int:
        cursor = self.conn.execute("DELETE FROM pending_packages")
        self.conn.commit()
        return cursor.rowcount

    def record_sync(
        self,
        started_at: str,
        prs_seen: int,
        packages_seen: int,
        errors: int,
        note: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO sync_runs (started_at, finished_at, prs_seen, packages_seen, errors, note)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                started_at,
                datetime.now().isoformat(timespec="seconds"),
                prs_seen,
                packages_seen,
                errors,
                note,
            ),
        )
        self.conn.commit()

    # prefetch cache (shared across PRs that share a commit)

    def cache_get(self, rev: str) -> str | None:
        row = self.conn.execute(
            "SELECT nar_hash FROM prefetch_cache WHERE rev = ?", (rev,)
        ).fetchone()
        return row["nar_hash"] if row else None

    def cache_put(self, rev: str, nar_hash: str) -> None:
        self.conn.execute(
            """
            INSERT INTO prefetch_cache (rev, nar_hash, computed_at)
            VALUES (?, ?, ?)
            ON CONFLICT(rev) DO UPDATE SET
                nar_hash = excluded.nar_hash,
                computed_at = excluded.computed_at
            """,
            (rev, nar_hash, datetime.now().isoformat(timespec="seconds")),
        )
        self.conn.commit()

    def revs_needing_hash(self) -> list[str]:
        """Distinct head_revs whose pending_packages rows still lack a nar_hash.

        We don't filter by prefetch_cache here on purpose: prefetch_many's
        phase 1 consults the cache and writes the hash onto the row, so even
        cache-hit revs need to flow through to drive that row update.
        """
        cursor = self.conn.execute(
            """
            SELECT DISTINCT head_rev AS rev
            FROM pending_packages
            WHERE head_rev IS NOT NULL
              AND (nar_hash IS NULL OR nar_hash = '')
            """
        )
        return [row["rev"] for row in cursor]

    # reads

    def get_pr_meta(self, pr_number: int) -> dict | None:
        row = self.conn.execute(
            "SELECT MIN(pr_updated_at) AS pr_updated_at, MIN(head_rev) AS head_rev "
            "FROM pending_packages WHERE pr_number = ?",
            (pr_number,),
        ).fetchone()
        if row is None or row["pr_updated_at"] is None:
            return None
        return {"pr_updated_at": row["pr_updated_at"], "head_rev": row["head_rev"]}

    def check(self, name: str) -> list[PendingPackage]:
        cursor = self.conn.execute(
            """
            SELECT * FROM pending_packages
            WHERE name = ? OR attr_path = ?
            ORDER BY merge_ready DESC, pr_updated_at DESC, pr_created_at DESC
            """,
            (name, name),
        )
        return [self._row(r) for r in cursor]

    def search(self, query: str, limit: int = 20) -> list[PendingPackage]:
        try:
            cursor = self.conn.execute(
                """
                SELECT p.*, bm25(pending_packages_fts) AS rank
                FROM pending_packages_fts
                JOIN pending_packages p ON pending_packages_fts.rowid = p.id
                WHERE pending_packages_fts MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (query, limit),
            )
            return [self._row(r) for r in cursor]
        except sqlite3.OperationalError:
            like = f"%{query}%"
            cursor = self.conn.execute(
                """
                SELECT * FROM pending_packages
                WHERE name LIKE ? OR attr_path LIKE ? OR pr_title LIKE ?
                ORDER BY merge_ready DESC, pr_updated_at DESC
                LIMIT ?
                """,
                (like, like, like, limit),
            )
            return [self._row(r) for r in cursor]

    def list_recent(
        self,
        limit: int = 20,
        merge_ready_only: bool = False,
        include_drafts: bool = True,
        only_with_hash: bool = False,
    ) -> list[PendingPackage]:
        sql = "SELECT * FROM pending_packages WHERE 1=1"
        params: list = []
        if merge_ready_only:
            sql += " AND merge_ready = 1"
        if not include_drafts:
            sql += " AND draft = 0"
        if only_with_hash:
            sql += " AND nar_hash IS NOT NULL AND nar_hash <> ''"
        sql += " ORDER BY pr_updated_at DESC, pr_created_at DESC LIMIT ?"
        params.append(limit)
        cursor = self.conn.execute(sql, params)
        return [self._row(r) for r in cursor]

    def get_by_pr(self, pr_number: int) -> list[PendingPackage]:
        cursor = self.conn.execute(
            "SELECT * FROM pending_packages WHERE pr_number = ? ORDER BY attr_path",
            (pr_number,),
        )
        return [self._row(r) for r in cursor]

    def stats(self) -> dict:
        total = self.conn.execute("SELECT COUNT(*) FROM pending_packages").fetchone()[0]
        prs = self.conn.execute(
            "SELECT COUNT(DISTINCT pr_number) FROM pending_packages"
        ).fetchone()[0]
        ready = self.conn.execute(
            "SELECT COUNT(*) FROM pending_packages WHERE merge_ready = 1"
        ).fetchone()[0]
        drafts = self.conn.execute(
            "SELECT COUNT(*) FROM pending_packages WHERE draft = 1"
        ).fetchone()[0]
        with_hash = self.conn.execute(
            "SELECT COUNT(*) FROM pending_packages WHERE nar_hash IS NOT NULL AND nar_hash <> ''"
        ).fetchone()[0]
        cache_size = self.conn.execute("SELECT COUNT(*) FROM prefetch_cache").fetchone()[0]
        last_sync = self.conn.execute(
            "SELECT finished_at, prs_seen, packages_seen FROM sync_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return {
            "packages": total,
            "prs": prs,
            "merge_ready": ready,
            "drafts": drafts,
            "with_hash": with_hash,
            "cache_size": cache_size,
            "last_sync": dict(last_sync) if last_sync else None,
        }

    # row -> model

    def _row(self, row: sqlite3.Row) -> PendingPackage:
        labels_raw = row["labels"]
        try:
            labels = json.loads(labels_raw) if labels_raw else []
        except (TypeError, json.JSONDecodeError):
            labels = []
        return PendingPackage(
            id=row["id"],
            pr_number=row["pr_number"],
            name=row["name"],
            attr_path=row["attr_path"] or "",
            version=row["version"],
            author=row["author"],
            pr_url=row["pr_url"],
            pr_title=row["pr_title"],
            pr_body=row["pr_body"],
            state=row["state"],
            labels=labels,
            draft=bool(row["draft"]),
            merge_ready=bool(row["merge_ready"]),
            head_rev=row["head_rev"] if "head_rev" in row.keys() else None,
            nar_hash=row["nar_hash"] if "nar_hash" in row.keys() else None,
            pr_created_at=row["pr_created_at"],
            pr_updated_at=row["pr_updated_at"],
        )
