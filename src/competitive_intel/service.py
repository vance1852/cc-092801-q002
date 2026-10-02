"""赛道竞争快照的领域用例。

不变量：
- 资产记录只追加版本，永不就地修改；
- 同源归并关系只追加与“撤销+重建”，拆回不删除历史；
- 快照发布即冻结输入与结论，投决引用后锁定；更新只能产生新版本，
  因此任何已经用于投决的历史判断都能按其引用的版本完整复现。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Mapping, Sequence

from .analysis import CapturedItem, analyze
from .clock import SystemClock, isoformat
from .contracts import AssetRecordDraft, ContractError, CREDIBILITY_LEVELS, normalize_label
from .disclosure import can_see, disclose_evidence_gaps, disclose_item
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .jsonio import canonical_json, content_digest
from .storage import initialize, transaction


ROLE_PERMISSIONS: Mapping[str, frozenset[str]] = {
    "analyst": frozenset(
        {
            "record.read",
            "record.write",
            "credibility.write",
            "merge.write",
            "snapshot.compose",
            "snapshot.read",
        }
    ),
    "reviewer": frozenset(
        {"record.read", "merge.write", "snapshot.read", "snapshot.publish"}
    ),
    "committee": frozenset({"record.read", "snapshot.read", "judgment.write"}),
    "auditor": frozenset({"record.read", "snapshot.read", "audit.read"}),
}

JUDGMENT_DECISIONS: frozenset[str] = frozenset({"proceed", "follow", "watch", "reject"})


class CompetitiveIntelService:
    """在单个 SQLite 连接上提供全部赛道比较操作。"""

    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    def _now(self) -> str:
        return isoformat(self.clock.now())

    # ----- 基础身份与权限 -------------------------------------------------

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT user_id, display_name, role, active FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"用户不存在: {user_id}")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _audit(
        self, entity_type: str, entity_id: str, event_type: str, actor_id: str, payload: Mapping[str, Any]
    ) -> None:
        self.connection.execute(
            "INSERT INTO audit_events(entity_type,entity_id,event_type,actor_id,payload_json,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (entity_type, entity_id, event_type, actor_id, canonical_json(payload), self._now()),
        )

    def create_user(self, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        """创建用户；与 discovery_lab 一致，作为离线引导入口，不做角色权限检查。"""

        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed(f"未知角色: {role}")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO users(user_id,display_name,role,created_at) VALUES(?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, self._now()),
                )
                self._audit("user", user_id.strip(), "user.created", user_id.strip(), {"role": role})
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"用户已存在: {user_id}") from exc
        return {"user_id": user_id.strip(), "role": role}

    # ----- 资产记录版本 ---------------------------------------------------

    def register_record(self, actor_id: str, record_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "record.write")
        draft = self._parse_draft(raw)
        if self.connection.execute(
            "SELECT 1 FROM record_versions WHERE record_id=?", (record_id,)
        ).fetchone():
            raise Conflict(f"资产记录已存在，请改为修订: {record_id}")
        return self._insert_record_version(actor_id, record_id, 1, draft)

    def revise_record(self, actor_id: str, record_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "record.write")
        draft = self._parse_draft(raw)
        latest = self.connection.execute(
            "SELECT max(version) FROM record_versions WHERE record_id=?", (record_id,)
        ).fetchone()[0]
        if latest is None:
            raise NotFound(f"资产记录不存在: {record_id}")
        return self._insert_record_version(actor_id, record_id, latest + 1, draft)

    @staticmethod
    def _parse_draft(raw: Mapping[str, Any]) -> AssetRecordDraft:
        try:
            return AssetRecordDraft.from_dict(raw)
        except ContractError as exc:
            raise ValidationFailed(str(exc)) from exc

    def _insert_record_version(
        self, actor_id: str, record_id: str, version: int, draft: AssetRecordDraft
    ) -> dict[str, Any]:
        content = draft.to_content()
        digest = content_digest([content])
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO record_versions(record_id,version,source,display_name,organization,origin,"
                    "sensitivity,target,mechanism,target_key,mechanism_key,modalities_json,indications_json,"
                    "clinical_attributes_json,stage,evidence_confidence,experiments_json,timeline_json,"
                    "evidence_refs_json,notes,content_sha256,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        record_id,
                        version,
                        draft.source,
                        draft.display_name,
                        draft.organization,
                        draft.origin,
                        draft.sensitivity,
                        draft.target,
                        draft.mechanism,
                        normalize_label(draft.target),
                        normalize_label(draft.mechanism),
                        canonical_json(list(draft.modalities)),
                        canonical_json(list(draft.indications)),
                        canonical_json(
                            {key: list(value) for key, value in sorted(draft.clinical_attributes.items())}
                        ),
                        draft.stage,
                        draft.evidence_tier,
                        canonical_json(content["experiments"]),
                        canonical_json(content["timeline"]),
                        canonical_json(list(draft.evidence_refs)),
                        draft.notes,
                        digest,
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit(
                    "record",
                    record_id,
                    "record.version_created",
                    actor_id,
                    {"version": version, "content_sha256": digest},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("记录版本冲突或内容摘要重复") from exc
        return {"record_id": record_id, "version": version, "content_sha256": digest}

    def get_record(self, actor_id: str, record_id: str, version: int | None = None) -> dict[str, Any]:
        role = self._require(actor_id, "record.read")["role"]
        row = self._record_row(record_id, version)
        annotations = self.connection.execute(
            "SELECT annotation_id,record_version,scope,level,rationale,evidence_refs_json,annotated_by,annotated_at "
            "FROM confidence_annotations WHERE record_id=? ORDER BY annotation_id",
            (record_id,),
        ).fetchall()
        content = self._row_content(row)
        membership = self.connection.execute(
            "SELECT membership_id,asset_id,status FROM asset_memberships WHERE record_id=? ORDER BY membership_id DESC",
            (record_id,),
        ).fetchone()
        view = disclose_item(
            role,
            content
            | {
                "record_id": record_id,
                "version": row["version"],
                "asset_id": membership["asset_id"] if membership and membership["status"] == "active" else None,
                "membership_id": membership["membership_id"] if membership and membership["status"] == "active" else None,
                "credibility": self._latest_annotation_view(record_id, row["version"]),
                "content_sha256": row["content_sha256"],
            },
        )
        annotation_views = []
        for item in annotations:
            annotation = {
                "annotation_id": item["annotation_id"],
                "record_version": item["record_version"],
                "scope": item["scope"],
                "level": item["level"],
                "rationale": item["rationale"],
                "evidence_refs": json.loads(item["evidence_refs_json"]),
                "annotated_by": item["annotated_by"],
                "annotated_at": item["annotated_at"],
            }
            annotated_row = self._record_row(record_id, item["record_version"])
            if not can_see(role, "experiment_detail", annotated_row["sensitivity"]):
                annotation["rationale"] = "***REDACTED***"
                annotation["evidence_refs"] = []
            annotation_views.append(annotation)
        return {
            "record_id": record_id,
            "latest_version": row["version"],
            "content": view,
            "annotations": annotation_views,
        }

    def _record_row(self, record_id: str, version: int | None = None) -> sqlite3.Row:
        if version is None:
            row = self.connection.execute(
                "SELECT * FROM record_versions WHERE record_id=? ORDER BY version DESC LIMIT 1",
                (record_id,),
            ).fetchone()
        else:
            row = self.connection.execute(
                "SELECT * FROM record_versions WHERE record_id=? AND version=?", (record_id, version)
            ).fetchone()
        if row is None:
            raise NotFound(f"资产记录版本不存在: {record_id}@{version if version is not None else 'latest'}")
        return row

    @staticmethod
    def _row_content(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "source": row["source"],
            "display_name": row["display_name"],
            "organization": row["organization"],
            "origin": row["origin"],
            "sensitivity": row["sensitivity"],
            "target": row["target"],
            "mechanism": row["mechanism"],
            "modalities": json.loads(row["modalities_json"]),
            "indications": json.loads(row["indications_json"]),
            "clinical_attributes": json.loads(row["clinical_attributes_json"]),
            "stage": row["stage"],
            "evidence_tier": row["evidence_confidence"],
            "experiments": json.loads(row["experiments_json"]),
            "timeline": json.loads(row["timeline_json"]),
            "evidence_refs": json.loads(row["evidence_refs_json"]),
            "notes": row["notes"],
        }

    # ----- 证据可信度标注 -------------------------------------------------

    def annotate_credibility(
        self,
        actor_id: str,
        record_id: str,
        record_version: int,
        level: str,
        rationale: str,
        evidence_refs: Sequence[str],
        scope: str = "track_review",
    ) -> dict[str, Any]:
        self._require(actor_id, "credibility.write")
        if level not in CREDIBILITY_LEVELS:
            raise ValidationFailed(f"可信度必须是 {sorted(CREDIBILITY_LEVELS)} 之一")
        if not rationale.strip():
            raise ValidationFailed("可信度判定理由不能为空")
        refs = [item.strip() for item in evidence_refs if item.strip()]
        if not refs:
            raise ValidationFailed("可信度判定必须引用至少一条证据")
        self._record_row(record_id, record_version)
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO confidence_annotations(record_id,record_version,scope,level,rationale,"
                "evidence_refs_json,annotated_by,annotated_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    record_id,
                    record_version,
                    scope.strip(),
                    level,
                    rationale.strip(),
                    canonical_json(refs),
                    actor_id,
                    self._now(),
                ),
            )
            annotation_id = cursor.lastrowid
            self._audit(
                "credibility",
                str(annotation_id),
                "credibility.annotated",
                actor_id,
                {"record_id": record_id, "record_version": record_version, "level": level},
            )
        return {
            "annotation_id": annotation_id,
            "record_id": record_id,
            "record_version": record_version,
            "level": level,
            "supersedes_annotation": self._previous_annotation_id(record_id, record_version, annotation_id),
        }

    def _previous_annotation_id(self, record_id: str, record_version: int, current_id: int) -> int | None:
        row = self.connection.execute(
            "SELECT max(annotation_id) FROM confidence_annotations "
            "WHERE record_id=? AND record_version=? AND annotation_id<>?",
            (record_id, record_version, current_id),
        ).fetchone()
        return row[0]

    def _latest_annotation(self, record_id: str, record_version: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM confidence_annotations WHERE record_id=? AND record_version=? "
            "ORDER BY annotation_id DESC LIMIT 1",
            (record_id, record_version),
        ).fetchone()

    def _latest_annotation_view(self, record_id: str, record_version: int) -> dict[str, Any] | None:
        row = self._latest_annotation(record_id, record_version)
        if row is None:
            return None
        return {
            "annotation_id": row["annotation_id"],
            "level": row["level"],
            "rationale": row["rationale"],
            "evidence_refs": json.loads(row["evidence_refs_json"]),
            "annotated_by": row["annotated_by"],
            "annotated_at": row["annotated_at"],
        }

    # ----- 同源归并与拆回 -------------------------------------------------

    def create_asset(
        self, actor_id: str, asset_id: str, record_ids: Sequence[str], reason: str
    ) -> dict[str, Any]:
        """把若干尚无归属的记录归并为一个同源资产。"""

        self._require(actor_id, "merge.write")
        self._validate_merge_inputs(asset_id, record_ids, reason, allow_existing_asset=False)
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "INSERT INTO canonical_assets(asset_id,rationale,created_by,created_at) VALUES(?,?,?,?)",
                (asset_id, reason.strip(), actor_id, self._now()),
            )
            membership_ids = self._attach_records(actor_id, asset_id, record_ids, reason)
            self._audit(
                "asset", asset_id, "asset.created", actor_id,
                {"record_ids": list(record_ids), "membership_ids": membership_ids},
            )
        return self.get_asset(actor_id, asset_id)

    def merge_into_asset(
        self, actor_id: str, asset_id: str, record_ids: Sequence[str], reason: str
    ) -> dict[str, Any]:
        """把更多同源记录并入既有资产（被错误拆出的记录可重新归并）。"""

        self._require(actor_id, "merge.write")
        self._validate_merge_inputs(asset_id, record_ids, reason, allow_existing_asset=True)
        with transaction(self.connection, immediate=True):
            membership_ids = self._attach_records(actor_id, asset_id, record_ids, reason)
            self._audit(
                "asset", asset_id, "asset.records_merged", actor_id,
                {"record_ids": list(record_ids), "membership_ids": membership_ids},
            )
        return self.get_asset(actor_id, asset_id)

    def _validate_merge_inputs(
        self, asset_id: str, record_ids: Sequence[str], reason: str, *, allow_existing_asset: bool
    ) -> None:
        if not asset_id.strip() or not reason.strip():
            raise ValidationFailed("资产编号与归并理由不能为空")
        ids = [item.strip() for item in record_ids if item.strip()]
        if not ids or len(set(ids)) != len(ids):
            raise ValidationFailed("归并记录列表不能为空且不能重复")
        asset_exists = self.connection.execute(
            "SELECT 1 FROM canonical_assets WHERE asset_id=?", (asset_id,)
        ).fetchone()
        if allow_existing_asset and asset_exists is None:
            raise NotFound(f"同源资产不存在: {asset_id}")
        if not allow_existing_asset and asset_exists is not None:
            raise Conflict(f"同源资产已存在: {asset_id}")
        for record_id in ids:
            row = self.connection.execute(
                "SELECT asset_id,status FROM asset_memberships WHERE record_id=? AND status='active'",
                (record_id,),
            ).fetchone()
            if row is not None and row["asset_id"] == asset_id and allow_existing_asset:
                raise InvalidState(f"记录 {record_id} 已归属同源资产 {asset_id}")
            if row is not None:
                raise InvalidState(f"记录 {record_id} 已归属其他同源资产，需先拆回")
            if self.connection.execute(
                "SELECT 1 FROM record_versions WHERE record_id=?", (record_id,)
            ).fetchone() is None:
                raise NotFound(f"资产记录不存在: {record_id}")

    def _attach_records(
        self, actor_id: str, asset_id: str, record_ids: Sequence[str], reason: str
    ) -> list[int]:
        membership_ids: list[int] = []
        for record_id in dict.fromkeys(item.strip() for item in record_ids if item.strip()):
            previous = self.connection.execute(
                "SELECT membership_id FROM asset_memberships WHERE record_id=? ORDER BY membership_id DESC LIMIT 1",
                (record_id,),
            ).fetchone()
            cursor = self.connection.execute(
                "INSERT INTO asset_memberships(asset_id,record_id,previous_membership_id,status,reason,"
                "decided_by,decided_at) VALUES(?,?,?,'active',?,?,?)",
                (asset_id, record_id, previous[0] if previous else None, reason.strip(), actor_id, self._now()),
            )
            membership_ids.append(cursor.lastrowid)
        return membership_ids

    def unmerge_record(self, actor_id: str, record_id: str, reason: str) -> dict[str, Any]:
        """拆回误关联：关闭当前有效归属，记录恢复为独立条目；历史关系保留。"""

        self._require(actor_id, "merge.write")
        if not reason.strip():
            raise ValidationFailed("拆回理由不能为空")
        with transaction(self.connection, immediate=True):
            row = self.connection.execute(
                "SELECT membership_id,asset_id FROM asset_memberships WHERE record_id=? AND status='active'",
                (record_id,),
            ).fetchone()
            if row is None:
                raise InvalidState(f"记录当前没有有效归属: {record_id}")
            cursor = self.connection.execute(
                "UPDATE asset_memberships SET status='revoked',revoked_by=?,revoked_at=?,revoke_reason=? "
                "WHERE membership_id=? AND status='active'",
                (actor_id, self._now(), reason.strip(), row["membership_id"]),
            )
            if cursor.rowcount != 1:
                raise InvalidState("归属关系已被其他操作改变")
            self._audit(
                "asset", row["asset_id"], "asset.record_unmerged", actor_id,
                {"record_id": record_id, "membership_id": row["membership_id"], "reason": reason.strip()},
            )
        return {"record_id": record_id, "membership_id": row["membership_id"], "status": "revoked"}

    def get_asset(self, actor_id: str, asset_id: str) -> dict[str, Any]:
        self._require(actor_id, "record.read")
        asset = self.connection.execute(
            "SELECT * FROM canonical_assets WHERE asset_id=?", (asset_id,)
        ).fetchone()
        if asset is None:
            raise NotFound(f"同源资产不存在: {asset_id}")
        memberships = self.connection.execute(
            "SELECT membership_id,record_id,previous_membership_id,status,reason,decided_by,decided_at,"
            "revoked_by,revoked_at,revoke_reason FROM asset_memberships WHERE asset_id=? ORDER BY membership_id",
            (asset_id,),
        ).fetchall()
        return {
            "asset_id": asset_id,
            "rationale": asset["rationale"],
            "created_by": asset["created_by"],
            "created_at": asset["created_at"],
            "active_records": [row["record_id"] for row in memberships if row["status"] == "active"],
            "membership_history": [dict(row) for row in memberships],
        }

    # ----- 竞争快照 -------------------------------------------------------

    def create_snapshot(
        self,
        actor_id: str,
        snapshot_id: str,
        scope: Mapping[str, Any],
        record_ids: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """创建快照新版本：冻结当前记录版本/归属/可信度并计算分析结论。"""

        self._require(actor_id, "snapshot.compose")
        normalized_scope = self._normalize_scope(scope)
        selected = self._select_snapshot_records(normalized_scope, record_ids)
        if not selected:
            raise ValidationFailed("快照范围内没有任何资产记录")

        previous = self.connection.execute(
            "SELECT max(version) AS version FROM snapshots WHERE snapshot_id=?", (snapshot_id,)
        ).fetchone()
        version = (previous["version"] or 0) + 1

        captured: list[CapturedItem] = []
        manifests: list[dict[str, Any]] = []
        membership_ids: list[int] = []
        for record_id, record_row in selected:
            membership = self.connection.execute(
                "SELECT membership_id,asset_id FROM asset_memberships WHERE record_id=? AND status='active'",
                (record_id,),
            ).fetchone()
            annotation = self._latest_annotation(record_id, record_row["version"])
            credibility = None
            if annotation is not None:
                credibility = {
                    "annotation_id": annotation["annotation_id"],
                    "level": annotation["level"],
                    "rationale": annotation["rationale"],
                    "evidence_refs": json.loads(annotation["evidence_refs_json"]),
                }
            content = self._row_content(record_row)
            captured.append(
                CapturedItem(
                    record_id=record_id,
                    version=record_row["version"],
                    content=content,
                    membership_id=membership["membership_id"] if membership else None,
                    asset_id=membership["asset_id"] if membership else None,
                    credibility=credibility,
                    content_sha256=record_row["content_sha256"],
                )
            )
            if membership is not None:
                membership_ids.append(membership["membership_id"])
            manifests.append(
                {
                    "record_id": record_id,
                    "version": record_row["version"],
                    "content_sha256": record_row["content_sha256"],
                    "membership_id": membership["membership_id"] if membership else None,
                    "frozen_asset_id": membership["asset_id"] if membership else None,
                    "credibility": credibility,
                }
            )

        result = analyze(normalized_scope, captured, membership_ids=membership_ids)
        scope_json = canonical_json(normalized_scope)
        scope_fingerprint = content_digest([normalized_scope])
        input_digest = content_digest([normalized_scope, *manifests])

        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "INSERT INTO snapshots(snapshot_id,version,scope_json,scope_fingerprint,input_sha256,"
                "algorithm_version,result_json,status,supersedes_version,created_by,created_at) "
                "VALUES(?,?,?,?,?,?,?,'draft',?,?,?)",
                (
                    snapshot_id,
                    version,
                    scope_json,
                    scope_fingerprint,
                    input_digest,
                    result["algorithm_version"],
                    canonical_json(result),
                    None if version == 1 else version - 1,
                    actor_id,
                    self._now(),
                ),
            )
            for manifest, item in zip(manifests, captured):
                self.connection.execute(
                    "INSERT INTO snapshot_items(snapshot_id,snapshot_version,record_id,record_version,"
                    "membership_id,frozen_asset_id,confidence_annotation_id,record_content_sha256) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (
                        snapshot_id,
                        version,
                        item.record_id,
                        item.version,
                        item.membership_id,
                        item.asset_id,
                        None if item.credibility is None else item.credibility["annotation_id"],
                        item.content_sha256,
                    ),
                )
            self._audit(
                "snapshot",
                snapshot_id,
                "snapshot.version_created",
                actor_id,
                {"version": version, "input_sha256": input_digest, "records": len(manifests)},
            )
        return self.get_snapshot(actor_id, snapshot_id, version)

    @staticmethod
    def _normalize_scope(raw: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(raw, Mapping):
            raise ValidationFailed("快照范围必须是对象")
        name = raw.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValidationFailed("快照范围 name 不能为空")
        as_of = raw.get("as_of")
        if not isinstance(as_of, str) or not as_of.strip():
            raise ValidationFailed("快照范围 as_of 必须是 YYYY-MM-DD")
        try:
            from datetime import datetime as _datetime

            _datetime.strptime(as_of.strip()[:10], "%Y-%m-%d")
        except ValueError as exc:
            raise ValidationFailed("快照范围 as_of 必须是 YYYY-MM-DD") from exc
        scope: dict[str, Any] = {"name": name.strip(), "as_of": as_of.strip()[:10]}
        for key in ("target", "mechanism"):
            value = raw.get(key)
            if value is not None:
                if not isinstance(value, str) or not value.strip():
                    raise ValidationFailed(f"快照范围 {key} 必须是非空字符串")
                scope[key] = value.strip()
                scope[f"{key}_key"] = normalize_label(value.strip())
        universe = raw.get("indication_universe")
        if universe is not None:
            if not isinstance(universe, Sequence) or isinstance(universe, str):
                raise ValidationFailed("快照范围 indication_universe 必须是字符串数组")
            parsed = [str(item).strip() for item in universe if str(item).strip()]
            if not parsed:
                raise ValidationFailed("快照范围 indication_universe 不能为空")
            scope["indication_universe"] = parsed
        return scope

    def _select_snapshot_records(
        self, scope: Mapping[str, Any], record_ids: Sequence[str] | None
    ) -> list[tuple[str, sqlite3.Row]]:
        if record_ids is not None:
            ids = [item.strip() for item in record_ids if item.strip()]
            if not ids:
                raise ValidationFailed("快照记录列表不能为空")
            if len(set(ids)) != len(ids):
                raise ValidationFailed("快照记录不能重复")
            return [(record_id, self._record_row(record_id)) for record_id in ids]
        query = "SELECT * FROM record_versions r WHERE version=(SELECT max(version) FROM record_versions WHERE record_id=r.record_id)"
        conditions: list[str] = []
        args: list[Any] = []
        if "target_key" in scope:
            conditions.append("target_key=?")
            args.append(scope["target_key"])
        if "mechanism_key" in scope:
            conditions.append("mechanism_key=?")
            args.append(scope["mechanism_key"])
        if conditions:
            query += " AND " + " AND ".join(conditions)
        query += " ORDER BY record_id"
        rows = self.connection.execute(query, args).fetchall()
        return [(row["record_id"], row) for row in rows]

    def publish_snapshot(self, actor_id: str, snapshot_id: str, version: int) -> dict[str, Any]:
        self._require(actor_id, "snapshot.publish")
        with transaction(self.connection, immediate=True):
            row = self.connection.execute(
                "SELECT status FROM snapshots WHERE snapshot_id=? AND version=?",
                (snapshot_id, version),
            ).fetchone()
            if row is None:
                raise NotFound(f"快照版本不存在: {snapshot_id}@v{version}")
            if row["status"] != "draft":
                raise InvalidState(f"快照当前状态为 {row['status']}，不能发布")
            self.connection.execute(
                "UPDATE snapshots SET status='published',published_by=?,published_at=? "
                "WHERE snapshot_id=? AND version=? AND status='draft'",
                (actor_id, self._now(), snapshot_id, version),
            )
            self._audit(
                "snapshot", snapshot_id, "snapshot.published", actor_id, {"version": version}
            )
        return self.get_snapshot(actor_id, snapshot_id, version)

    def record_judgment(
        self,
        actor_id: str,
        judgment_id: str,
        snapshot_id: str,
        version: int,
        decision: str,
        rationale: str,
    ) -> dict[str, Any]:
        """投决引用已发布快照；写入即锁定该版本，此后任何更新只能另出新版。"""

        self._require(actor_id, "judgment.write")
        if decision not in JUDGMENT_DECISIONS:
            raise ValidationFailed(f"投决结论必须是 {sorted(JUDGMENT_DECISIONS)} 之一")
        if not rationale.strip():
            raise ValidationFailed("投决理由不能为空")
        snapshot = self.connection.execute(
            "SELECT * FROM snapshots WHERE snapshot_id=? AND version=?", (snapshot_id, version)
        ).fetchone()
        if snapshot is None:
            raise NotFound(f"快照版本不存在: {snapshot_id}@v{version}")
        if snapshot["status"] != "published":
            raise InvalidState("只有已发布的快照可以用于投决")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO investment_judgments(judgment_id,snapshot_id,snapshot_version,"
                    "snapshot_input_sha256,snapshot_scope_fingerprint,decision,rationale,decided_by,decided_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        judgment_id,
                        snapshot_id,
                        version,
                        snapshot["input_sha256"],
                        snapshot["scope_fingerprint"],
                        decision,
                        rationale.strip(),
                        actor_id,
                        self._now(),
                    ),
                )
                cursor = self.connection.execute(
                    "UPDATE snapshots SET status='locked' WHERE snapshot_id=? AND version=? AND status='published'",
                    (snapshot_id, version),
                )
                if cursor.rowcount != 1:
                    raise InvalidState("快照状态已变化，投决未能锁定")
                self._audit(
                    "judgment",
                    judgment_id,
                    "judgment.recorded",
                    actor_id,
                    {"snapshot_id": snapshot_id, "snapshot_version": version, "decision": decision},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("该快照版本已经存在投决判断或判断编号冲突") from exc
        return {
            "judgment_id": judgment_id,
            "snapshot_id": snapshot_id,
            "snapshot_version": version,
            "decision": decision,
            "locked_input_sha256": snapshot["input_sha256"],
        }

    def get_snapshot(
        self, actor_id: str, snapshot_id: str, version: int | None = None
    ) -> dict[str, Any]:
        role = self._require(actor_id, "snapshot.read")["role"]
        if version is None:
            row = self.connection.execute(
                "SELECT * FROM snapshots WHERE snapshot_id=? ORDER BY version DESC LIMIT 1",
                (snapshot_id,),
            ).fetchone()
        else:
            row = self.connection.execute(
                "SELECT * FROM snapshots WHERE snapshot_id=? AND version=?", (snapshot_id, version)
            ).fetchone()
        if row is None:
            raise NotFound(f"快照不存在: {snapshot_id}@{version if version is not None else 'latest'}")

        items = self.connection.execute(
            "SELECT * FROM snapshot_items "
            "WHERE snapshot_id=? AND snapshot_version=? ORDER BY record_id",
            (snapshot_id, row["version"]),
        ).fetchall()
        item_views: list[dict[str, Any]] = []
        sensitivity_by_ref: dict[str, str] = {}
        frozen_integrity: list[dict[str, Any]] = []
        for item_row in items:
            record_row = self.connection.execute(
                "SELECT * FROM record_versions WHERE record_id=? AND version=?",
                (item_row["record_id"], item_row["record_version"]),
            ).fetchone()
            content = self._row_content(record_row)
            ref = f"{item_row['record_id']}@v{item_row['record_version']}"
            sensitivity_by_ref[ref] = content["sensitivity"]
            annotation = None
            if item_row["confidence_annotation_id"] is not None:
                annotation_row = self.connection.execute(
                    "SELECT * FROM confidence_annotations WHERE annotation_id=?",
                    (item_row["confidence_annotation_id"],),
                ).fetchone()
                annotation = {
                    "annotation_id": annotation_row["annotation_id"],
                    "level": annotation_row["level"],
                    "rationale": annotation_row["rationale"],
                    "evidence_refs": json.loads(annotation_row["evidence_refs_json"]),
                    "annotated_by": annotation_row["annotated_by"],
                    "annotated_at": annotation_row["annotated_at"],
                }
            view = disclose_item(
                role,
                content
                | {
                    "record_id": item_row["record_id"],
                    "version": item_row["record_version"],
                    "asset_id": item_row["frozen_asset_id"],
                    "membership_id": item_row["membership_id"],
                    "credibility": annotation,
                    "content_sha256": item_row["record_content_sha256"],
                },
            )
            item_views.append(view)
            frozen_integrity.append(
                {
                    "record_ref": ref,
                    "frozen_sha256": item_row["record_content_sha256"],
                    "frozen_asset_id": item_row["frozen_asset_id"],
                    "membership_id": item_row["membership_id"],
                }
            )

        result = json.loads(row["result_json"])
        result = dict(result)
        result["evidence_gaps"] = disclose_evidence_gaps(
            role, result["evidence_gaps"], sensitivity_by_ref
        )

        judgment = self.connection.execute(
            "SELECT judgment_id,decision,rationale,decided_by,decided_at,snapshot_input_sha256 "
            "FROM investment_judgments WHERE snapshot_id=? AND snapshot_version=?",
            (snapshot_id, row["version"]),
        ).fetchone()
        judgment_view: dict[str, Any] | None = None
        if judgment is not None:
            judgment_view = dict(judgment)
            # 投决结论对所有可读快照的角色可见；理由可能复述敏感实验细节，
            # 对看不到实验细节的 analyst 角色遮蔽。
            if role == "analyst":
                judgment_view["rationale"] = "***REDACTED***"

        return {
            "snapshot_id": snapshot_id,
            "version": row["version"],
            "status": row["status"],
            "supersedes_version": row["supersedes_version"],
            "algorithm_version": row["algorithm_version"],
            "scope": json.loads(row["scope_json"]),
            "scope_fingerprint": row["scope_fingerprint"],
            "input_sha256": row["input_sha256"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "published_by": row["published_by"],
            "published_at": row["published_at"],
            "result": result,
            "items": item_views,
            "frozen_integrity": frozen_integrity,
            "judgment": judgment_view,
        }

    def list_snapshots(self, actor_id: str) -> list[dict[str, Any]]:
        self._require(actor_id, "snapshot.read")
        rows = self.connection.execute(
            "SELECT snapshot_id,version,status,supersedes_version,input_sha256,created_at,published_at "
            "FROM snapshots ORDER BY snapshot_id,version"
        ).fetchall()
        return [dict(row) for row in rows]

    def audit_events(
        self, actor_id: str, entity_type: str | None = None, entity_id: str | None = None
    ) -> list[dict[str, Any]]:
        self._require(actor_id, "audit.read")
        query = (
            "SELECT event_id,entity_type,entity_id,event_type,actor_id,payload_json,created_at "
            "FROM audit_events"
        )
        clauses: list[str] = []
        args: list[Any] = []
        if entity_type is not None:
            clauses.append("entity_type=?")
            args.append(entity_type)
        if entity_id is not None:
            clauses.append("entity_id=?")
            args.append(entity_id)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY event_id"
        return [
            dict(row) | {"payload": json.loads(row["payload_json"])}
            for row in self.connection.execute(query, args).fetchall()
        ]
