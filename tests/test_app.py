"""Stdlib-only tests: python -m unittest discover -s tests"""

from __future__ import annotations

import sys
import tempfile
import unittest
import zipfile
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from iw29_export import convert, pipeline, xlsx  # noqa: E402
from iw29_export.config import Config  # noqa: E402
from iw29_export.errors import ConfigError  # noqa: E402

MOCK_CONFIG = """
[sap]
system = "FR3"

[selection]
transaction = "IW29"
lookback_days = 14

[[selection.filters]]
field = "SWERK"
values = ["1000"]

[[selection.filters]]
field = "ARBPL"
values = ["MECH01", "ELEC02"]

[export]
folder = '{folder}'
filename_pattern = "IW29_{{system}}_{{timestamp:%Y%m%d}}.xlsx"

[archive]
enabled = true
archive_after_days = 7

[dataset]
enabled = true

[runtime]
mock = true
log_folder = '{logs}'
"""


def _write_config(folder: Path) -> Path:
    export_folder = folder / "sync"
    config_path = folder / "config.toml"
    config_path.write_text(
        MOCK_CONFIG.format(folder=export_folder, logs=folder / "logs"),
        encoding="utf-8",
    )
    return config_path


class ConfigTests(unittest.TestCase):
    def test_example_config_is_valid(self):
        config = Config.load(PROJECT_ROOT / "config.example.toml")
        self.assertEqual(config.sap.system, "FR3")
        self.assertEqual(config.selection.transaction, "IW29")
        self.assertTrue(any(f.field_name == "ARBPL" for f in config.selection.filters))

    def test_dates_default_to_lookback_window(self):
        with tempfile.TemporaryDirectory() as scratch:
            config = Config.load(_write_config(Path(scratch)))
        date_from, date_to = config.selection.resolved_dates(date(2026, 7, 30))
        self.assertEqual(date_to, "30.07.2026")
        self.assertEqual(date_from, "16.07.2026")

    def test_bad_export_mode_is_rejected(self):
        with tempfile.TemporaryDirectory() as scratch:
            config = Config.load(_write_config(Path(scratch)))
            with self.assertRaises(ConfigError):
                config.with_overrides(**{"export.mode": "csv"})

    def test_filename_pattern_renders(self):
        with tempfile.TemporaryDirectory() as scratch:
            config = Config.load(_write_config(Path(scratch)))
        from datetime import datetime

        name = config.export.render_filename("FR3", datetime(2026, 7, 30, 6, 0))
        self.assertEqual(name, "IW29_FR3_20260730.xlsx")


class CoerceTests(unittest.TestCase):
    def test_sap_date(self):
        self.assertEqual(convert.coerce("30.07.2026"), date(2026, 7, 30))

    def test_european_decimal(self):
        self.assertEqual(convert.coerce("1.234,56"), 1234.56)

    def test_us_decimal(self):
        self.assertEqual(convert.coerce("1,234.56"), 1234.56)

    def test_trailing_minus(self):
        self.assertEqual(convert.coerce("42,50-"), -42.5)

    def test_leading_zero_stays_text(self):
        self.assertEqual(convert.coerce("000123"), "000123")

    def test_blank_becomes_none(self):
        self.assertIsNone(convert.coerce("   "))

    def test_text_is_trimmed(self):
        self.assertEqual(convert.coerce("  Pump fault "), "Pump fault")


class ParseTests(unittest.TestCase):
    def test_tab_delimited(self):
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "list.txt"
            path.write_text(
                "Notification\tCosts\tCreated\n"
                "10200001\t1.234,56\t30.07.2026\n"
                "10200002\t99,00\t29.07.2026\n",
                encoding="utf-8",
            )
            table = convert.read_sap_text(path)
        self.assertEqual(table.headers, ["Notification", "Costs", "Created"])
        self.assertEqual(table.row_count, 2)
        self.assertEqual(table.rows[0][1], 1234.56)
        self.assertEqual(table.rows[1][2], date(2026, 7, 29))

    def test_pipe_framed_list_with_rulers(self):
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "list.txt"
            path.write_text(
                "-------------------------\n"
                "|Notification|Work Ctr  |\n"
                "-------------------------\n"
                "|10200001    |MECH01    |\n"
                "-------------------------\n",
                encoding="utf-8",
            )
            table = convert.read_sap_text(path)
        self.assertEqual(table.headers, ["Notification", "Work Ctr"])
        self.assertEqual(table.rows, [[10200001, "MECH01"]])

    def test_iw29_download_shape(self):
        """What IW29 'Text with Tabs' actually produces: a page title, blank
        lines, then a header and rows that all start with a tab."""
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "list.txt"
            path.write_text(
                "30.07.2026          Dynamic List Display                    1\n"
                "\n"
                "\n"
                "\tP\tTyp\tCreated On\tMessage\n"
                "\n"
                "\t3\tNC\t07.10.2010\t13073653\n"
                "\t2\tNC\t27.01.2011\t13076382\n",
                encoding="utf-8",
            )
            table = convert.read_sap_text(path)
        self.assertEqual(table.headers, ["P", "Typ", "Created On", "Message"])
        self.assertEqual(table.row_count, 2)
        self.assertEqual(table.rows[0], [3, "NC", date(2010, 10, 7), 13073653])

    def test_repeated_page_header_is_dropped(self):
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "list.txt"
            path.write_text(
                "Notification\tWork Ctr\n"
                "10200001\tMECH01\n"
                "Notification\tWork Ctr\n"
                "10200002\tMECH02\n",
                encoding="utf-8",
            )
            table = convert.read_sap_text(path)
        self.assertEqual(table.row_count, 2)
        self.assertEqual(table.rows[1], [10200002, "MECH02"])

    def test_duplicate_headers_are_disambiguated(self):
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "list.txt"
            path.write_text("Status\tStatus\nA\tB\n", encoding="utf-8")
            table = convert.read_sap_text(path)
        self.assertEqual(table.headers, ["Status", "Status (2)"])


class XlsxTests(unittest.TestCase):
    def test_workbook_is_a_valid_package(self):
        table = convert.Table(
            headers=["Notification", "Created", "Costs", "Note"],
            rows=[
                [10200001, date(2026, 7, 30), 1234.56, 'Leak & <check> "now"'],
                [10200002, None, None, None],
            ],
        )
        with tempfile.TemporaryDirectory() as scratch:
            target = Path(scratch) / "out.xlsx"
            convert.write_xlsx(table, target, "IW29")
            with zipfile.ZipFile(target) as archive:
                self.assertIsNone(archive.testzip())
                names = set(archive.namelist())
                self.assertIn("xl/worksheets/sheet1.xml", names)
                self.assertIn("xl/styles.xml", names)
                sheet = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")
        self.assertIn("Notification", sheet)
        self.assertIn("Leak &amp; &lt;check&gt;", sheet)
        self.assertIn('<autoFilter ref="A1:D3"/>', sheet)
        serial = (date(2026, 7, 30) - date(1899, 12, 30)).days
        self.assertIn(f"<v>{serial}</v>", sheet)

    def test_column_letters(self):
        self.assertEqual(xlsx.column_letter(1), "A")
        self.assertEqual(xlsx.column_letter(26), "Z")
        self.assertEqual(xlsx.column_letter(27), "AA")


class PipelineTests(unittest.TestCase):
    def test_mock_run_produces_workbook_and_dataset(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            config = Config.load(_write_config(root))
            config = config.with_overrides(
                **{"export.staging_folder": root / "staging"}
            )
            result = pipeline.run(config)

            self.assertIsNotNone(result.workbook)
            self.assertTrue(result.workbook.is_file())
            self.assertGreater(result.row_count, 0)
            self.assertEqual(result.source, "mock")
            self.assertTrue(result.dataset_file.is_file())

            with zipfile.ZipFile(result.workbook) as archive:
                self.assertIsNone(archive.testzip())

            dataset_text = result.dataset_file.read_text(encoding="utf-8-sig")
            self.assertIn("run_timestamp", dataset_text.splitlines()[0])
            self.assertEqual(len(dataset_text.strip().splitlines()), result.row_count + 1)

            leftovers = list((root / "sync").glob("~$*"))
            self.assertEqual(leftovers, [], "temporary files should not be left behind")


if __name__ == "__main__":
    unittest.main()
