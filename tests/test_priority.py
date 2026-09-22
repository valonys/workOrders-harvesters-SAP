"""Priority Harmonization Engine tests. Stdlib only."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from iw29_export import convert, pipeline, priority  # noqa: E402
from iw29_export.config import Config  # noqa: E402

from test_app import _write_config  # noqa: E402

BASE_DESCRIPTION = "S/PI/TBR1/FZ28/NSD/GI13-305/LEAK/CI/SK/2"


class ExtractTests(unittest.TestCase):
    def test_base_case_last_token_is_inspector_priority_2(self):
        result = priority.harmonize(BASE_DESCRIPTION, "Intermediate")
        self.assertEqual(result.inspector_priority, 2)
        self.assertEqual(result.priority_final, 2)
        self.assertEqual(result.sap_priority, "Intermediate")
        self.assertEqual(result.sap_rank, 3)
        self.assertEqual(result.mismatch, "Yes")
        self.assertEqual(result.variance, -1)
        self.assertEqual(result.source, "Inspection Naming Convention")
        self.assertEqual(result.confidence, "High")
        self.assertEqual(result.fire_zone, "FZ28")
        self.assertEqual(result.parse_status, "Parsed")

    def test_priorities_one_through_five(self):
        for rank in range(1, 6):
            description = f"S/PI/TBR1/FZ28/NSD/GI13-305/LEAK/CI/SK/{rank}"
            self.assertEqual(priority.extract_inspector_priority(description), rank)

    def test_hyphenated_tag_is_not_the_priority(self):
        self.assertEqual(priority.extract_inspector_priority(BASE_DESCRIPTION), 2)

    def test_backslash_separators(self):
        description = r"S\PI\TBR1\FZ28\NSD\GI13-305\LEAK\CI\SK\1"
        self.assertEqual(priority.extract_inspector_priority(description), 1)

    def test_blank_description_does_not_invent_a_priority(self):
        result = priority.harmonize("   ", "Intermediate")
        self.assertIsNone(result.priority_final)
        self.assertEqual(result.parse_status, "DescriptionBlank")
        self.assertEqual(result.sap_rank, 3)

    def test_unstructured_text_leaves_priority_final_blank(self):
        result = priority.harmonize("Leaking flange on line 12", "Urgent")
        self.assertIsNone(result.inspector_priority)
        self.assertIsNone(result.priority_final)
        self.assertEqual(result.parse_status, "LastTokenNotNumeric")
        self.assertEqual(result.source, "Unparsed")

    def test_out_of_range_last_token(self):
        result = priority.harmonize("S/PI/TBR1/FZ1/NSD/GI1-1/LEAK/CI/SK/9")
        self.assertIsNone(result.priority_final)
        self.assertEqual(result.parse_status, "PriorityOutOfRange")

    def test_short_slash_path_is_medium_confidence(self):
        result = priority.harmonize("LEAK/2", "Urgent")
        self.assertEqual(result.priority_final, 2)
        self.assertEqual(result.confidence, "Medium")
        self.assertEqual(result.naming_valid, "No")
        self.assertEqual(result.mismatch, "No")


class SapRankTests(unittest.TestCase):
    def test_intermediate_is_rank_three(self):
        self.assertEqual(priority.map_sap_priority("Intermediate"), 3)

    def test_numeric_and_label_aliases(self):
        self.assertEqual(priority.map_sap_priority(1), 1)
        self.assertEqual(priority.map_sap_priority("Urgent"), 2)
        self.assertEqual(priority.map_sap_priority("low"), 4)
        self.assertEqual(priority.map_sap_priority("Immediate"), 1)

    def test_aligned_ranks_are_not_a_mismatch(self):
        result = priority.harmonize(BASE_DESCRIPTION.replace("/2", "/3"), "Intermediate")
        self.assertEqual(result.inspector_priority, 3)
        self.assertEqual(result.mismatch, "No")
        self.assertEqual(result.variance, 0)


class EnrichTests(unittest.TestCase):
    def test_enrich_appends_columns_and_keeps_sap_priority(self):
        table = convert.Table(
            headers=["Notification", "Description", "Priority"],
            rows=[["43014305", BASE_DESCRIPTION, "Intermediate"]],
        )
        enriched = priority.enrich(table)
        self.assertIn("Priority_Final", enriched.headers)
        self.assertEqual(enriched.headers[:3], ["Notification", "Description", "Priority"])
        row = dict(zip(enriched.headers, enriched.rows[0]))
        self.assertEqual(row["Notification"], "43014305")
        self.assertEqual(row["Priority"], "Intermediate")
        self.assertEqual(row["InspectorPriority"], 2)
        self.assertEqual(row["Priority_Final"], 2)
        self.assertEqual(row["PriorityMismatch"], "Yes")
        self.assertEqual(row["FireZone"], "FZ28")

    def test_enrich_is_idempotent(self):
        table = convert.Table(
            headers=["Notification", "Description", "Priority"],
            rows=[["43014305", BASE_DESCRIPTION, "Intermediate"]],
        )
        once = priority.enrich(table)
        twice = priority.enrich(once)
        self.assertEqual(once.headers, twice.headers)
        self.assertEqual(once.rows, twice.rows)
        self.assertEqual(twice.headers.count("Priority_Final"), 1)

    def test_missing_description_column_still_emits_schema(self):
        table = convert.Table(headers=["Notification"], rows=[["43014305"]])
        enriched = priority.enrich(table)
        row = dict(zip(enriched.headers, enriched.rows[0]))
        self.assertIsNone(row["Priority_Final"])
        self.assertEqual(row["ParseStatus"], "DescriptionColumnMissing")


class PipelineTests(unittest.TestCase):
    def test_mock_run_publishes_priority_final_for_base_case(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            config = Config.load(_write_config(root))
            config = config.with_overrides(
                **{"export.staging_folder": root / "staging"}
            )
            result = pipeline.run(config)
            header = result.dataset_file.read_text(encoding="utf-8-sig").splitlines()[0]
            self.assertIn("Priority_Final", header)
            self.assertIn("InspectorPriority", header)
            self.assertIn("PriorityMismatch", header)

            table = convert.read_xlsx(result.workbook)
            row = dict(zip(table.headers, table.rows[0]))
            self.assertEqual(str(row["Notification"]), "43014305")
            self.assertEqual(row["Description"], BASE_DESCRIPTION)
            self.assertEqual(row["Priority_Final"], 2)
            self.assertEqual(row["SAPPriority"], "Intermediate")
            self.assertEqual(row["PriorityMismatch"], "Yes")


if __name__ == "__main__":
    unittest.main()
