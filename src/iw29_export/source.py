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
                writer.writerow(
                    [
                        f"10{2000000 + index}",
                        self._random.choice(_MOCK_TYPES),
                        self._random.choice(_MOCK_TEXTS),
                        self._random.choice(self.plants),
                        self._random.choice(self.work_centers),
                        self._random.randint(1, 4),
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
