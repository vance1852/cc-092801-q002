from __future__ import annotations

import unittest
from pathlib import Path

from competitive_intel.acceptance import run


ROOT = Path(__file__).resolve().parents[1]


class CompetitiveAcceptanceTests(unittest.TestCase):
    def test_offline_acceptance(self) -> None:
        result = run(ROOT)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["record_count"], 8)
        self.assertTrue(result["history_immutable"])
        self.assertTrue(result["frozen_asset_still_merged"])
        self.assertTrue(result["split_reflected_in_v2"])
        self.assertTrue(result["asset_count_changed_after_split"])
        self.assertTrue(result["analyst_sensitive_redacted"])
        self.assertTrue(result["role_minimum_disclosure"])
        self.assertEqual(result["judgment"], "follow")
        self.assertGreater(result["uncovered_indication_cells"], 0)
        self.assertIn("stage_evidence_mismatch", result["evidence_gap_kinds"])
        self.assertEqual(result["schema"]["missing_tables"], [])
        self.assertEqual(len(result["locked_input_sha256"]), 64)


if __name__ == "__main__":
    unittest.main()
