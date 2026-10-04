from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone

from track_intel.acceptance import run as acceptance_run
from track_intel.analysis import ALGORITHM_VERSION, compare, derive_groups
from track_intel.api import JsonApplication
from track_intel.clock import FrozenClock
from track_intel.errors import Conflict, Forbidden, NotFound, ValidationFailed
from track_intel.service import TrackIntelService
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

TRACK = {"target": "KRAS G12C", "mechanism": "small-molecule inhibitor", "indication": "NSCLC"}


def record_payload(record_id, *, source_kind="external", phase="phase2", sensitivity="open",
                   features=("oral",), key_experiment=None, sponsor="外部公司", note=None):
    return {
        "record_id": record_id,
        "source_kind": source_kind,
        **TRACK,
        "phase": phase,
        "sponsor": sponsor,
        "sensitivity": sensitivity,
        "clinical_features": list(features),
        "key_experiment": key_experiment,
        "note": note,
    }


class ServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 10, 4, 8, 0, tzinfo=timezone.utc))
        self.service = TrackIntelService(self.connection, self.clock)
        for user_id, role in (
            ("analyst", "analyst"),
            ("reviewer", "reviewer"),
            ("viewer", "viewer"),
            ("auditor", "auditor"),
        ):
            self.service.create_user(user_id, user_id, role)

    def tearDown(self) -> None:
        self.connection.close()

    def seed_track(self) -> None:
        self.service.register_record("analyst", record_payload(
            "int-01", source_kind="internal", phase="phase2", sensitivity="restricted",
            features=("oral", "first-line", "biomarker-g12c"),
            key_experiment={"experiment_ref": "batch-1", "experiment_version": "analysis-1"},
            sponsor="内部项目",
        ))
        self.service.register_record("analyst", record_payload("ext-01", phase="phase3", features=("oral", "second-line")))
        self.service.register_record("analyst", record_payload("ext-02", phase="phase2", features=("oral", "first-line")))
        self.service.register_record("analyst", record_payload("ext-03", phase="phase1", features=("oral",)))


class CompareTests(ServiceTestCase):
    def test_crowding_is_explainable_and_deterministic(self) -> None:
        self.seed_track()
        view = self.service.compare_tracks("analyst", TRACK)
        crowding = view["result"]["crowding"]
        # phase2(内部)=4, phase3=5, phase2=4, phase1=3
        self.assertEqual(crowding["score"], 16)
        self.assertEqual(crowding["band"], "moderate")
        self.assertEqual(sum(f["contribution"] for f in crowding["factors"]), crowding["score"])
        self.assertEqual(crowding["program_count"], 4)
        self.assertEqual(view["algorithm_version"], ALGORITHM_VERSION)
        reviewer_view = self.service.get_snapshot("reviewer", view["snapshot_id"])
        self.assertEqual(reviewer_view["result"]["provenance"]["record_revisions"]["int-01"], 1)

    def test_follow_risk_and_differentiation(self) -> None:
        self.seed_track()
        view = self.service.compare_tracks("analyst", TRACK)
        follow = view["result"]["follow_risk"]
        self.assertEqual(follow["signal"], "contested")
        self.assertEqual(follow["most_advanced_internal_phase"], "phase2")
        self.assertEqual(follow["external_ahead_or_equal"], 2)
        differentiation = view["result"]["differentiation"]
        self.assertEqual([item["feature"] for item in differentiation["uncovered"]], ["biomarker-g12c"])
        contested = {item["feature"]: item["external_programs"] for item in differentiation["contested"]}
        self.assertEqual(contested, {"oral": 3})

    def test_evidence_gaps_flag_missing_and_stale_evidence(self) -> None:
        self.seed_track()
        self.service.add_timeline_event("analyst", "ext-03", {
            "event_date": "2024-06-01", "kind": "conference",
            "summary": "早期会议摘要", "source_ref": "conf://2024",
        })
        view = self.service.compare_tracks("analyst", TRACK)
        rules = {gap["rule"] for gap in view["result"]["evidence_gaps"]}
        self.assertIn("missing_public_timeline", rules)
        self.assertIn("stale_public_signal", rules)
        self.assertIn("unrated_record", rules)
        self.assertIn("unrated_timeline", rules)

    def test_internal_record_without_key_experiment_is_gap(self) -> None:
        self.service.register_record("analyst", record_payload("int-01", source_kind="internal", phase="phase1"))
        view = self.service.compare_tracks("analyst", TRACK)
        rules = {gap["rule"] for gap in view["result"]["evidence_gaps"]}
        self.assertIn("missing_key_experiment", rules)

    def test_empty_track_is_not_found(self) -> None:
        with self.assertRaises(NotFound):
            self.service.compare_tracks("analyst", TRACK)

    def test_selector_filters_by_mechanism_and_indication(self) -> None:
        self.seed_track()
        other = record_payload("ext-other", phase="phase3")
        other["mechanism"] = "antibody"
        self.service.register_record("analyst", other)
        view = self.service.compare_tracks("analyst", TRACK)
        ids = [r["record_id"] for r in view["records"]]
        self.assertNotIn("ext-other", ids)
        wide = self.service.compare_tracks("analyst", {"target": "KRAS G12C"})
        self.assertIn("ext-other", [r["record_id"] for r in wide["records"]])


class SnapshotIntegrityTests(ServiceTestCase):
    def test_decision_locked_snapshot_is_never_rewritten(self) -> None:
        self.seed_track()
        first = self.service.compare_tracks("analyst", TRACK)
        self.service.record_decision("reviewer", first["snapshot_id"], {
            "decision": "advance", "rationale": "差异窗口开放",
        })
        locked = self.service.get_snapshot("reviewer", first["snapshot_id"])
        self.assertEqual(locked["state"], "decision_locked")
        self.assertEqual(len(locked["decisions"]), 1)
        first_full_result = locked["result"]

        # 数据演进：修订记录、合并、标注，只产生新快照修订。
        revised = record_payload(
            "int-01", source_kind="internal", phase="phase3", sensitivity="restricted",
            features=("oral", "first-line", "biomarker-g12c"),
            key_experiment={"experiment_ref": "batch-2", "experiment_version": "analysis-2"},
            sponsor="内部项目",
        )
        self.service.revise_record("analyst", "int-01", revised)
        self.service.merge_records("analyst", {"record_ids": ["ext-01", "ext-02"], "reason": "同源"})
        second = self.service.compare_tracks("analyst", TRACK)
        self.assertEqual(second["revision"], 2)
        self.assertNotEqual(second["input_sha256"], first["input_sha256"])

        reread = self.service.get_snapshot("auditor", first["snapshot_id"])
        self.assertEqual(reread["input_sha256"], first["input_sha256"])
        self.assertEqual(reread["result"], first_full_result)
        self.assertEqual(reread["result"]["provenance"]["record_revisions"]["int-01"], 1)

    def test_replay_returns_same_snapshot_when_inputs_unchanged(self) -> None:
        self.seed_track()
        first = self.service.compare_tracks("analyst", TRACK)
        again = self.service.compare_tracks("analyst", TRACK)
        self.assertFalse(first["replayed"])
        self.assertTrue(again["replayed"])
        self.assertEqual(again["snapshot_id"], first["snapshot_id"])
        count = self.connection.execute("SELECT count(*) FROM snapshots").fetchone()[0]
        self.assertEqual(count, 1)

    def test_database_rejects_snapshot_content_mutation(self) -> None:
        self.seed_track()
        first = self.service.compare_tracks("analyst", TRACK)
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "UPDATE snapshots SET result_json='{}' WHERE snapshot_id=?", (first["snapshot_id"],)
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("DELETE FROM snapshots WHERE snapshot_id=?", (first["snapshot_id"],))
        self.service.record_decision("reviewer", first["snapshot_id"], {"decision": "hold", "rationale": "观察"})
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("UPDATE snapshot_decisions SET decision='advance'")
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("DELETE FROM snapshot_decisions")

    def test_verify_snapshot_replays_stored_manifest(self) -> None:
        self.seed_track()
        first = self.service.compare_tracks("analyst", TRACK)
        verify = self.service.verify_snapshot("auditor", first["snapshot_id"])
        self.assertTrue(verify["valid"])
        self.assertTrue(verify["input_match"])
        self.assertTrue(verify["result_match"])

    def test_decisions_are_append_only_history(self) -> None:
        self.seed_track()
        first = self.service.compare_tracks("analyst", TRACK)
        self.service.record_decision("reviewer", first["snapshot_id"], {"decision": "watch", "rationale": "先观察"})
        self.service.record_decision("reviewer", first["snapshot_id"], {"decision": "advance", "rationale": "窗口确认"})
        view = self.service.get_snapshot("reviewer", first["snapshot_id"])
        self.assertEqual([d["decision"] for d in view["decisions"]], ["watch", "advance"])


class LinkAndAnnotationTests(ServiceTestCase):
    def test_merge_collapses_duplicate_sources_and_split_restores(self) -> None:
        self.seed_track()
        self.service.register_record("analyst", record_payload("ext-02-dup", phase="phase2", features=("oral", "first-line")))
        before = self.service.compare_tracks("analyst", TRACK)
        self.assertEqual(before["result"]["crowding"]["program_count"], 5)
        merged = self.service.merge_records("analyst", {
            "record_ids": ["ext-02", "ext-02-dup"], "reason": "同一项目两次登记",
        })
        after = self.service.compare_tracks("analyst", TRACK)
        self.assertEqual(after["result"]["crowding"]["program_count"], 4)
        self.assertEqual(after["result"]["crowding"]["score"], before["result"]["crowding"]["score"] - 4)
        merged_programs = [p for p in after["result"]["programs"] if p["merged"]]
        self.assertEqual(len(merged_programs), 1)
        self.assertEqual(merged_programs[0]["member_record_ids"], ["ext-02", "ext-02-dup"])

        self.service.split_record("analyst", "ext-02-dup", "复核后确认是不同项目")
        restored = self.service.compare_tracks("analyst", TRACK)
        self.assertEqual(restored["result"]["crowding"]["program_count"], 5)

    def test_merge_requires_existing_records_and_distinct_groups(self) -> None:
        self.seed_track()
        with self.assertRaises(NotFound):
            self.service.merge_records("analyst", {"record_ids": ["ext-01", "ext-x"], "reason": "x"})
        self.service.merge_records("analyst", {"record_ids": ["ext-01", "ext-02"], "reason": "同源"})
        with self.assertRaises(Conflict):
            self.service.merge_records("analyst", {"record_ids": ["ext-01", "ext-02"], "reason": "重复"})
        with self.assertRaises(Conflict):
            self.service.split_record("analyst", "ext-03", "不在组中")
        with self.assertRaises(ValidationFailed):
            self.service.merge_records("analyst", {"record_ids": ["ext-01"], "reason": "只有一条"})

    def test_derive_groups_replays_event_log(self) -> None:
        events = [
            {"action": "merge", "group_id": "g1", "record_ids": ["a", "b"]},
            {"action": "merge", "group_id": "g2", "record_ids": ["b", "c"]},
            {"action": "split", "group_id": "g2", "record_ids": ["c"]},
        ]
        self.assertEqual(derive_groups(events), {"a": "g1", "b": "g2"})

    def test_credibility_annotation_latest_wins_and_feeds_gaps(self) -> None:
        self.seed_track()
        event = self.service.add_timeline_event("analyst", "ext-01", {
            "event_date": "2026-05-01", "kind": "press_release",
            "summary": "新闻稿", "source_ref": "press://1",
        })
        self.service.annotate_credibility("analyst", {
            "subject_type": "timeline_event", "subject_id": str(event["event_id"]),
            "level": "low", "rationale": "仅新闻稿",
        })
        view = self.service.compare_tracks("analyst", TRACK)
        self.assertIn("low_credibility_timeline", {g["rule"] for g in view["result"]["evidence_gaps"]})
        self.service.annotate_credibility("analyst", {
            "subject_type": "timeline_event", "subject_id": str(event["event_id"]),
            "level": "high", "rationale": "找到登记佐证",
        })
        updated = self.service.compare_tracks("analyst", TRACK)
        rules = {g["rule"] for g in updated["result"]["evidence_gaps"]}
        self.assertNotIn("low_credibility_timeline", rules)
        count = self.connection.execute("SELECT count(*) FROM credibility_annotations").fetchone()[0]
        self.assertEqual(count, 2)

    def test_annotation_requires_existing_subject(self) -> None:
        self.seed_track()
        with self.assertRaises(NotFound):
            self.service.annotate_credibility("analyst", {
                "subject_type": "record", "subject_id": "ext-x", "level": "low", "rationale": "x",
            })
        with self.assertRaises(NotFound):
            self.service.annotate_credibility("analyst", {
                "subject_type": "timeline_event", "subject_id": "999", "level": "low", "rationale": "x",
            })


class MaskingTests(ServiceTestCase):
    def test_viewer_sees_masked_identity_and_no_experiment_detail(self) -> None:
        self.seed_track()
        view = self.service.compare_tracks("analyst", TRACK)
        viewer_view = self.service.get_snapshot("viewer", view["snapshot_id"])
        masked = [r for r in viewer_view["records"] if r["masked"]]
        self.assertEqual(len(masked), 1)
        self.assertEqual(masked[0]["sponsor"], "已脱敏")
        self.assertIsNone(masked[0]["key_experiment"])
        self.assertTrue(masked[0]["record_id"].startswith("masked-"))
        # 脱敏视图仍保留拥挤度与差异结论，且化名在结果内一致。
        self.assertEqual(viewer_view["result"]["crowding"]["score"], view["result"]["crowding"]["score"])
        pseudonym = masked[0]["record_id"]
        revisions = viewer_view["result"]["provenance"]["record_revisions"]
        self.assertIn(pseudonym, revisions)
        self.assertNotIn("int-01", revisions)

    def test_reviewer_sees_full_identity(self) -> None:
        self.seed_track()
        view = self.service.compare_tracks("analyst", TRACK)
        reviewer_view = self.service.get_snapshot("reviewer", view["snapshot_id"])
        internal = [r for r in reviewer_view["records"] if r["record_id"] == "int-01"]
        self.assertEqual(internal[0]["sponsor"], "内部项目")
        self.assertEqual(internal[0]["key_experiment"]["experiment_version"], "analysis-1")

    def test_sensitive_timeline_details_hidden_from_viewer(self) -> None:
        self.seed_track()
        self.service.add_timeline_event("analyst", "int-01", {
            "event_date": "2026-09-01", "kind": "regulatory",
            "summary": "内部沟通纪要", "source_ref": "internal://doc-9",
        })
        record = self.service.get_record("viewer", "int-01")
        self.assertEqual(record["timeline"], [{"event_date": "2026-09-01", "kind": "regulatory", "credibility": "unrated"}])
        full = self.service.get_record("reviewer", "int-01")
        self.assertEqual(full["timeline"][0]["summary"], "内部沟通纪要")


class PermissionTests(ServiceTestCase):
    def test_role_separation(self) -> None:
        self.seed_track()
        with self.assertRaises(Forbidden):
            self.service.compare_tracks("viewer", TRACK)
        with self.assertRaises(Forbidden):
            self.service.register_record("reviewer", record_payload("ext-09"))
        with self.assertRaises(Forbidden):
            self.service.annotate_credibility("viewer", {
                "subject_type": "record", "subject_id": "ext-01", "level": "low", "rationale": "x",
            })
        view = self.service.compare_tracks("analyst", TRACK)
        with self.assertRaises(Forbidden):
            self.service.record_decision("analyst", view["snapshot_id"], {"decision": "advance", "rationale": "x"})
        with self.assertRaises(Forbidden):
            self.service.verify_snapshot("viewer", view["snapshot_id"])
        with self.assertRaises(Forbidden):
            self.service.audit_events("analyst", "snapshot", "1")

    def test_audit_trail_records_governance_events(self) -> None:
        self.seed_track()
        self.service.merge_records("analyst", {"record_ids": ["ext-01", "ext-02"], "reason": "同源"})
        view = self.service.compare_tracks("analyst", TRACK)
        self.service.record_decision("reviewer", view["snapshot_id"], {"decision": "advance", "rationale": "窗口开放"})
        events = self.service.audit_events("auditor", "snapshot", str(view["snapshot_id"]))
        self.assertEqual([e["event_type"] for e in events], ["snapshot.created", "snapshot.decision_recorded"])
        record_events = self.service.audit_events("auditor", "record", "int-01")
        self.assertEqual(record_events[0]["event_type"], "record.registered")


class ValidationTests(ServiceTestCase):
    def test_record_contract_enforced(self) -> None:
        bad = record_payload("ext-bad", phase="phase4")
        with self.assertRaises(ValidationFailed):
            self.service.register_record("analyst", bad)
        bad2 = record_payload("ext-bad2", sensitivity="secret")
        with self.assertRaises(ValidationFailed):
            self.service.register_record("analyst", bad2)
        with self.assertRaises(Conflict):
            self.service.register_record("analyst", record_payload("ext-dup"))
            self.service.register_record("analyst", record_payload("ext-dup"))

    def test_revision_history_is_kept(self) -> None:
        self.service.register_record("analyst", record_payload("ext-01", phase="phase1"))
        self.service.revise_record("analyst", "ext-01", record_payload("ext-01", phase="phase2"))
        record = self.service.get_record("reviewer", "ext-01")
        self.assertEqual(record["revision"], 2)
        self.assertEqual(record["phase"], "phase2")
        count = self.connection.execute(
            "SELECT count(*) FROM pipeline_record_versions WHERE record_id='ext-01'"
        ).fetchone()[0]
        self.assertEqual(count, 2)
        with self.assertRaises(NotFound):
            self.service.revise_record("analyst", "ext-x", record_payload("ext-x"))


class ApiTests(ServiceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.app = JsonApplication(self.service)

    def test_health(self) -> None:
        response = self.app.handle("GET", "/health")
        self.assertEqual(response.status, 200)

    def test_missing_actor_is_rejected(self) -> None:
        response = self.app.handle("POST", "/records", body=b"{}")
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["code"], "validation_failed")

    def test_compare_flow_over_http(self) -> None:
        self.seed_track()
        headers = {"X-Actor-Id": "analyst"}
        response = self.app.handle("POST", "/tracks/compare", headers, json.dumps(TRACK).encode())
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["result"]["crowding"]["band"], "moderate")
        snapshot_id = response.body["snapshot_id"]
        decision = self.app.handle(
            "POST", f"/snapshots/{snapshot_id}/decisions", {"X-Actor-Id": "reviewer"},
            json.dumps({"decision": "advance", "rationale": "窗口开放"}).encode(),
        )
        self.assertEqual(decision.status, 201)
        fetched = self.app.handle("GET", f"/snapshots/{snapshot_id}", {"X-Actor-Id": "viewer"})
        self.assertEqual(fetched.status, 200)
        self.assertEqual(fetched.body["state"], "decision_locked")
        self.assertTrue(any(r["masked"] for r in fetched.body["records"]))
        verify = self.app.handle("GET", f"/snapshots/{snapshot_id}/verify", {"X-Actor-Id": "auditor"})
        self.assertTrue(verify.body["valid"])
        forbidden = self.app.handle("GET", f"/snapshots/{snapshot_id}/verify", {"X-Actor-Id": "viewer"})
        self.assertEqual(forbidden.status, 403)


class AcceptanceTests(unittest.TestCase):
    def test_offline_acceptance(self) -> None:
        result = acceptance_run(ROOT)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["crowding"], "moderate")
        self.assertEqual(result["follow_risk"], "contested")
        self.assertEqual(result["uncovered_features"], ["biomarker-g12c"])
        self.assertEqual(result["snapshot_v1"]["state"], "decision_locked")
        self.assertEqual(result["snapshot_v2"]["revision"], 2)
        self.assertTrue(result["replayed_same_snapshot"])
        self.assertTrue(result["verify_valid"])
        self.assertEqual(result["decision"], "advance")
        self.assertEqual(result["viewer_masked_records"], 1)
        self.assertEqual(result["schema"]["missing_tables"], [])
        self.assertIn("stale_public_signal", result["evidence_gap_rules"])
        self.assertIn("low_credibility_timeline", result["evidence_gap_rules"])


if __name__ == "__main__":
    unittest.main()
