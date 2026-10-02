"""Stdlib-only tests for the FPSO slicer filter check."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from iw29_export import pbi_filter_check  # noqa: E402


def _write_dataset(folder: Path, fact_rows, lookup_rows=None, mirror=True):
    dataset = folder / "dataset"
    dataset.mkdir(parents=True, exist_ok=True)
    fact_fields = [
        "Site", "ItemClass", "WorkOrder", "StatusPrimary", "SECE",
        "BacklogBucket", "IsCompleted", "IsBacklog",
    ]
    with (dataset / "FPSO_wo_fact.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as handle:
        import csv

        writer = csv.DictWriter(handle, fieldnames=fact_fields)
        writer.writeheader()
        writer.writerows(fact_rows)
    if lookup_rows is not None:
        with (dataset / "FPSO_item_class_lookup.csv").open(
            "w", encoding="utf-8-sig", newline=""
        ) as handle:
            import csv

            writer = csv.DictWriter(
                handle, fieldnames=["Site", "Order", "ItemClass"]
            )
            writer.writeheader()
            writer.writerows(lookup_rows)
    if mirror:
        import shutil

        shutil.copyfile(
            dataset / "FPSO_wo_fact.csv", dataset / "CLV_wo_fact.csv"
        )
    return dataset


GOOD_FACT = [
    {
        "Site": "GIR", "ItemClass": "Campaign", "WorkOrder": "73299999",
        "StatusPrimary": "QCAP", "SECE": "SECE", "BacklogBucket": "",
        "IsCompleted": "True", "IsBacklog": "False",
    },
    {
        "Site": "GIR", "ItemClass": "FU Items", "WorkOrder": "73166043",
        "StatusPrimary": "INIT", "SECE": "NON SECE",
        "BacklogBucket": "< 6 Months",
        "IsCompleted": "False", "IsBacklog": "True",
    },
]

GOOD_LOOKUP = [
    {"Site": "GIR", "Order": "73299999", "ItemClass": "Campaign"},
    {"Site": "GIR", "Order": "73166043", "ItemClass": "FU Items"},
]


class FilterCheckTests(unittest.TestCase):
    def test_clean_dataset_passes(self):
        with tempfile.TemporaryDirectory() as scratch:
            dataset = _write_dataset(
                Path(scratch), GOOD_FACT, GOOD_LOOKUP, mirror=True
            )
            audit = pbi_filter_check.audit_dataset(dataset)
            self.assertTrue(audit.ok, audit.issues)
            self.assertEqual(audit.fact_rows, 2)

    def test_trailing_space_and_case_split_are_flagged(self):
        fact = [
            dict(GOOD_FACT[0], ItemClass="Campaign "),
            dict(GOOD_FACT[1], ItemClass="campaign"),
        ]
        with tempfile.TemporaryDirectory() as scratch:
            dataset = _write_dataset(
                Path(scratch), fact, GOOD_LOOKUP, mirror=True
            )
            audit = pbi_filter_check.audit_dataset(dataset)
            self.assertFalse(audit.ok)
            text = "\n".join(audit.issues)
            self.assertIn("leading/trailing spaces", text)
            self.assertIn("case variants", text)

    def test_blank_class_and_orphan_lookup_are_flagged(self):
        fact = [dict(GOOD_FACT[0], ItemClass="")]
        lookup = [{"Site": "GIR", "Order": "73299999", "ItemClass": "Other"}]
        with tempfile.TemporaryDirectory() as scratch:
            dataset = _write_dataset(
                Path(scratch), fact, lookup, mirror=True
            )
            audit = pbi_filter_check.audit_dataset(dataset)
            self.assertFalse(audit.ok)
            text = "\n".join(audit.issues)
            self.assertIn("blank ItemClass", text)

    def test_stale_clv_mirror_is_flagged(self):
        with tempfile.TemporaryDirectory() as scratch:
            dataset = _write_dataset(
                Path(scratch), GOOD_FACT, GOOD_LOOKUP, mirror=False
            )
            (dataset / "CLV_wo_fact.csv").write_text(
                "Site,ItemClass,WorkOrder\nGIR,Campaign,73299999\n",
                encoding="utf-8-sig",
            )
            audit = pbi_filter_check.audit_dataset(dataset)
            self.assertFalse(audit.ok)
            self.assertTrue(
                any("CLV_wo_fact" in issue for issue in audit.issues)
            )

    def test_missing_fact_reports_issue(self):
        with tempfile.TemporaryDirectory() as scratch:
            dataset = Path(scratch) / "dataset"
            dataset.mkdir()
            audit = pbi_filter_check.audit_dataset(dataset)
            self.assertFalse(audit.ok)
            self.assertIn("fact missing", audit.issues[0])


if __name__ == "__main__":
    unittest.main()
