"""赛道竞争快照服务的 SQLite 模式与事务辅助。

所有业务事实均为追加式（append-only）：资产记录修订、可信度标注、同源归并
关系和竞争快照都只插入新版本行，从不更新或删除，从而保证任何已经用于投决的
历史判断都可以按其引用的具体版本完整复现。
"""

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
    role TEXT NOT NULL CHECK (role IN ('analyst', 'reviewer', 'committee', 'auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL
);

-- 资产记录的全部历史版本；每次修订追加一行，旧版本永不修改。
CREATE TABLE IF NOT EXISTS record_versions (
    record_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    source TEXT NOT NULL,
    display_name TEXT NOT NULL,
    organization TEXT NOT NULL,
    origin TEXT NOT NULL CHECK (origin IN ('internal', 'external')),
    sensitivity TEXT NOT NULL CHECK (sensitivity IN ('public', 'restricted', 'sensitive')),
    target TEXT NOT NULL,
    mechanism TEXT NOT NULL,
    target_key TEXT NOT NULL,
    mechanism_key TEXT NOT NULL,
    modalities_json TEXT NOT NULL,
    indications_json TEXT NOT NULL,
    clinical_attributes_json TEXT NOT NULL,
    stage TEXT NOT NULL,
    evidence_confidence TEXT NOT NULL,
    experiments_json TEXT NOT NULL,
    timeline_json TEXT NOT NULL,
    evidence_refs_json TEXT NOT NULL,
    notes TEXT,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    created_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL,
    PRIMARY KEY (record_id, version)
);

-- 证据可信度标注：同一记录版本同一作用域以最新一条为准，历史全部保留。
CREATE TABLE IF NOT EXISTS confidence_annotations (
    annotation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id TEXT NOT NULL,
    record_version INTEGER NOT NULL,
    scope TEXT NOT NULL,
    level TEXT NOT NULL CHECK (level IN ('high', 'medium', 'low')),
    rationale TEXT NOT NULL,
    evidence_refs_json TEXT NOT NULL,
    annotated_by TEXT NOT NULL REFERENCES users(user_id),
    annotated_at TEXT NOT NULL,
    FOREIGN KEY (record_id, record_version) REFERENCES record_versions(record_id, version)
);

CREATE TABLE IF NOT EXISTS canonical_assets (
    asset_id TEXT PRIMARY KEY,
    rationale TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL
);

-- 同源归并关系：合并插入新行，拆回只把旧行置为 revoked 并新插入关系，
-- previous_membership_id 串起完整归并谱系。
CREATE TABLE IF NOT EXISTS asset_memberships (
    membership_id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id TEXT NOT NULL REFERENCES canonical_assets(asset_id),
    record_id TEXT NOT NULL,
    previous_membership_id INTEGER REFERENCES asset_memberships(membership_id),
    status TEXT NOT NULL CHECK (status IN ('active', 'revoked')),
    reason TEXT NOT NULL,
    decided_by TEXT NOT NULL REFERENCES users(user_id),
    decided_at TEXT NOT NULL,
    revoked_by TEXT REFERENCES users(user_id),
    revoked_at TEXT,
    revoke_reason TEXT
);

-- 一条记录同一时间只能有效归属一个同源资产。
CREATE UNIQUE INDEX IF NOT EXISTS one_active_membership_per_record
ON asset_memberships(record_id)
WHERE status = 'active';

-- 竞争快照：同一 snapshot_id 下版本递增，发布后冻结，投决引用后置为 locked。
CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    scope_json TEXT NOT NULL,
    scope_fingerprint TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    algorithm_version TEXT NOT NULL,
    result_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('draft', 'published', 'locked')),
    supersedes_version INTEGER,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    published_by TEXT,
    published_at TEXT,
    PRIMARY KEY (snapshot_id, version)
);

-- 快照片逐条钉住当时的记录版本、归并关系和可信度标注。
-- frozen_* 是捕获当时归属真相的副本：后续即使记录被拆回，已发布快照不变。
CREATE TABLE IF NOT EXISTS snapshot_items (
    snapshot_id TEXT NOT NULL,
    snapshot_version INTEGER NOT NULL,
    record_id TEXT NOT NULL,
    record_version INTEGER NOT NULL,
    membership_id INTEGER,
    frozen_asset_id TEXT,
    confidence_annotation_id INTEGER,
    record_content_sha256 TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, snapshot_version, record_id),
    FOREIGN KEY (snapshot_id, snapshot_version) REFERENCES snapshots(snapshot_id, version),
    FOREIGN KEY (record_id, record_version) REFERENCES record_versions(record_id, version),
    FOREIGN KEY (membership_id) REFERENCES asset_memberships(membership_id),
    FOREIGN KEY (confidence_annotation_id) REFERENCES confidence_annotations(annotation_id)
);

-- 投决判断：一旦写入，引用的快照版本即被锁定，任何更新只能产生新版本。
CREATE TABLE IF NOT EXISTS investment_judgments (
    judgment_id TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL,
    snapshot_version INTEGER NOT NULL,
    snapshot_input_sha256 TEXT NOT NULL,
    snapshot_scope_fingerprint TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('proceed', 'follow', 'watch', 'reject')),
    rationale TEXT NOT NULL,
    decided_by TEXT NOT NULL REFERENCES users(user_id),
    decided_at TEXT NOT NULL,
    UNIQUE (snapshot_id, snapshot_version),
    FOREIGN KEY (snapshot_id, snapshot_version) REFERENCES snapshots(snapshot_id, version)
);

CREATE TABLE IF NOT EXISTS audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

REQUIRED_TABLES = frozenset(
    {
        "schema_meta", "users", "record_versions", "confidence_annotations",
        "canonical_assets", "asset_memberships", "snapshots", "snapshot_items",
        "investment_judgments", "audit_events",
    }
)


def connect(path: str | Path) -> sqlite3.Connection:
    """打开连接并启用严格的事务与外键设置。"""

    connection = sqlite3.connect(str(path), isolation_level=None)
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
    """初始化数据库结构，重复执行不改变已有数据。"""

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
