"""赛道竞争快照的完整产品流程离线验收入口。

演示并自检：
1. 内部与外部记录登记、同源合并与拆回；
2. 证据可信度标注；
3. 快照冻结输入与结论、发布、投决锁定；
4. 投决后修订记录并另出新版快照，历史投决快照逐字节不变；
5. 敏感项目身份与实验细节按角色最小披露。
"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from .jsonio import canonical_json
from .service import CompetitiveIntelService
from .storage import connect, inspect_schema


def run(workspace: Path) -> dict[str, object]:
    fixtures = workspace / "fixtures"
    records = json.loads(
        (fixtures / "competitive_intel_demo_records.json").read_text(encoding="utf-8")
    )["records"]

    with tempfile.TemporaryDirectory(prefix="competitive-intel-") as temporary:
        database = Path(temporary) / "competitive.sqlite3"
        connection = connect(database)
        try:
            service = CompetitiveIntelService(connection)
            service.create_user("analyst-1", "情报分析员", "analyst")
            service.create_user("reviewer-1", "科学平台主管", "reviewer")
            service.create_user("committee-1", "投决委员", "committee")
            service.create_user("auditor-1", "审计人员", "auditor")

            for record in records:
                service.register_record("analyst-1", record["record_id"], record)

            # 内部项目与其对外会议摘要实为同源，合并为一个资产。
            service.create_asset(
                "analyst-1",
                "asset-tl1a-01",
                ["rec-int-0471", "rec-ext-0922"],
                "会议摘要 2241 经核实为内部项目的合作中心披露",
            )
            # 标注证据可信度。
            service.annotate_credibility(
                "analyst-1",
                "rec-alpha-614",
                1,
                "high",
                "III 期顶线结果与注册库记录一致",
                ["https://example.org/pr/alpha-614-topline"],
            )
            service.annotate_credibility(
                "analyst-1",
                "rec-delta-rumor",
                1,
                "low",
                "仅行业通讯传闻，无公司确认与实验版本",
                ["newsletter://tracker/2025-01-20"],
            )

            scope = {
                "name": "TL1A 赛道 2026Q3 评审",
                "as_of": "2026-09-30",
                "target": "TL1A",
                "indication_universe": ["溃疡性结肠炎", "克罗恩病", "强直性脊柱炎"],
            }
            v1 = service.create_snapshot("analyst-1", "snap-tl1a-2026q3", scope)
            service.publish_snapshot("reviewer-1", "snap-tl1a-2026q3", v1["version"])
            judgment = service.record_judgment(
                "committee-1",
                "judg-001",
                "snap-tl1a-2026q3",
                v1["version"],
                "follow",
                "抗体赛道拥挤但本公司 II 期数据具差异化，持续跟踪联用与皮下剂型窗口",
            )

            # 固化投决所依据的历史结论。
            v1_after_judgment = service.get_snapshot(
                "auditor-1", "snap-tl1a-2026q3", v1["version"]
            )
            v1_result_fingerprint = canonical_json(v1_after_judgment["result"])
            v1_input_sha = v1_after_judgment["input_sha256"]
            tracks_v1 = v1_after_judgment["result"]["tracks"]
            antibody_track = next(
                track for track in tracks_v1 if track["mechanism_key"] == "tl1a_中和抗体"
            )
            heat_v1 = antibody_track["heat"]
            assets_v1 = antibody_track["asset_count"]
            if v1_after_judgment["status"] != "locked":
                raise AssertionError("投决后快照应为 locked")

            # 投决之后：外部记录修订（阶段推进）、误关联被拆回，全部只产生新事实。
            service.revise_record("analyst-1", "rec-gamma-331", _promoted(records, "rec-gamma-331"))
            service.unmerge_record(
                "analyst-1",
                "rec-ext-0922",
                "复核发现合作中心摘要来自另一独立项目，拆回误关联",
            )

            v2 = service.create_snapshot("analyst-1", "snap-tl1a-2026q3", scope)
            service.publish_snapshot("reviewer-1", "snap-tl1a-2026q3", v2["version"])

            # 历史投决快照必须逐字节保持，且仍能复现当时结论。
            v1_replayed = service.get_snapshot("auditor-1", "snap-tl1a-2026q3", v1["version"])
            if v1_replayed["status"] != "locked":
                raise AssertionError("历史投决快照应保持 locked")
            history_immutable = (
                canonical_json(v1_replayed["result"]) == v1_result_fingerprint
                and v1_replayed["input_sha256"] == v1_input_sha
                and v1_replayed["judgment"]["decision"] == "follow"
            )
            frozen_asset_still_merged = any(
                item["asset_id"] == "asset-tl1a-01"
                and item["record_id"] == "rec-ext-0922"
                for item in v1_replayed["items"]
            )

            # 新快照反映拆回后的现状（该记录在 v2 中不再归属同源资产，赛道资产数 +1）。
            v2_cover = next(
                item for item in v2["items"] if item["record_id"] == "rec-ext-0922"
            )
            split_reflected = v2_cover["asset_id"] is None
            antibody_track_v2 = next(
                track for track in v2["result"]["tracks"]
                if track["mechanism_key"] == "tl1a_中和抗体"
            )
            heat_v2 = antibody_track_v2["heat"]
            asset_count_changed = antibody_track_v2["asset_count"] == assets_v1 + 1

            # 角色最小披露：分析员看不到敏感项目身份与实验细节。
            analyst_view = service.get_snapshot("analyst-1", "snap-tl1a-2026q3", v2["version"])
            sensitive_item = next(
                item for item in analyst_view["items"]
                if item["record_id"] == "rec-int-0471"
            )
            analyst_redacted = (
                sensitive_item["display_name"] == "***REDACTED***"
                and sensitive_item["experiments"]
                and sensitive_item["experiments"][0].get("redacted") is True
                and sensitive_item["target"] == "TL1A"
            )
            # 评审主管可见身份，但仍不见敏感实验细节；投决委员可见全部。
            reviewer_item = next(
                item
                for item in service.get_snapshot("reviewer-1", "snap-tl1a-2026q3", v2["version"])["items"]
                if item["record_id"] == "rec-int-0471"
            )
            committee_item = next(
                item
                for item in service.get_snapshot("committee-1", "snap-tl1a-2026q3", v2["version"])["items"]
                if item["record_id"] == "rec-int-0471"
            )
            role_disclosure = (
                reviewer_item["display_name"] == "PROJ-NIGHTINGALE"
                and reviewer_item["experiments"][0].get("redacted") is True
                and committee_item["experiments"][0]["experiment_version_id"] == "exp-int-pkpd-0471"
            )

            schema = inspect_schema(connection)
            audit_count = len(service.audit_events("auditor-1"))
        finally:
            connection.close()

    if schema["missing_tables"] or schema["schema_version"] != "1":
        raise RuntimeError("SQLite 基础结构检查失败")
    white_space = v2["result"]["clinical_white_space"]
    gaps = v2["result"]["evidence_gaps"]
    return {
        "status": "ok",
        "record_count": len(records),
        "snapshot_v1_heat": heat_v1,
        "snapshot_v2_heat": heat_v2,
        "asset_count_changed_after_split": asset_count_changed,
        "history_immutable": history_immutable,
        "frozen_asset_still_merged": frozen_asset_still_merged,
        "split_reflected_in_v2": split_reflected,
        "analyst_sensitive_redacted": analyst_redacted,
        "role_minimum_disclosure": role_disclosure,
        "judgment": judgment["decision"],
        "locked_input_sha256": judgment["locked_input_sha256"],
        "uncovered_indication_cells": len(white_space["uncovered_cells"]),
        "external_only_positions": len(white_space["external_only_positions"]),
        "evidence_gap_kinds": sorted({gap["kind"] for gap in gaps}),
        "audit_event_count": audit_count,
        "schema": schema,
    }


def _promoted(records: list[dict], record_id: str) -> dict:
    import copy

    record = copy.deepcopy(next(item for item in records if item["record_id"] == record_id))
    record["stage"] = "phase_2"
    record["clinical_attributes"]["line_of_therapy"] = ["1L", "2L"]
    record["timeline"].append(
        {"event_type": "trial_started", "event_date": "2026-08-01", "summary": "II 期启动", "source_ref": "https://example.org/trial/gamma331-p2"}
    )
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="执行赛道竞争快照服务的离线自检")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    result = run(args.workspace.resolve())
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
