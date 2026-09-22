"""The boundary between "where the data comes from" and everything downstream."""

from __future__ import annotations

import csv
import random
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, List, Optional

ProgressFn = Callable[[str], None]


@dataclass
class Extract:
    """A raw file as SAP handed it over, before we make it presentable."""

    path: Path
    kind: str  # "text" (tab delimited) or "xlsx"
    row_count: Optional[int] = None
    columns: List[str] = field(default_factory=list)
    reported_by_sap: Optional[int] = None


class ReportSource:
    """Interface implemented by the real SAP driver and by the mock."""

    name = "source"

    def extract(self, staging_dir: Path, progress: ProgressFn) -> Extract:
        raise NotImplementedError


_MOCK_COLUMNS = [
    "Notification",
    "Notification Type",
    "Description",
    "Maintenance Plant",
    "Main WorkCtr",
    "Priority",
    "Created On",
    "Required End",
    "System Status",
    "Reported By",
    "Equipment",
    "Functional Location",
    "Costs (EUR)",
]

_MOCK_WORK_CENTERS = ["MECH01", "ELEC02", "INST03", "UTIL04"]
_MOCK_TYPES = ["M1", "M2", "M3"]
_MOCK_STATUSES = ["OSNO", "NOPR OSNO", "NOCO", "OSNO MPLA"]
_MOCK_ASSETS = ["TBR1", "GIR1", "DAL1", "PAZ1", "CLV1"]
_MOCK_DEFECTS = ["LEAK", "CORR", "THIN", "CRACK", "PIT"]
_MOCK_SAP_LABELS = {
    1: "Immediate",
    2: "Urgent",
    3: "Intermediate",
    4: "Low",
    5: "Very low",
}
_MOCK_TEXTS = [
    "Pump vibration above limit",
    "Conveyor belt misalignment",
    "Leaking flange on line 12",
    "Motor overheating during ramp-up",
    "Sensor reading drifts after CIP",
    "Guard door interlock intermittent",
    "Hydraulic pressure loss overnight",
    "Bearing noise on drive end",
]
_BASE_CASE_NOTIFICATION = "43014305"
_BASE_CASE_DESCRIPTION = "S/PI/TBR1/FZ28/NSD/GI13-305/LEAK/CI/SK/2"
_BASE_CASE_SAP_PRIORITY = "Intermediate"


class MockReportSource(ReportSource):
    """Generates plausible IW29 output so the whole app can run with no SAP at all."""

    name = "mock"

    def __init__(
        self,
        rows: int = 250,
        plants: Optional[List[str]] = None,
        work_centers: Optional[List[str]] = None,
        date_from: Optional[date] = None,
        date_to: Optional[date] = None,
        seed: Optional[int] = None,
    ):
        self.rows = max(rows, 0)
        self.plants = plants or ["1000"]
        self.work_centers = work_centers or _MOCK_WORK_CENTERS
        self.date_to = date_to or date.today()
        self.date_from = date_from or (self.date_to - timedelta(days=30))
        self._random = random.Random(seed)

    def extract(self, staging_dir: Path, progress: ProgressFn) -> Extract:
        progress("Mock mode: generating sample IW29 rows (SAP is not contacted).")
        staging_dir.mkdir(parents=True, exist_ok=True)
        target = staging_dir / f"mock_iw29_{datetime.now():%Y%m%d_%H%M%S}.txt"
        span = max((self.date_to - self.date_from).days, 0)

        with target.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, delimiter="\t", quoting=csv.QUOTE_MINIMAL)
            writer.writerow(_MOCK_COLUMNS)
            for index in range(self.rows):
                created = self.date_from + timedelta(
                    days=self._random.randint(0, span) if span else 0
                )
                required = created + timedelta(days=self._random.randint(1, 45))
                notification, description, sap_priority = self._mock_notification(index)
                writer.writerow(
                    [
                        notification,
                        self._random.choice(_MOCK_TYPES),
                        description,
                        self._random.choice(self.plants),
                        self._random.choice(self.work_centers),
                        sap_priority,
                        created.strftime("%d.%m.%Y"),
                        required.strftime("%d.%m.%Y"),
                        self._random.choice(_MOCK_STATUSES),
                        self._random.choice(["A.MARTIN", "J.SILVA", "K.OWENS"]),
                        f"EQ-{self._random.randint(10000, 99999)}",
                        f"PLANT1-AREA{self._random.randint(1, 6)}-L{self._random.randint(1, 9)}",
                        f"{self._random.uniform(120, 18000):.2f}".replace(".", ","),
                    ]
                )

        progress(f"Mock extract written: {target.name} ({self.rows} rows).")
        return Extract(
            path=target,
            kind="text",
            row_count=self.rows,
            columns=list(_MOCK_COLUMNS),
            reported_by_sap=self.rows,
        )

    def _mock_notification(self, index: int) -> tuple:
        """First row is the documented base case; later rows mix convention vs free text."""
        if index == 0:
            return (
                _BASE_CASE_NOTIFICATION,
                _BASE_CASE_DESCRIPTION,
                _BASE_CASE_SAP_PRIORITY,
            )
        inspector = self._random.randint(1, 4)
        if self._random.random() < 0.35:
            sap_rank = self._random.choice(
                [rank for rank in range(1, 5) if rank != inspector] or [inspector]
            )
        else:
            sap_rank = inspector
        sap_priority = _MOCK_SAP_LABELS[sap_rank]
        if self._random.random() < 0.12:
            return (
                f"10{2000000 + index}",
                self._random.choice(_MOCK_TEXTS),
                sap_priority,
            )
        asset = self._random.choice(_MOCK_ASSETS)
        fire_zone = self._random.randint(1, 32)
        line = self._random.randint(10, 99)
        spool = self._random.randint(100, 999)
        defect = self._random.choice(_MOCK_DEFECTS)
        description = (
            f"S/PI/{asset}/FZ{fire_zone}/NSD/GI{line}-{spool}/{defect}/CI/SK/{inspector}"
        )
        return f"10{2000000 + index}", description, sap_priority
