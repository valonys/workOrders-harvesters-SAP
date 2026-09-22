"""Priority Harmonization Engine.

Integrity inspectors encode the true field-assessed priority as the last
numeric segment of the IW29 notification description:

    S/PI/TBR1/FZ28/NSD/GI13-305/LEAK/CI/SK/2
                                              ^
                                              InspectorPriority = 2

SAP IW29 still carries the Prioritization Matrix value (often a label such
as Intermediate). Reporting, backlog ranking and Power BI must use the
inspector digit. The SAP value is retained for audit and mismatch analysis.

Governing rule
--------------
Priority_Final = InspectorPriority when the last description token is 1–5.
Otherwise Priority_Final is blank (never silently filled from the matrix).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, List, Optional, Sequence, Tuple

from .convert import Table
from .logging_setup import get_logger

log = get_logger("priority")

PRIORITY_MIN = 1
PRIORITY_MAX = 5
MIN_CONVENTION_TOKENS = 6

SOURCE_INSPECTION = "Inspection Naming Convention"
SOURCE_UNPARSED = "Unparsed"

CONFIDENCE_HIGH = "High"
CONFIDENCE_MEDIUM = "Medium"
CONFIDENCE_LOW = "Low"
CONFIDENCE_NONE = "None"

STATUS_PARSED = "Parsed"
STATUS_BLANK = "DescriptionBlank"
STATUS_NOT_NUMERIC = "LastTokenNotNumeric"
STATUS_OUT_OF_RANGE = "PriorityOutOfRange"
STATUS_NO_DESCRIPTION_COLUMN = "DescriptionColumnMissing"

YES = "Yes"
NO = "No"

OUTPUT_COLUMNS: Tuple[str, ...] = (
    "SAPPriority",
    "SAPPriorityRank",
    "InspectorPriority",
    "Priority_Final",
    "PriorityMismatch",
    "PriorityVariance",
    "PrioritySource",
    "PriorityConfidence",
    "NamingConventionValid",
    "FireZone",
    "ParseStatus",
)

_DESCRIPTION_ALIASES = (
    "description",
    "short text",
    "notification description",
    "description of notification",
    "qmtxt",
    "notif. description",
)
_SAP_PRIORITY_ALIASES = (
    "priority",
    "pri",
    "priok",
    "priority type",
    "notification priority",
    "sappriority",
)

_TOKEN_SPLIT = re.compile(r"[/\\]+")
_PRIORITY_TOKEN = re.compile(
    r"^(?:p(?:ri(?:ority)?)?[-_ ]?)?([1-9]\d?)$",
    re.IGNORECASE,
)
_FIRE_ZONE = re.compile(r"^fz[-_]?(\d+)$", re.IGNORECASE)

# SAP PM matrix labels → rank. 1 is most severe. Intermediate is rank 3.
_SAP_RANK_ALIASES = {
    "1": 1,
    "01": 1,
    "very high": 1,
    "immediate": 1,
    "emergency": 1,
    "critical": 1,
    "2": 2,
    "02": 2,
    "high": 2,
    "urgent": 2,
    "3": 3,
    "03": 3,
    "medium": 3,
    "intermediate": 3,
    "normal": 3,
    "4": 4,
    "04": 4,
    "low": 4,
    "5": 5,
    "05": 5,
    "very low": 5,
    "planning": 5,
}


@dataclass(frozen=True)
class HarmonizedPriority:
    sap_priority: Any = None
    sap_rank: Optional[int] = None
    inspector_priority: Optional[int] = None
    priority_final: Optional[int] = None
    mismatch: Optional[str] = None
    variance: Optional[int] = None
    source: str = SOURCE_UNPARSED
    confidence: str = CONFIDENCE_NONE
    naming_valid: str = NO
    fire_zone: Optional[str] = None
    parse_status: str = STATUS_BLANK

    def as_cells(self) -> List[Any]:
        return [
            self.sap_priority,
            self.sap_rank,
            self.inspector_priority,
            self.priority_final,
            self.mismatch,
            self.variance,
            self.source,
            self.confidence,
            self.naming_valid,
            self.fire_zone,
            self.parse_status,
        ]


@dataclass
class EnrichmentStats:
    rows: int = 0
    parsed: int = 0
    unparsed: int = 0
    mismatches: int = 0
    sap_under_ranked: int = 0
    high_confidence: int = 0

    def log_summary(self) -> None:
        log.info(
            "Priority engine: %d rows, %d parsed, %d unparsed, %d mismatches, "
            "%d SAP-under-ranked (inspector more severe).",
            self.rows,
            self.parsed,
            self.unparsed,
            self.mismatches,
            self.sap_under_ranked,
        )


def extract_inspector_priority(description: Any) -> Optional[int]:
    """Return the last numeric segment of a notification description, or None."""
    return harmonize(description, sap_priority=None).inspector_priority


def map_sap_priority(value: Any) -> Optional[int]:
    """Map a SAP matrix code or label onto ranks 1–5."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if PRIORITY_MIN <= value <= PRIORITY_MAX else None
    if isinstance(value, float) and value.is_integer():
        rank = int(value)
        return rank if PRIORITY_MIN <= rank <= PRIORITY_MAX else None
    key = re.sub(r"\s+", " ", str(value).strip().lower())
    if not key:
        return None
    return _SAP_RANK_ALIASES.get(key)


def extract_fire_zone(description: Any) -> Optional[str]:
    for token in _tokens(description):
        match = _FIRE_ZONE.match(token)
        if match:
            return f"FZ{match.group(1)}"
    return None


def harmonize(description: Any, sap_priority: Any = None) -> HarmonizedPriority:
    sap_rank = map_sap_priority(sap_priority)
    sap_display = _display_sap(sap_priority)
    fire_zone = extract_fire_zone(description)

    text = _clean_text(description)
    if not text:
        return HarmonizedPriority(
            sap_priority=sap_display,
            sap_rank=sap_rank,
            fire_zone=fire_zone,
            parse_status=STATUS_BLANK,
            confidence=CONFIDENCE_LOW if sap_rank else CONFIDENCE_NONE,
        )

    tokens = _tokens(text)
    last = tokens[-1] if tokens else ""
    match = _PRIORITY_TOKEN.match(last)
    if match is None:
        return HarmonizedPriority(
            sap_priority=sap_display,
            sap_rank=sap_rank,
            fire_zone=fire_zone,
            parse_status=STATUS_NOT_NUMERIC,
            confidence=CONFIDENCE_LOW if sap_rank else CONFIDENCE_NONE,
        )

    inspector = int(match.group(1))
    if not (PRIORITY_MIN <= inspector <= PRIORITY_MAX):
        return HarmonizedPriority(
            sap_priority=sap_display,
            sap_rank=sap_rank,
            fire_zone=fire_zone,
            parse_status=STATUS_OUT_OF_RANGE,
            confidence=CONFIDENCE_LOW if sap_rank else CONFIDENCE_NONE,
        )

    naming_valid = len(tokens) >= MIN_CONVENTION_TOKENS and last.isdigit()
    confidence = CONFIDENCE_HIGH if naming_valid else CONFIDENCE_MEDIUM
    mismatch, variance = _compare(inspector, sap_rank)
    return HarmonizedPriority(
        sap_priority=sap_display,
        sap_rank=sap_rank,
        inspector_priority=inspector,
        priority_final=inspector,
        mismatch=mismatch,
        variance=variance,
        source=SOURCE_INSPECTION,
        confidence=confidence,
        naming_valid=YES if naming_valid else NO,
        fire_zone=fire_zone,
        parse_status=STATUS_PARSED,
    )


def enrich(table: Table) -> Table:
    """Append harmonized priority columns. Idempotent if run twice."""
    keep_indexes = [
        index
        for index, header in enumerate(table.headers)
        if header not in OUTPUT_COLUMNS
    ]
    headers = [table.headers[index] for index in keep_indexes]
    headers.extend(OUTPUT_COLUMNS)

    desc_index = _find_column(table.headers, _DESCRIPTION_ALIASES)
    sap_index = _find_column(table.headers, _SAP_PRIORITY_ALIASES)
    stats = EnrichmentStats()

    if desc_index is None:
        log.warning(
            "No description column found among %s; Priority_Final will be blank.",
            table.headers,
        )

    rows: List[List[Any]] = []
    for row in table.rows:
        kept = [_cell(row, index) for index in keep_indexes]
        if desc_index is None:
            result = HarmonizedPriority(
                sap_priority=_display_sap(_cell(row, sap_index)),
                sap_rank=map_sap_priority(_cell(row, sap_index)),
                parse_status=STATUS_NO_DESCRIPTION_COLUMN,
            )
        else:
            result = harmonize(_cell(row, desc_index), _cell(row, sap_index))
        kept.extend(result.as_cells())
        rows.append(kept)
        _tally(stats, result)

    stats.log_summary()
    return Table(headers=headers, rows=rows)


def _tally(stats: EnrichmentStats, result: HarmonizedPriority) -> None:
    stats.rows += 1
    if result.inspector_priority is not None:
        stats.parsed += 1
    else:
        stats.unparsed += 1
    if result.mismatch == YES:
        stats.mismatches += 1
    if result.variance is not None and result.variance < 0:
        stats.sap_under_ranked += 1
    if result.confidence == CONFIDENCE_HIGH:
        stats.high_confidence += 1


def _compare(
    inspector: int, sap_rank: Optional[int]
) -> Tuple[Optional[str], Optional[int]]:
    if sap_rank is None:
        return None, None
    variance = inspector - sap_rank
    return (YES if variance else NO), variance


def _tokens(description: Any) -> List[str]:
    text = _clean_text(description)
    if not text:
        return []
    return [token for token in _TOKEN_SPLIT.split(text) if token]


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value).strip())


def _display_sap(value: Any) -> Any:
    if value is None or value == "":
        return None
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        return text or None
    return value


def _find_column(headers: Sequence[str], aliases: Iterable[str]) -> Optional[int]:
    wanted = {alias.lower() for alias in aliases}
    for index, header in enumerate(headers):
        name = re.sub(r"\s+", " ", str(header or "").strip()).lower()
        name = re.sub(r"\s+\(\d+\)$", "", name)
        if name in wanted:
            return index
    return None


def _cell(row: Sequence[Any], index: Optional[int]) -> Any:
    if index is None or index < 0 or index >= len(row):
        return None
    return row[index]
