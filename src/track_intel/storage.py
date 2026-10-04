"""赛道比较服务的 SQLite 模式与事务辅助。"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path


SCHEMA_VERSION = 1

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('analyst', 'reviewer', 'viewer', 'auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);

CREATE TABLE IF NOT EXISTS record_registry (
    record_id TEXT PRIMARY KEY,
    registered_by TEXT NOT NULL REFERENCES users(user_id),
    registered_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pipeline_record_versions (
    record_id TEXT NOT NULL REFERENCES record_registry(record_id),
    revision INTEGER NOT NULL CHECK (revision > 0),
    source_kind TEXT NOT NULL CHECK (source_kind IN ('internal', 'external')),
    target TEXT NOT NULL,
    mechanism TEXT NOT NULL,
    indication TEXT NOT NULL,
    phase TEXT NOT NULL CHECK (phase IN ('discovery', 'preclinical', 'phase1', 'phase2', 'phase3', 'nda', 'approved')),
    sponsor TEXT NOT NULL,
    sensitivity TEXT NOT NULL CHECK (sensitivity IN ('open', 'restricted', 'confidential')),
    clinical_features_json TEXT NOT NULL,
    key_experiment_json TEXT,
    first_public_date TEXT,
    note TEXT,
    recorded_by TEXT NOT NULL REFERENCES users(user_id),
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (record_id, revision)
);

CREATE TABLE IF NOT EXISTS timeline_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id TEXT NOT NULL REFERENCES record_registry(record_id),
    event_date TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('publication', 'trial_registry', 'conference', 'press_release', 'regulatory', 'patent')),
    summary TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    recorded_by TEXT NOT NULL REFERENCES users(user_id),
    recorded_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_timeline_record
ON timeline_events(record_id, event_date, event_id);

CREATE TABLE IF NOT EXISTS link_events (
    link_event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL CHECK (action IN ('merge', 'split')),
    group_id TEXT NOT NULL,
    record_ids_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    actor_id TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS credibility_annotations (
    annotation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_type TEXT NOT NULL CHECK (subject_type IN ('record', 'timeline_event')),
    subject_id TEXT NOT NULL,
    level TEXT NOT NULL CHECK (level IN ('high', 'medium', 'low')),
    rationale TEXT NOT NULL,
    actor_id TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_annotations_subject
ON credibility_annotations(subject_type, subject_id, annotation_id);

CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    track_key TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision > 0),
    algorithm_version TEXT NOT NULL,
    input_sha256 TEXT NOT NULL CHECK (length(input_sha256) = 64),
    input_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'decision_locked')),
    created_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE (track_key, revision),
    UNIQUE (track_key, input_sha256)
);

CREATE TABLE IF NOT EXISTS snapshot_decisions (
    decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(snapshot_id),
    decision TEXT NOT NULL CHECK (decision IN ('advance', 'hold', 'decline', 'watch')),
    rationale TEXT NOT NULL,
    decided_by TEXT NOT NULL REFERENCES users(user_id),
    decided_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_snapshot_decisions
ON snapshot_decisions(snapshot_id, decision_id);

CREATE TABLE IF NOT EXISTS audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- 快照内容一经写入不可改写、不可删除；投决引用同样只能追加。
-- 允许的唯一变更是 state 从 open 变为 decision_locked（不涉及内容列）。
CREATE TRIGGER IF NOT EXISTS snapshots_immutable_content
BEFORE UPDATE ON snapshots
WHEN OLD.track_key <> NEW.track_key
  OR OLD.revision <> NEW.revision
  OR OLD.algorithm_version <> NEW.algorithm_version
  OR OLD.input_sha256 <> NEW.input_sha256
  OR OLD.input_json <> NEW.input_json
  OR OLD.result_json <> NEW.result_json
BEGIN
    SELECT RAISE(ABORT, 'snapshot content is immutable');
END;

CREATE TRIGGER IF NOT EXISTS snapshots_no_delete
BEFORE DELETE ON snapshots
BEGIN
    SELECT RAISE(ABORT, 'snapshots cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS snapshot_decisions_no_update
BEFORE UPDATE ON snapshot_decisions
BEGIN
    SELECT RAISE(ABORT, 'decisions cannot be modified');
END;

CREATE TRIGGER IF NOT EXISTS snapshot_decisions_no_delete
BEFORE DELETE ON snapshot_decisions
BEGIN
    SELECT RAISE(ABORT, 'decisions cannot be deleted');
END;
"""

REQUIRED_TABLES = frozenset({
    "schema_meta", "users", "record_registry", "pipeline_record_versions",
    "timeline_events", "link_events", "credibility_annotations",
    "snapshots", "snapshot_decisions", "audit_events",
})


def connect(path: str | Path) -> sqlite3.Connection:
    """打开连接并启用严格的事务与外键设置。"""

    connection = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


@contextlib.contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    """显式事务；异常时保证回滚。"""

    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def initialize(connection: sqlite3.Connection) -> None:
    """初始化基础资料表，重复执行不改变已有数据。"""

    connection.executescript(SCHEMA_SQL)
    with transaction(connection, immediate=True):
        connection.execute(
            "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )


def inspect_schema(connection: sqlite3.Connection) -> dict[str, object]:
    """返回适合机器检查的数据库结构摘要。"""

    table_rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    tables = tuple(row["name"] for row in table_rows)
    version_row = connection.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()
    missing = sorted(REQUIRED_TABLES - set(tables))
    foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
    return {
        "tables": tables,
        "missing_tables": missing,
        "schema_version": None if version_row is None else version_row["value"],
        "foreign_keys": bool(foreign_keys),
    }
