"""赛道比较服务的领域用例。

竞争快照是不可变的事实记录：每次比较把当时的输入清单、算法版本和结果
一起落库；投决会引用某个具体快照版本后，该版本永远保持原样，后续任何
记录修订、合并、拆分或可信度标注只会产生新的快照修订。
"""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import uuid
from datetime import timezone
from typing import Any, Mapping

from .analysis import ALGORITHM_VERSION, compare, derive_groups
from .clock import SystemClock, isoformat
from .contracts import (
    CredibilityAnnotationInput,
    DecisionInput,
    MergeInput,
    PipelineRecord,
    TimelineEventInput,
    TrackSelector,
    ValidationError,
)
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .jsonio import canonical_json, content_digest
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "analyst": {"record.write", "link.write", "annotation.write", "snapshot.create", "track.read"},
    "reviewer": {"track.read", "sensitive.read", "decision.write"},
    "viewer": {"track.read"},
    "auditor": {"track.read", "sensitive.read", "audit.read"},
}

MASKED_SPONSOR = "已脱敏"


class TrackIntelService:
    """在单个 SQLite 连接上提供全部赛道比较操作。"""

    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    def _now(self) -> str:
        return isoformat(self.clock.now())

    def _today(self) -> str:
        return self.clock.now().astimezone(timezone.utc).date().isoformat()

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

    def _can_sensitive(self, user: sqlite3.Row) -> bool:
        return "sensitive.read" in ROLE_PERMISSIONS[user["role"]]

    def _audit(
        self, entity_type: str, entity_id: str, event_type: str, actor_id: str, payload: Mapping[str, Any]
    ) -> None:
        self.connection.execute(
            "INSERT INTO audit_events(entity_type,entity_id,event_type,actor_id,payload_json,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (entity_type, entity_id, event_type, actor_id, canonical_json(payload), self._now()),
        )

    def create_user(self, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed(f"未知角色: {role}")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO users(user_id,display_name,role) VALUES(?,?,?)",
                    (user_id.strip(), display_name.strip(), role),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"用户已存在: {user_id}") from exc
        return {"user_id": user_id.strip(), "role": role}

    # ------------------------------------------------------------------
    # 管线记录
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_record(raw: Mapping[str, Any]) -> PipelineRecord:
        try:
            return PipelineRecord.from_dict(raw)
        except ValidationError as exc:
            raise ValidationFailed(str(exc)) from exc

    def _insert_record_version(self, record: PipelineRecord, revision: int, actor_id: str) -> None:
        self.connection.execute(
            "INSERT INTO pipeline_record_versions(record_id,revision,source_kind,target,mechanism,indication,"
            "phase,sponsor,sensitivity,clinical_features_json,key_experiment_json,first_public_date,note,"
            "recorded_by,recorded_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                record.record_id,
                revision,
                record.source_kind,
                record.target,
                record.mechanism,
                record.indication,
                record.phase,
                record.sponsor,
                record.sensitivity,
                canonical_json(list(record.clinical_features)),
                None if record.key_experiment is None else canonical_json(record.key_experiment.as_dict()),
                record.first_public_date,
                record.note,
                actor_id,
                self._now(),
            ),
        )

    def register_record(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "record.write")
        record = self._parse_record(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO record_registry(record_id,registered_by,registered_at) VALUES(?,?,?)",
                    (record.record_id, actor_id, self._now()),
                )
                self._insert_record_version(record, 1, actor_id)
                self._audit("record", record.record_id, "record.registered", actor_id, {"revision": 1})
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"管线记录已存在: {record.record_id}") from exc
        return self.get_record(actor_id, record.record_id)

    def revise_record(self, actor_id: str, record_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "record.write")
        record = self._parse_record(raw)
        if record.record_id != record_id:
            raise ValidationFailed("路径与内容中的 record_id 不一致")
        registry = self.connection.execute(
            "SELECT record_id FROM record_registry WHERE record_id=?", (record_id,)
        ).fetchone()
        if registry is None:
            raise NotFound(f"管线记录不存在: {record_id}")
        latest = self.connection.execute(
            "SELECT MAX(revision) FROM pipeline_record_versions WHERE record_id=?", (record_id,)
        ).fetchone()[0]
        with transaction(self.connection, immediate=True):
            self._insert_record_version(record, latest + 1, actor_id)
            self._audit("record", record_id, "record.revised", actor_id, {"revision": latest + 1})
        return self.get_record(actor_id, record_id)

    def _latest_record_row(self, record_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM pipeline_record_versions WHERE record_id=? ORDER BY revision DESC LIMIT 1",
            (record_id,),
        ).fetchone()
        if row is None:
            raise NotFound(f"管线记录不存在: {record_id}")
        return row

    @staticmethod
    def _record_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "record_id": row["record_id"],
            "revision": row["revision"],
            "source_kind": row["source_kind"],
            "target": row["target"],
            "mechanism": row["mechanism"],
            "indication": row["indication"],
            "phase": row["phase"],
            "sponsor": row["sponsor"],
            "sensitivity": row["sensitivity"],
            "clinical_features": json.loads(row["clinical_features_json"]),
            "key_experiment": None if row["key_experiment_json"] is None else json.loads(row["key_experiment_json"]),
            "first_public_date": row["first_public_date"],
            "note": row["note"],
        }

    @staticmethod
    def _pseudonym(record_id: str) -> str:
        return "masked-" + hashlib.sha256(record_id.encode("utf-8")).hexdigest()[:12]

    def _mask_record(self, record: dict[str, Any], can_sensitive: bool) -> dict[str, Any]:
        if can_sensitive or record["sensitivity"] == "open":
            return {**record, "masked": False}
        return {
            **record,
            "record_id": self._pseudonym(record["record_id"]),
            "sponsor": MASKED_SPONSOR,
            "key_experiment": None,
            "note": None,
            "masked": True,
        }

    def _mask_timeline(
        self, record_id: str, sensitivity: str, events: list[dict[str, Any]], can_sensitive: bool
    ) -> list[dict[str, Any]]:
        if can_sensitive or sensitivity == "open":
            return events
        return [
            {"event_date": event["event_date"], "kind": event["kind"], "credibility": event["credibility"]}
            for event in events
        ]

    def get_record(self, actor_id: str, record_id: str) -> dict[str, Any]:
        user = self._require(actor_id, "track.read")
        row = self._latest_record_row(record_id)
        record = self._record_dict(row)
        can_sensitive = self._can_sensitive(user)
        timeline = self._timeline_map([record_id]).get(record_id, [])
        return {
            **self._mask_record(record, can_sensitive),
            "timeline": self._mask_timeline(record_id, record["sensitivity"], timeline, can_sensitive),
        }

    def add_timeline_event(self, actor_id: str, record_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "record.write")
        try:
            event = TimelineEventInput.from_dict(raw)
        except ValidationError as exc:
            raise ValidationFailed(str(exc)) from exc
        registry = self.connection.execute(
            "SELECT record_id FROM record_registry WHERE record_id=?", (record_id,)
        ).fetchone()
        if registry is None:
            raise NotFound(f"管线记录不存在: {record_id}")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO timeline_events(record_id,event_date,kind,summary,source_ref,recorded_by,recorded_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (record_id, event.event_date, event.kind, event.summary, event.source_ref, actor_id, self._now()),
            )
            event_id = int(cursor.lastrowid)
            self._audit("record", record_id, "timeline.added", actor_id, {"event_id": event_id})
        return {"event_id": event_id, "record_id": record_id, "event_date": event.event_date, "kind": event.kind}

    # ------------------------------------------------------------------
    # 同源合并与误关联拆回
    # ------------------------------------------------------------------

    def _link_events(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT action,group_id,record_ids_json FROM link_events ORDER BY link_event_id"
        ).fetchall()
        return [
            {"action": row["action"], "group_id": row["group_id"], "record_ids": json.loads(row["record_ids_json"])}
            for row in rows
        ]

    def _membership(self) -> dict[str, str]:
        return derive_groups(self._link_events())

    def _require_records(self, record_ids: list[str] | tuple[str, ...]) -> None:
        for record_id in record_ids:
            if self.connection.execute(
                "SELECT 1 FROM record_registry WHERE record_id=?", (record_id,)
            ).fetchone() is None:
                raise NotFound(f"管线记录不存在: {record_id}")

    def merge_records(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "link.write")
        try:
            merge = MergeInput.from_dict(raw)
        except ValidationError as exc:
            raise ValidationFailed(str(exc)) from exc
        self._require_records(merge.record_ids)
        membership = self._membership()
        groups = {membership.get(record_id) for record_id in merge.record_ids}
        if len(groups) == 1 and None not in groups:
            raise Conflict("这些记录已经属于同一合并组")
        group_id = "grp-" + uuid.uuid4().hex[:12]
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "INSERT INTO link_events(action,group_id,record_ids_json,reason,actor_id,created_at) "
                "VALUES('merge',?,?,?,?,?)",
                (group_id, canonical_json(list(merge.record_ids)), merge.reason, actor_id, self._now()),
            )
            self._audit("link", group_id, "link.merged", actor_id, {
                "record_ids": list(merge.record_ids), "reason": merge.reason,
            })
        return {"group_id": group_id, "record_ids": list(merge.record_ids)}

    def split_record(self, actor_id: str, record_id: str, reason: str) -> dict[str, Any]:
        self._require(actor_id, "link.write")
        if not reason.strip():
            raise ValidationFailed("拆分原因不能为空")
        self._require_records([record_id])
        membership = self._membership()
        group_id = membership.get(record_id)
        if group_id is None:
            raise Conflict("记录不在任何合并组中")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "INSERT INTO link_events(action,group_id,record_ids_json,reason,actor_id,created_at) "
                "VALUES('split',?,?,?,?,?)",
                (group_id, canonical_json([record_id]), reason.strip(), actor_id, self._now()),
            )
            self._audit("link", group_id, "link.split", actor_id, {
                "record_id": record_id, "reason": reason.strip(),
            })
        return {"record_id": record_id, "split_from": group_id}

    # ------------------------------------------------------------------
    # 证据可信度标注
    # ------------------------------------------------------------------

    def annotate_credibility(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "annotation.write")
        try:
            annotation = CredibilityAnnotationInput.from_dict(raw)
        except ValidationError as exc:
            raise ValidationFailed(str(exc)) from exc
        if annotation.subject_type == "record":
            self._require_records([annotation.subject_id])
        else:
            row = self.connection.execute(
                "SELECT event_id FROM timeline_events WHERE event_id=?", (annotation.subject_id,)
            ).fetchone()
            if row is None:
                raise NotFound(f"时间线事件不存在: {annotation.subject_id}")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO credibility_annotations(subject_type,subject_id,level,rationale,actor_id,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (
                    annotation.subject_type,
                    annotation.subject_id,
                    annotation.level,
                    annotation.rationale,
                    actor_id,
                    self._now(),
                ),
            )
            annotation_id = int(cursor.lastrowid)
            self._audit("annotation", str(annotation_id), "credibility.annotated", actor_id, {
                "subject_type": annotation.subject_type,
                "subject_id": annotation.subject_id,
                "level": annotation.level,
            })
        return {"annotation_id": annotation_id, "level": annotation.level}

    def _credibility_map(self) -> dict[tuple[str, str], str]:
        """每个主体只保留最新一条标注；历史标注全部保留在表中。"""

        rows = self.connection.execute(
            "SELECT subject_type,subject_id,level FROM credibility_annotations ORDER BY annotation_id"
        ).fetchall()
        result: dict[tuple[str, str], str] = {}
        for row in rows:
            result[(row["subject_type"], row["subject_id"])] = row["level"]
        return result

    def _timeline_map(self, record_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        wanted = set(record_ids)
        rows = self.connection.execute(
            "SELECT event_id,record_id,event_date,kind,summary,source_ref FROM timeline_events ORDER BY event_id"
        ).fetchall()
        credibility = self._credibility_map()
        result: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            if row["record_id"] not in wanted:
                continue
            result.setdefault(row["record_id"], []).append({
                "event_id": row["event_id"],
                "event_date": row["event_date"],
                "kind": row["kind"],
                "summary": row["summary"],
                "source_ref": row["source_ref"],
                "credibility": credibility.get(("timeline_event", str(row["event_id"])), "unrated"),
            })
        return result

    # ------------------------------------------------------------------
    # 竞争快照
    # ------------------------------------------------------------------

    def _manifest(self, selector: TrackSelector) -> dict[str, Any]:
        rows = self.connection.execute(
            "SELECT v.* FROM pipeline_record_versions v "
            "JOIN (SELECT record_id, MAX(revision) AS revision FROM pipeline_record_versions GROUP BY record_id) latest "
            "ON latest.record_id=v.record_id AND latest.revision=v.revision ORDER BY v.record_id"
        ).fetchall()
        records = [self._record_dict(row) for row in rows]
        records = [record for record in records if selector.matches(record)]
        if not records:
            raise NotFound("赛道内没有匹配的管线记录")
        record_ids = [record["record_id"] for record in records]
        membership = self._membership()
        groups = {record_id: membership[record_id] for record_id in record_ids if record_id in membership}
        timeline = self._timeline_map(record_ids)
        credibility = self._credibility_map()
        record_credibility = {
            record_id: credibility[("record", record_id)]
            for record_id in record_ids
            if ("record", record_id) in credibility
        }
        return {
            "track": selector.as_dict(),
            "as_of": self._today(),
            "records": records,
            "link_groups": groups,
            "timeline": timeline,
            "record_credibility": record_credibility,
        }

    def compare_tracks(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        user = self._require(actor_id, "snapshot.create")
        try:
            selector = TrackSelector.from_dict(raw)
        except ValidationError as exc:
            raise ValidationFailed(str(exc)) from exc
        manifest = self._manifest(selector)
        digest = content_digest([manifest])
        existing = self.connection.execute(
            "SELECT * FROM snapshots WHERE track_key=? AND input_sha256=?",
            (selector.track_key, digest),
        ).fetchone()
        if existing is not None:
            return self._snapshot_view(user, existing, replayed=True)
        result = compare(manifest)
        revision = self.connection.execute(
            "SELECT COALESCE(MAX(revision),0)+1 FROM snapshots WHERE track_key=?",
            (selector.track_key,),
        ).fetchone()[0]
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO snapshots(track_key,revision,algorithm_version,input_sha256,input_json,result_json,"
                    "state,created_by,created_at) VALUES(?,?,?,?,?,?, 'open', ?,?)",
                    (
                        selector.track_key,
                        revision,
                        ALGORITHM_VERSION,
                        digest,
                        canonical_json(manifest),
                        canonical_json(result),
                        actor_id,
                        self._now(),
                    ),
                )
                snapshot_id = int(cursor.lastrowid)
                self._audit("snapshot", str(snapshot_id), "snapshot.created", actor_id, {
                    "track_key": selector.track_key, "revision": revision, "input_sha256": digest,
                })
        except sqlite3.IntegrityError as exc:
            raise Conflict("快照并发创建冲突，请重试") from exc
        row = self.connection.execute(
            "SELECT * FROM snapshots WHERE snapshot_id=?", (snapshot_id,)
        ).fetchone()
        return self._snapshot_view(user, row, replayed=False)

    def _snapshot_view(self, user: sqlite3.Row, row: sqlite3.Row, replayed: bool | None = None) -> dict[str, Any]:
        manifest = json.loads(row["input_json"])
        result = json.loads(row["result_json"])
        can_sensitive = self._can_sensitive(user)
        records_view, timeline_view, result_view = self._mask_snapshot(manifest, result, can_sensitive)
        decisions = self.connection.execute(
            "SELECT decision_id,decision,rationale,decided_by,decided_at FROM snapshot_decisions "
            "WHERE snapshot_id=? ORDER BY decision_id",
            (row["snapshot_id"],),
        ).fetchall()
        view: dict[str, Any] = {
            "snapshot_id": row["snapshot_id"],
            "track_key": row["track_key"],
            "revision": row["revision"],
            "algorithm_version": row["algorithm_version"],
            "input_sha256": row["input_sha256"],
            "state": row["state"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "result": result_view,
            "records": records_view,
            "timeline": timeline_view,
            "decisions": [dict(decision) for decision in decisions],
        }
        if replayed is not None:
            view["replayed"] = replayed
        return view

    def _mask_snapshot(
        self, manifest: Mapping[str, Any], result: Mapping[str, Any], can_sensitive: bool
    ) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
        records = manifest["records"]
        if can_sensitive:
            return (
                [{**record, "masked": False} for record in records],
                {record_id: list(events) for record_id, events in manifest["timeline"].items()},
                copy.deepcopy(result),
            )
        sensitive_ids = {record["record_id"] for record in records if record["sensitivity"] != "open"}

        def public_id(record_id: str) -> str:
            return self._pseudonym(record_id) if record_id in sensitive_ids else record_id

        records_view = [self._mask_record(record, can_sensitive=False) for record in records]
        sensitivity_by_id = {record["record_id"]: record["sensitivity"] for record in records}
        timeline_view: dict[str, Any] = {}
        for record_id, events in manifest["timeline"].items():
            timeline_view[public_id(record_id)] = self._mask_timeline(
                record_id, sensitivity_by_id[record_id], list(events), can_sensitive=False
            )
        result_view = copy.deepcopy(result)
        for program in result_view["programs"]:
            program["member_record_ids"] = [public_id(record_id) for record_id in program["member_record_ids"]]
        for gap in result_view["evidence_gaps"]:
            if gap.get("record_id") is not None:
                gap["record_id"] = public_id(gap["record_id"])
        provenance = result_view["provenance"]
        provenance["record_revisions"] = {
            public_id(record_id): revision
            for record_id, revision in provenance["record_revisions"].items()
        }
        return records_view, timeline_view, result_view

    def get_snapshot(self, actor_id: str, snapshot_id: int) -> dict[str, Any]:
        user = self._require(actor_id, "track.read")
        row = self.connection.execute(
            "SELECT * FROM snapshots WHERE snapshot_id=?", (snapshot_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"快照不存在: {snapshot_id}")
        return self._snapshot_view(user, row)

    # ------------------------------------------------------------------
    # 投决引用与完整性校验
    # ------------------------------------------------------------------

    def record_decision(self, actor_id: str, snapshot_id: int, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "decision.write")
        try:
            decision = DecisionInput.from_dict(raw)
        except ValidationError as exc:
            raise ValidationFailed(str(exc)) from exc
        row = self.connection.execute(
            "SELECT snapshot_id,state FROM snapshots WHERE snapshot_id=?", (snapshot_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"快照不存在: {snapshot_id}")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO snapshot_decisions(snapshot_id,decision,rationale,decided_by,decided_at) "
                "VALUES(?,?,?,?,?)",
                (snapshot_id, decision.decision, decision.rationale, actor_id, self._now()),
            )
            decision_id = int(cursor.lastrowid)
            self.connection.execute(
                "UPDATE snapshots SET state='decision_locked' WHERE snapshot_id=? AND state='open'",
                (snapshot_id,),
            )
            self._audit("snapshot", str(snapshot_id), "snapshot.decision_recorded", actor_id, {
                "decision_id": decision_id, "decision": decision.decision,
            })
        return {"decision_id": decision_id, "snapshot_id": snapshot_id, "decision": decision.decision, "state": "decision_locked"}

    def verify_snapshot(self, actor_id: str, snapshot_id: int) -> dict[str, Any]:
        """用快照自带的输入清单重放计算，校验内容自始未被改写。"""

        self._require(actor_id, "audit.read")
        row = self.connection.execute(
            "SELECT * FROM snapshots WHERE snapshot_id=?", (snapshot_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"快照不存在: {snapshot_id}")
        manifest = json.loads(row["input_json"])
        input_match = content_digest([manifest]) == row["input_sha256"]
        recomputed = compare(manifest)
        result_match = canonical_json(recomputed) == row["result_json"]
        return {
            "snapshot_id": snapshot_id,
            "algorithm_version": row["algorithm_version"],
            "input_match": input_match,
            "result_match": result_match,
            "valid": input_match and result_match,
        }

    def audit_events(self, actor_id: str, entity_type: str, entity_id: str) -> list[dict[str, Any]]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute(
            "SELECT event_id,entity_type,entity_id,event_type,actor_id,payload_json,created_at FROM audit_events "
            "WHERE entity_type=? AND entity_id=? ORDER BY event_id",
            (entity_type, entity_id),
        ).fetchall()
        return [dict(row) | {"payload": json.loads(row["payload_json"])} for row in rows]
