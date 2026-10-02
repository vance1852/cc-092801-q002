from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone

from competitive_intel.clock import FrozenClock
from competitive_intel.contracts import AssetRecordDraft, ContractError
from competitive_intel.errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from competitive_intel.jsonio import canonical_json
from competitive_intel.service import CompetitiveIntelService


def make_record(**overrides) -> dict:
    record = {
        "source": "trial_registry",
        "display_name": "ASSET-1",
        "organization": "示例公司",
        "origin": "external",
        "sensitivity": "public",
        "target": "TL1A",
        "mechanism": "TL1A 中和抗体",
        "modalities": ["单抗"],
        "indications": ["溃疡性结肠炎"],
        "clinical_attributes": {"combination": ["单药"], "route": ["静脉输注"]},
        "stage": "phase_2",
        "experiments": [
            {
                "experiment_version_id": "exp-1",
                "experiment_type": "PoC",
                "version": "v1",
                "result_summary": "缓解率 30%",
                "observed_at": "2026-01-01",
            }
        ],
        "timeline": [
            {
                "event_type": "trial_started",
                "event_date": "2025-09-01",
                "summary": "II 期启动",
                "source_ref": "https://example.org/t1",
            }
        ],
        "evidence_tier": "conference",
        "evidence_refs": ["https://example.org/t1"],
    }
    record.update(overrides)
    return record


class ServiceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 30, tzinfo=timezone.utc))
        self.service = CompetitiveIntelService(self.connection, self.clock)
        self.service.create_user("analyst", "分析员", "analyst")
        self.service.create_user("reviewer", "评审", "reviewer")
        self.service.create_user("committee", "委员", "committee")
        self.service.create_user("auditor", "审计", "auditor")

    def tearDown(self) -> None:
        self.connection.close()

    def register(self, record_id: str, **overrides) -> dict:
        return self.service.register_record("analyst", record_id, make_record(**overrides))


class ContractTests(unittest.TestCase):
    def test_valid_draft(self) -> None:
        draft = AssetRecordDraft.from_dict(make_record())
        self.assertEqual(draft.target, "TL1A")
        self.assertEqual(draft.clinical_attributes["route"], ("静脉输注",))

    def test_indications_required(self) -> None:
        raw = make_record(indications=[])
        with self.assertRaisesRegex(ContractError, "indications 不能为空"):
            AssetRecordDraft.from_dict(raw)

    def test_unknown_stage_rejected(self) -> None:
        raw = make_record(stage="phase_9")
        with self.assertRaisesRegex(ContractError, "stage"):
            AssetRecordDraft.from_dict(raw)

    def test_unknown_clinical_dimension_rejected(self) -> None:
        raw = make_record(clinical_attributes={"unknown_dim": ["x"]})
        with self.assertRaisesRegex(ContractError, "未知维度"):
            AssetRecordDraft.from_dict(raw)

    def test_evidence_refs_required(self) -> None:
        raw = make_record(evidence_refs=[])
        with self.assertRaisesRegex(ContractError, "evidence_refs 不能为空"):
            AssetRecordDraft.from_dict(raw)

    def test_bad_date_rejected(self) -> None:
        raw = make_record(timeline=[{"event_type": "disclosed", "event_date": "not-a-date", "summary": "x"}])
        with self.assertRaisesRegex(ContractError, "必须是 YYYY-MM-DD"):
            AssetRecordDraft.from_dict(raw)

    def test_duplicate_experiment_id_rejected(self) -> None:
        raw = make_record(
            experiments=[
                {
                    "experiment_version_id": "exp",
                    "experiment_type": "PoC",
                    "version": "v1",
                    "result_summary": "a",
                    "observed_at": "2026-01-01",
                },
                {
                    "experiment_version_id": "exp",
                    "experiment_type": "PoC",
                    "version": "v2",
                    "result_summary": "b",
                    "observed_at": "2026-02-01",
                },
            ]
        )
        with self.assertRaisesRegex(ContractError, "experiment_version_id 不能重复"):
            AssetRecordDraft.from_dict(raw)


class RecordVersioningTests(ServiceTestBase):
    def test_revision_appends_and_keeps_history(self) -> None:
        self.register("r1")
        self.service.revise_record("analyst", "r1", make_record(stage="phase_3"))
        latest = self.service.get_record("analyst", "r1")
        self.assertEqual(latest["latest_version"], 2)
        self.assertEqual(latest["content"]["stage"], "phase_3")
        v1 = self.service.get_record("analyst", "r1", 1)
        self.assertEqual(v1["content"]["stage"], "phase_2")
        rows = self.connection.execute(
            "SELECT count(*) FROM record_versions WHERE record_id='r1'"
        ).fetchone()[0]
        self.assertEqual(rows, 2)

    def test_register_duplicate_rejected(self) -> None:
        self.register("r1")
        with self.assertRaises(Conflict):
            self.register("r1")

    def test_revise_missing_rejected(self) -> None:
        with self.assertRaises(NotFound):
            self.service.revise_record("analyst", "missing", make_record())

    def test_same_content_same_digest(self) -> None:
        first = self.register("r1")
        self.connection.execute("DELETE FROM record_versions WHERE record_id='r1'")
        again = self.register("r1")
        self.assertEqual(first["content_sha256"], again["content_sha256"])


class CredibilityTests(ServiceTestBase):
    def test_annotation_history_kept(self) -> None:
        self.register("r1")
        self.service.annotate_credibility("analyst", "r1", 1, "low", "仅传闻", ["ref-1"])
        second = self.service.annotate_credibility("analyst", "r1", 1, "high", "已被 III 期证实", ["ref-2"])
        self.assertEqual(second["supersedes_annotation"], 1)
        view = self.service.get_record("analyst", "r1")
        self.assertEqual(view["content"]["credibility"]["level"], "high")
        self.assertEqual(len(view["annotations"]), 2)

    def test_requires_evidence_ref(self) -> None:
        self.register("r1")
        with self.assertRaises(ValidationFailed):
            self.service.annotate_credibility("analyst", "r1", 1, "low", "理由", [])

    def test_reviewer_cannot_annotate(self) -> None:
        self.register("r1")
        with self.assertRaises(Forbidden):
            self.service.annotate_credibility("reviewer", "r1", 1, "low", "x", ["r"])


class MergeUnmergeTests(ServiceTestBase):
    def test_merge_and_unmerge_lineage(self) -> None:
        self.register("r1")
        self.register("r2")
        asset = self.service.create_asset("analyst", "a1", ["r1", "r2"], "同源")
        self.assertEqual(asset["active_records"], ["r1", "r2"])
        result = self.service.unmerge_record("analyst", "r2", "误关联")
        self.assertEqual(result["status"], "revoked")
        asset_after = self.service.get_asset("analyst", "a1")
        self.assertEqual(asset_after["active_records"], ["r1"])
        history = asset_after["membership_history"]
        self.assertEqual([row["status"] for row in history], ["active", "revoked"])
        self.assertIsNone(history[0]["previous_membership_id"])

    def test_remerge_records_lineage_pointer(self) -> None:
        self.register("r1")
        self.register("r2")
        self.service.create_asset("analyst", "a1", ["r1", "r2"], "同源")
        self.service.unmerge_record("analyst", "r2", "误关联")
        self.service.merge_into_asset("analyst", "a1", ["r2"], "复核后确认同源")
        membership = self.connection.execute(
            "SELECT previous_membership_id FROM asset_memberships WHERE record_id='r2' AND status='active'"
        ).fetchone()
        revoked = self.connection.execute(
            "SELECT membership_id FROM asset_memberships WHERE record_id='r2' AND status='revoked'"
        ).fetchone()
        self.assertEqual(membership[0], revoked[0])

    def test_cannot_merge_into_other_asset_without_unmerge(self) -> None:
        self.register("r1")
        self.service.create_asset("analyst", "a1", ["r1"], "同源")
        with self.assertRaises(InvalidState):
            self.service.merge_into_asset("analyst", "a1", ["r1"], "重复归并")

    def test_create_asset_rejects_duplicate(self) -> None:
        self.register("r1")
        self.service.create_asset("analyst", "a1", ["r1"], "x")
        with self.assertRaises(Conflict):
            self.service.create_asset("analyst", "a1", ["r1"], "y")

    def test_unmerge_without_membership(self) -> None:
        self.register("r1")
        with self.assertRaises(InvalidState):
            self.service.unmerge_record("analyst", "r1", "无归属")

    def test_committee_cannot_merge(self) -> None:
        self.register("r1")
        with self.assertRaises(Forbidden):
            self.service.create_asset("committee", "a1", ["r1"], "x")


def _snapshot_scope(**overrides) -> dict:
    scope = {"name": "TL1A 评审", "as_of": "2026-09-30", "target": "TL1A"}
    scope.update(overrides)
    return scope


class SnapshotImmutabilityTests(ServiceTestBase):
    def _populated(self) -> dict:
        self.register("r1", stage="phase_2")
        self.register("r2", stage="phase_3", indications=["克罗恩病"])
        scope = _snapshot_scope(indication_universe=["溃疡性结肠炎", "克罗恩病"])
        v1 = self.service.create_snapshot("analyst", "s1", scope)
        self.service.publish_snapshot("reviewer", "s1", v1["version"])
        judgment = self.service.record_judgment(
            "committee", "j1", "s1", v1["version"], "follow", "持续跟踪"
        )
        return {"scope": scope, "v1": v1, "judgment": judgment}

    def test_judgment_locks_snapshot(self) -> None:
        data = self._populated()
        snap = self.service.get_snapshot("auditor", "s1", data["v1"]["version"])
        self.assertEqual(snap["status"], "locked")
        self.assertEqual(snap["judgment"]["decision"], "follow")
        self.assertEqual(snap["judgment"]["snapshot_input_sha256"], data["v1"]["input_sha256"])

    def test_updates_create_new_version_without_rewriting_history(self) -> None:
        data = self._populated()
        version = data["v1"]["version"]
        historical = self.service.get_snapshot("auditor", "s1", version)
        historical_result = canonical_json(historical["result"])
        self.service.revise_record("analyst", "r1", make_record(stage="phase_2_3"))
        v2 = self.service.create_snapshot("analyst", "s1", data["scope"])
        self.assertEqual(v2["version"], 2)
        self.assertEqual(v2["supersedes_version"], 1)
        replayed = self.service.get_snapshot("auditor", "s1", version)
        self.assertEqual(canonical_json(replayed["result"]), historical_result)
        self.assertEqual(replayed["status"], "locked")
        self.assertEqual(replayed["input_sha256"], historical["input_sha256"])

    def test_unmerge_does_not_alter_frozen_snapshot(self) -> None:
        data = self._populated()
        self.service.create_asset("analyst", "a1", ["r1", "r2"], "误判同源")
        merged_snapshot = self.service.create_snapshot("analyst", "s2", data["scope"])
        self.service.unmerge_record("analyst", "r2", "拆回误关联")
        frozen = self.service.get_snapshot("auditor", "s2", merged_snapshot["version"])
        frozen_assets = {item["record_id"]: item["asset_id"] for item in frozen["items"]}
        self.assertEqual(frozen_assets["r2"], "a1")
        latest = self.service.create_snapshot("analyst", "s3", data["scope"])
        latest_assets = {item["record_id"]: item["asset_id"] for item in latest["items"]}
        self.assertIsNone(latest_assets["r2"])

    def test_judgment_requires_published(self) -> None:
        self.register("r1")
        snap = self.service.create_snapshot("analyst", "s1", _snapshot_scope())
        with self.assertRaises(InvalidState):
            self.service.record_judgment("committee", "j1", "s1", snap["version"], "watch", "x")

    def test_analyst_cannot_publish_or_judge(self) -> None:
        self.register("r1")
        snap = self.service.create_snapshot("analyst", "s1", _snapshot_scope())
        with self.assertRaises(Forbidden):
            self.service.publish_snapshot("analyst", "s1", snap["version"])
        with self.assertRaises(Forbidden):
            self.service.record_judgment("analyst", "j1", "s1", snap["version"], "watch", "x")

    def test_snapshot_requires_records(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.service.create_snapshot("analyst", "s1", _snapshot_scope())

    def test_snapshot_pins_specific_record_versions(self) -> None:
        self.register("r1", stage="phase_1")
        scope = _snapshot_scope()
        first = self.service.create_snapshot("analyst", "s1", scope)
        self.service.revise_record("analyst", "r1", make_record(stage="phase_3"))
        second = self.service.create_snapshot("analyst", "s1", scope)
        first_item = next(item for item in first["items"] if item["record_id"] == "r1")
        second_item = next(item for item in second["items"] if item["record_id"] == "r1")
        self.assertEqual(first_item["version"], 1)
        self.assertEqual(second_item["version"], 2)
        self.assertNotEqual(first["input_sha256"], second["input_sha256"])


    def test_judgment_rationale_redacted_for_analyst(self) -> None:
        data = self._populated()
        version = data["v1"]["version"]
        auditor = self.service.get_snapshot("auditor", "s1", version)
        self.assertEqual(auditor["judgment"]["rationale"], "持续跟踪")
        analyst = self.service.get_snapshot("analyst", "s1", version)
        self.assertEqual(analyst["judgment"]["decision"], "follow")
        self.assertEqual(analyst["judgment"]["rationale"], "***REDACTED***")
        committee = self.service.get_snapshot("committee", "s1", version)
        self.assertEqual(committee["judgment"]["rationale"], "持续跟踪")


class AnalysisTests(ServiceTestBase):
    def test_heat_levels(self) -> None:
        for index in range(8):
            stage = "phase_3" if index < 4 else "phase_1"
            self.register(f"r{index}", display_name=f"A{index}", stage=stage)
        snap = self.service.create_snapshot("analyst", "s1", _snapshot_scope())
        track = snap["result"]["tracks"][0]
        self.assertEqual(track["heat"], "saturated")
        self.assertEqual(track["phase2plus_assets"], 4)
        matched = [rule for rule in track["heat_rule_trace"] if rule["matched"]]
        self.assertEqual(matched[-1]["level"], "saturated")

    def test_open_track(self) -> None:
        self.register("r1", stage="preclinical")
        snap = self.service.create_snapshot("analyst", "s1", _snapshot_scope())
        self.assertEqual(snap["result"]["tracks"][0]["heat"], "open")

    def test_mechanisms_form_separate_tracks(self) -> None:
        self.register("r1", mechanism="TL1A 中和抗体")
        self.register("r2", mechanism="TL1A 小分子拮抗剂", modalities=["小分子"])
        snap = self.service.create_snapshot("analyst", "s1", _snapshot_scope())
        self.assertEqual(len(snap["result"]["tracks"]), 2)

    def test_merge_reduces_asset_count(self) -> None:
        self.register("r1")
        self.register("r2")
        before = self.service.create_snapshot("analyst", "s1", _snapshot_scope())
        self.assertEqual(before["result"]["tracks"][0]["asset_count"], 2)
        self.service.create_asset("analyst", "a1", ["r1", "r2"], "同源")
        after = self.service.create_snapshot("analyst", "s2", _snapshot_scope())
        self.assertEqual(after["result"]["tracks"][0]["asset_count"], 1)

    def test_clinical_white_space_uncovered_cells(self) -> None:
        self.register(
            "r1",
            clinical_attributes={"combination": ["单药"]},
        )
        scope = _snapshot_scope(indication_universe=["溃疡性结肠炎", "克罗恩病"])
        snap = self.service.create_snapshot("analyst", "s1", scope)
        white_space = snap["result"]["clinical_white_space"]
        uncovered = {(cell["indication"], cell["dimension"]) for cell in white_space["uncovered_cells"]}
        self.assertIn(("克罗恩病", "route"), uncovered)
        self.assertIn(("克罗恩病", "combination"), uncovered)
        # 溃疡性结肠炎声明了 combination，不应出现在该维度空白格里。
        self.assertNotIn(("溃疡性结肠炎", "combination"), uncovered)

    def test_external_only_positions_flag_internal_gap(self) -> None:
        self.register(
            "internal-1",
            origin="internal",
            clinical_attributes={"route": ["静脉输注"]},
        )
        self.register(
            "external-1",
            organization="外部公司",
            clinical_attributes={"route": ["皮下注射"]},
        )
        snap = self.service.create_snapshot("analyst", "s1", _snapshot_scope())
        positions = snap["result"]["clinical_white_space"]["external_only_positions"]
        subcutaneous = next(item for item in positions if item["label"] == "皮下注射")
        self.assertFalse(subcutaneous["internal_present"])

    def test_unique_positions(self) -> None:
        self.register("r1", clinical_attributes={"route": ["口服"]})
        self.register("r2", clinical_attributes={"route": ["静脉输注"]})
        snap = self.service.create_snapshot("analyst", "s1", _snapshot_scope())
        labels = {item["label"] for item in snap["result"]["clinical_white_space"]["unique_positions"]}
        self.assertIn("口服", labels)

    def test_evidence_gap_stage_evidence_mismatch(self) -> None:
        self.register("r1", stage="phase_3", evidence_tier="press_release", timeline=[])
        snap = self.service.create_snapshot(
            "analyst", "s1", _snapshot_scope(as_of="2026-09-30")
        )
        kinds = {gap["kind"] for gap in snap["result"]["evidence_gaps"]}
        self.assertIn("stage_evidence_mismatch", kinds)
        self.assertIn("timeline_stage_mismatch", kinds)

    def test_evidence_gap_credibility_unannotated(self) -> None:
        self.register("r1")
        snap = self.service.create_snapshot("analyst", "s1", _snapshot_scope())
        self.assertTrue(
            any(gap["kind"] == "credibility_unannotated" for gap in snap["result"]["evidence_gaps"])
        )

    def test_evidence_gap_low_credibility(self) -> None:
        self.register("r1")
        self.service.annotate_credibility("analyst", "r1", 1, "low", "无法核实", ["ref"])
        snap = self.service.create_snapshot("analyst", "s1", _snapshot_scope())
        gap = next(gap for gap in snap["result"]["evidence_gaps"] if gap["kind"] == "credibility_low")
        self.assertEqual(gap["annotation_id"], 1)

    def test_result_carries_version_manifest(self) -> None:
        self.register("r1", stage="phase_2")
        snap = self.service.create_snapshot("analyst", "s1", _snapshot_scope())
        records = snap["result"]["versions"]["records"]
        self.assertEqual(records[0]["record_id"], "r1")
        self.assertEqual(len(records[0]["content_sha256"]), 64)
        self.assertEqual(snap["result"]["algorithm_version"], snap["algorithm_version"])


class DisclosureTests(ServiceTestBase):
    def _snapshot_with_sensitive(self):
        self.register(
            "internal-1",
            origin="internal",
            sensitivity="sensitive",
            display_name="SECRET-PROJECT",
            organization="本公司",
        )
        self.register("external-1", display_name="PUBLIC", organization="外部公司")
        return self.service.create_snapshot("analyst", "s1", _snapshot_scope())

    def test_analyst_sees_structure_but_not_identity_or_experiments(self) -> None:
        snap = self._snapshot_with_sensitive()
        view = self.service.get_snapshot("analyst", "s1", snap["version"])
        sensitive = next(item for item in view["items"] if item["record_id"] == "internal-1")
        self.assertEqual(sensitive["display_name"], "***REDACTED***")
        self.assertEqual(sensitive["target"], "TL1A")
        self.assertEqual(sensitive["stage"], "phase_2")
        self.assertTrue(all(exp.get("redacted") for exp in sensitive["experiments"]))
        self.assertEqual(sensitive["evidence_refs"], [])

    def test_reviewer_sees_identity_but_not_sensitive_experiments(self) -> None:
        snap = self._snapshot_with_sensitive()
        view = self.service.get_snapshot("reviewer", "s1", snap["version"])
        sensitive = next(item for item in view["items"] if item["record_id"] == "internal-1")
        self.assertEqual(sensitive["display_name"], "SECRET-PROJECT")
        self.assertTrue(all(exp.get("redacted") for exp in sensitive["experiments"]))

    def test_committee_sees_full_detail(self) -> None:
        snap = self._snapshot_with_sensitive()
        view = self.service.get_snapshot("committee", "s1", snap["version"])
        sensitive = next(item for item in view["items"] if item["record_id"] == "internal-1")
        self.assertEqual(sensitive["display_name"], "SECRET-PROJECT")
        self.assertEqual(sensitive["experiments"][0]["experiment_version_id"], "exp-1")
        self.assertIn("https://example.org/t1", sensitive["evidence_refs"])

    def test_evidence_gap_detail_redacted_for_analyst(self) -> None:
        self.register(
            "internal-1",
            origin="internal",
            sensitivity="sensitive",
            stage="phase_3",
            evidence_tier="press_release",
            timeline=[],
        )
        snap = self.service.create_snapshot("analyst", "s1", _snapshot_scope())
        analyst_view = self.service.get_snapshot("analyst", "s1", snap["version"])
        mismatch = next(
            gap for gap in analyst_view["result"]["evidence_gaps"]
            if gap["kind"] == "stage_evidence_mismatch"
        )
        self.assertEqual(mismatch["detail"], "***REDACTED***")
        auditor_view = self.service.get_snapshot("auditor", "s1", snap["version"])
        auditor_mismatch = next(
            gap for gap in auditor_view["result"]["evidence_gaps"]
            if gap["kind"] == "stage_evidence_mismatch"
        )
        self.assertNotEqual(auditor_mismatch["detail"], "***REDACTED***")

    def test_restricted_experiment_hidden_from_analyst(self) -> None:
        self.register("r1", sensitivity="restricted")
        snap = self.service.create_snapshot("analyst", "s1", _snapshot_scope())
        view = self.service.get_snapshot("analyst", "s1", snap["version"])
        item = view["items"][0]
        self.assertTrue(all(exp.get("redacted") for exp in item["experiments"]))
        reviewer = self.service.get_snapshot("reviewer", "s1", snap["version"])
        self.assertEqual(reviewer["items"][0]["experiments"][0]["experiment_version_id"], "exp-1")

    def test_credibility_rationale_redacted_with_experiment_detail(self) -> None:
        self.register("internal-1", origin="internal", sensitivity="sensitive")
        self.service.annotate_credibility(
            "analyst", "internal-1", 1, "high", "内部 PK/PD 报告 v3 数据一致", ["internal://exp/3"]
        )
        snap = self.service.create_snapshot("analyst", "s1", _snapshot_scope())
        analyst = self.service.get_snapshot("analyst", "s1", snap["version"])
        item = next(entry for entry in analyst["items"] if entry["record_id"] == "internal-1")
        self.assertEqual(item["credibility"]["level"], "high")
        self.assertEqual(item["credibility"]["rationale"], "***REDACTED***")
        self.assertEqual(item["credibility"]["evidence_refs"], [])
        record_view = self.service.get_record("analyst", "internal-1")
        self.assertEqual(record_view["annotations"][0]["rationale"], "***REDACTED***")
        auditor = self.service.get_snapshot("auditor", "s1", snap["version"])
        auditor_item = next(entry for entry in auditor["items"] if entry["record_id"] == "internal-1")
        self.assertIn("PK/PD", auditor_item["credibility"]["rationale"])


class DeterminismTests(ServiceTestBase):
    def test_same_inputs_produce_same_digest(self) -> None:
        self.register("r1", stage="phase_2")
        scope = _snapshot_scope()
        first = self.service.create_snapshot("analyst", "s1", scope)
        second = self.service.create_snapshot("analyst", "s2", scope)
        self.assertEqual(first["input_sha256"], second["input_sha256"])
        self.assertEqual(canonical_json(first["result"]), canonical_json(second["result"]))


if __name__ == "__main__":
    unittest.main()
