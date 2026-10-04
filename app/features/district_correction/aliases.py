"""Corrections remembered per company and governorate.

Two sources feed it: a reviewer confirming a district (the test page or any client of
the feedback endpoint), and answers on which two independent methods agreed (the LLM
and the rules, or the LLM or rules and semantic search). The same text (up to spelling,
punctuation and the governorate name) is then settled without the LLM. A reviewer's
answer always wins: automatic learning never replaces it. Kept in its own SQLite file
so re-importing the Excel catalog never erases it.
"""

import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

REVIEWER = "reviewer"
AUTO = "auto"

SCHEMA = """
CREATE TABLE IF NOT EXISTS district_aliases (
    company TEXT NOT NULL, state_code TEXT NOT NULL, alias_key TEXT NOT NULL,
    district_name TEXT NOT NULL, sample_text TEXT NOT NULL,
    confirmations INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'reviewer',
    PRIMARY KEY (company, state_code, alias_key)
);
"""

# An automatic answer only replaces another automatic one; a reviewer replaces anything.
UPSERT = """
INSERT INTO district_aliases
    (company, state_code, alias_key, district_name, sample_text, confirmations, updated_at, source)
VALUES (?, ?, ?, ?, ?, 1, ?, ?)
ON CONFLICT (company, state_code, alias_key) DO UPDATE SET
    confirmations = CASE WHEN district_name = excluded.district_name AND source = excluded.source
                         THEN confirmations + 1 ELSE 1 END,
    district_name = excluded.district_name, sample_text = excluded.sample_text,
    updated_at = excluded.updated_at, source = excluded.source
WHERE district_aliases.source = 'auto' OR excluded.source = 'reviewer'
"""


class AliasStore:
    """SQLite-backed memory with an in-process copy for lookups during matching."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with closing(sqlite3.connect(self.path)) as db:
            db.executescript(SCHEMA)
            columns = {row[1] for row in db.execute("PRAGMA table_info(district_aliases)")}
            if "source" not in columns:  # files created before automatic learning
                db.execute("ALTER TABLE district_aliases ADD COLUMN source TEXT NOT NULL DEFAULT 'reviewer'")
                db.commit()
            rows = db.execute("SELECT company, state_code, alias_key, district_name, source "
                              "FROM district_aliases").fetchall()
        self._entries = {(company, state, key): (name, source) for company, state, key, name, source in rows}

    def __len__(self) -> int:
        return len(self._entries)

    def counts(self) -> dict[str, int]:
        counts = {REVIEWER: 0, AUTO: 0}
        for _, source in self._entries.values():
            counts[source] = counts.get(source, 0) + 1
        return counts

    def get(self, company: str, state_code: str, key: str) -> tuple[str, str] | None:
        """(district, source) remembered for this text key, or None."""
        return self._entries.get((company, state_code, key)) if key else None

    def learn(self, rows: list[tuple[str, str, str, str, str]], source: str = REVIEWER) -> int:
        """Save (company, state, key, district, sample text) rows; returns how many were stored."""
        with self._lock:
            rows = [row for row in rows
                    if source == REVIEWER or self._entries.get(tuple(row[:3]), (None, AUTO))[1] == AUTO]
            if not rows:
                return 0
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            with closing(sqlite3.connect(self.path)) as db:
                db.executemany(UPSERT, [(*row, now, source) for row in rows])
                db.commit()
            for company, state, key, name, _ in rows:
                self._entries[(company, state, key)] = (name, source)
        return len(rows)

    def forget(self, keys: list[tuple[str, str, str]]) -> int:
        """Remove remembered corrections; returns how many existed."""
        with self._lock, closing(sqlite3.connect(self.path)) as db:
            removed = 0
            for company, state, key in keys:
                removed += db.execute("DELETE FROM district_aliases WHERE company = ? AND state_code = ? "
                                      "AND alias_key = ?", (company, state, key)).rowcount
                self._entries.pop((company, state, key), None)
            db.commit()
        return removed
