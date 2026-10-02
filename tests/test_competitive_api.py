from __future__ import annotations

import json
import sqlite3
import unittest

from competitive_intel.api import JsonApplication
from competitive_intel.service import CompetitiveIntelService


def _body(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.app = JsonApplication(CompetitiveIntelService(self.connection))
        for user_id, role in (
            ("analyst", "analyst"),
            ("reviewer", "reviewer"),
            ("committee", "committee"),
            ("auditor", "auditor"),
        ):
            self.app.handle(
                "POST",
                "/users",
                body=_body({"user_id": user_id, "display_name": user_id, "role": role}),
            )
        self.record = {
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

    def tearDown(self) -> None:
        self.connection.close()

    def test_health(self) -> None:
        response = self.app.handle("GET", "/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["status"], "ok")
        self.assertEqual(response.body["schema_version"], "1")

    def test_requires_actor_header(self) -> None:
        response = self.app.handle("POST", "/records", body=_body({"record_id": "r1", **self.record}))
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["code"], "validation_failed")

    def test_full_snapshot_workflow_over_http(self) -> None:
        created = self.app.handle(
            "POST",
            "/records",
            headers={"X-Actor-Id": "analyst"},
            body=_body({"record_id": "r1", **self.record}),
        )
        self.assertEqual(created.status, 201)
        self.assertEqual(created.body["version"], 1)

        snapshot = self.app.handle(
            "POST",
            "/snapshots",
            headers={"X-Actor-Id": "analyst"},
            body=_body(
                {
                    "snapshot_id": "s1",
                    "scope": {"name": "TL1A 评审", "as_of": "2026-09-30", "target": "TL1A"},
                }
            ),
        )
        self.assertEqual(snapshot.status, 201)
        self.assertEqual(snapshot.body["status"], "draft")

        publish = self.app.handle(
            "POST",
            "/snapshots/s1/publish",
            headers={"X-Actor-Id": "reviewer"},
            body=_body({"version": 1}),
        )
        self.assertEqual(publish.status, 200)
        self.assertEqual(publish.body["status"], "published")

        judgment = self.app.handle(
            "POST",
            "/judgments",
            headers={"X-Actor-Id": "committee"},
            body=_body(
                {
                    "judgment_id": "j1",
                    "snapshot_id": "s1",
                    "snapshot_version": 1,
                    "decision": "proceed",
                    "rationale": "差异化窗口明确",
                }
            ),
        )
        self.assertEqual(judgment.status, 201)

        fetched = self.app.handle("GET", "/snapshots/s1", headers={"X-Actor-Id": "auditor"})
        self.assertEqual(fetched.status, 200)
        self.assertEqual(fetched.body["status"], "locked")
        self.assertEqual(fetched.body["judgment"]["decision"], "proceed")

    def test_analyst_forbidden_from_publish(self) -> None:
        self.app.handle(
            "POST",
            "/records",
            headers={"X-Actor-Id": "analyst"},
            body=_body({"record_id": "r1", **self.record}),
        )
        self.app.handle(
            "POST",
            "/snapshots",
            headers={"X-Actor-Id": "analyst"},
            body=_body(
                {
                    "snapshot_id": "s1",
                    "scope": {"name": "x", "as_of": "2026-09-30", "target": "TL1A"},
                }
            ),
        )
        response = self.app.handle(
            "POST",
            "/snapshots/s1/publish",
            headers={"X-Actor-Id": "analyst"},
            body=_body({"version": 1}),
        )
        self.assertEqual(response.status, 403)
        self.assertEqual(response.body["error"]["code"], "forbidden")

    def test_merge_and_unmerge_routes(self) -> None:
        for record_id in ("r1", "r2"):
            response = self.app.handle(
                "POST",
                "/records",
                headers={"X-Actor-Id": "analyst"},
                body=_body({"record_id": record_id, **self.record}),
            )
            self.assertEqual(response.status, 201)
        created = self.app.handle(
            "POST",
            "/assets",
            headers={"X-Actor-Id": "analyst"},
            body=_body({"asset_id": "a1", "record_ids": ["r1", "r2"], "reason": "同源"}),
        )
        self.assertEqual(created.status, 201)
        unmerged = self.app.handle(
            "POST",
            "/assets/unmerge",
            headers={"X-Actor-Id": "analyst"},
            body=_body({"record_id": "r2", "reason": "误关联"}),
        )
        self.assertEqual(unmerged.status, 200)
        self.assertEqual(unmerged.body["status"], "revoked")

    def test_credibility_route(self) -> None:
        self.app.handle(
            "POST",
            "/records",
            headers={"X-Actor-Id": "analyst"},
            body=_body({"record_id": "r1", **self.record}),
        )
        response = self.app.handle(
            "POST",
            "/credibility_annotations",
            headers={"X-Actor-Id": "analyst"},
            body=_body(
                {
                    "record_id": "r1",
                    "record_version": 1,
                    "level": "high",
                    "rationale": "注册库可核验",
                    "evidence_refs": ["https://example.org/t1"],
                }
            ),
        )
        self.assertEqual(response.status, 201)
        self.assertEqual(response.body["level"], "high")

    def test_record_version_route(self) -> None:
        self.app.handle(
            "POST",
            "/records",
            headers={"X-Actor-Id": "analyst"},
            body=_body({"record_id": "r1", **self.record}),
        )
        response = self.app.handle(
            "GET", "/records/r1/versions/1", headers={"X-Actor-Id": "analyst"}
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["content"]["stage"], "phase_2")

    def test_unknown_route(self) -> None:
        response = self.app.handle("GET", "/nope")
        self.assertEqual(response.status, 404)

    def test_bad_json(self) -> None:
        response = self.app.handle(
            "POST",
            "/records",
            headers={"X-Actor-Id": "analyst"},
            body=b"not-json",
        )
        self.assertEqual(response.status, 422)


if __name__ == "__main__":
    unittest.main()
