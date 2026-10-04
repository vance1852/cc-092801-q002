"""赛道比较能力的离线验收：从登记管线到投决锁定的完整流程。"""

from __future__ import annotations

import argparse
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .service import TrackIntelService
from .storage import connect, inspect_schema

TRACK = {"target": "KRAS G12C", "mechanism": "small-molecule inhibitor", "indication": "NSCLC"}


def _seed(service: TrackIntelService) -> None:
    service.register_record("analyst-1", {
        "record_id": "int-aurora-01", "source_kind": "internal", **TRACK,
        "phase": "phase2", "sponsor": "内部项目 Aurora", "sensitivity": "restricted",
        "clinical_features": ["oral", "first-line", "biomarker-g12c"],
        "key_experiment": {"experiment_ref": "batch-aurora-3", "experiment_version": "analysis-7"},
        "note": "内部核心候选",
    })
    service.register_record("analyst-1", {
        "record_id": "ext-mirati-01", "source_kind": "external", **TRACK,
        "phase": "phase3", "sponsor": "外部公司 M", "sensitivity": "open",
        "clinical_features": ["oral", "second-line"], "first_public_date": "2025-11-02",
    })
    service.register_record("analyst-1", {
        "record_id": "ext-nov-01", "source_kind": "external", **TRACK,
        "phase": "phase2", "sponsor": "外部公司 N", "sensitivity": "open",
        "clinical_features": ["oral", "first-line"], "first_public_date": "2026-01-15",
    })
    service.register_record("analyst-1", {
        "record_id": "ext-nov-dup", "source_kind": "external", **TRACK,
        "phase": "phase2", "sponsor": "外部公司 N", "sensitivity": "open",
        "clinical_features": ["oral", "first-line"], "first_public_date": "2026-01-15",
        "note": "同一项目的另一来源登记",
    })
    service.register_record("analyst-1", {
        "record_id": "ext-old-01", "source_kind": "external", **TRACK,
        "phase": "phase1", "sponsor": "外部公司 O", "sensitivity": "open",
        "clinical_features": ["oral"], "first_public_date": "2024-05-20",
    })


def run(workspace: Path) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="track-intel-") as temporary:
        database = Path(temporary) / "track_intel.sqlite3"
        connection = connect(database)
        try:
            service = TrackIntelService(
                connection, FrozenClock(datetime(2026, 10, 4, 8, 0, tzinfo=timezone.utc))
            )
            service.create_user("analyst-1", "赛道分析人员", "analyst")
            service.create_user("reviewer-1", "科学平台主管", "reviewer")
            service.create_user("viewer-1", "受限查看人员", "viewer")
            service.create_user("auditor-1", "审计人员", "auditor")
            _seed(service)

            # 同源记录合并：ext-nov-01 与 ext-nov-dup 是同一项目的两次登记。
            service.merge_records("analyst-1", {
                "record_ids": ["ext-nov-01", "ext-nov-dup"],
                "reason": "两条外部登记指向同一公司同一项目",
            })

            # 公开时间线与证据可信度标注。
            mirati_event = service.add_timeline_event("analyst-1", "ext-mirati-01", {
                "event_date": "2026-08-01", "kind": "trial_registry",
                "summary": "三期临床登记更新入组进度", "source_ref": "registry://NCT-0001",
            })
            nov_event = service.add_timeline_event("analyst-1", "ext-nov-01", {
                "event_date": "2026-05-01", "kind": "press_release",
                "summary": "新闻稿宣称二期达到主要终点", "source_ref": "press://2026-05-01",
            })
            service.add_timeline_event("analyst-1", "ext-old-01", {
                "event_date": "2024-06-01", "kind": "conference",
                "summary": "会议摘要披露一期剂量爬坡", "source_ref": "conf://2024",
            })
            service.annotate_credibility("analyst-1", {
                "subject_type": "record", "subject_id": "ext-mirati-01",
                "level": "high", "rationale": "登记信息与论文一致",
            })
            service.annotate_credibility("analyst-1", {
                "subject_type": "timeline_event", "subject_id": str(nov_event["event_id"]),
                "level": "low", "rationale": "仅新闻稿，无会议摘要或登记佐证",
            })

            # 第一版快照并用于投决。
            first = service.compare_tracks("analyst-1", TRACK)
            service.record_decision("reviewer-1", first["snapshot_id"], {
                "decision": "advance", "rationale": "biomarker-g12c 一线差异窗口仍然开放",
            })
            locked = service.get_snapshot("reviewer-1", first["snapshot_id"])

            # 快照之后的数据演进只产生新修订，不改写已投决版本。
            service.revise_record("analyst-1", "int-aurora-01", {
                "record_id": "int-aurora-01", "source_kind": "internal", **TRACK,
                "phase": "phase2", "sponsor": "内部项目 Aurora", "sensitivity": "restricted",
                "clinical_features": ["oral", "first-line", "biomarker-g12c"],
                "key_experiment": {"experiment_ref": "batch-aurora-4", "experiment_version": "analysis-9"},
            })
            second = service.compare_tracks("analyst-1", TRACK)
            replayed = service.compare_tracks("analyst-1", TRACK)
            reread_first = service.get_snapshot("auditor-1", first["snapshot_id"])
            verify = service.verify_snapshot("auditor-1", first["snapshot_id"])
            viewer_view = service.get_snapshot("viewer-1", second["snapshot_id"])
            schema = inspect_schema(connection)
        finally:
            connection.close()
    if schema["missing_tables"] or schema["schema_version"] != "1":
        raise RuntimeError("SQLite 基础结构检查失败")
    if reread_first["input_sha256"] != first["input_sha256"]:
        raise RuntimeError("已投决快照内容被改写")
    masked_internal = [r for r in viewer_view["records"] if r["masked"]]
    return {
        "status": "ok",
        "track_key": first["track_key"],
        "snapshot_v1": {"snapshot_id": first["snapshot_id"], "revision": first["revision"], "state": locked["state"]},
        "snapshot_v2": {"snapshot_id": second["snapshot_id"], "revision": second["revision"]},
        "replayed_same_snapshot": replayed["snapshot_id"] == second["snapshot_id"] and replayed["replayed"],
        "crowding": first["result"]["crowding"]["band"],
        "crowding_score": first["result"]["crowding"]["score"],
        "follow_risk": first["result"]["follow_risk"]["signal"],
        "uncovered_features": [item["feature"] for item in first["result"]["differentiation"]["uncovered"]],
        "evidence_gap_rules": sorted({gap["rule"] for gap in first["result"]["evidence_gaps"]}),
        "decision": locked["decisions"][0]["decision"],
        "verify_valid": verify["valid"],
        "viewer_masked_records": len(masked_internal),
        "schema": schema,
        "workspace": workspace.name,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="执行赛道比较服务的离线自检")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    result = run(args.workspace.resolve())
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
