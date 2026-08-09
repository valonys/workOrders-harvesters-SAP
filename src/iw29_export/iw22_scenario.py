"""Build a one-sentence Scenario summary from an IW22 notification PDF.

Prefers embedded PDF text (most harvested reports already have a text layer).
Returns an empty string when too little text is available (OCR can fill later).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Sequence

from .logging_setup import get_logger

log = get_logger("iw22_scenario")

_MIN_CHARS = 80
_MAX_SENTENCE = 420
_FLOC_RE = re.compile(
    r"\b((?:GIR|DAL|PAZ|CLV)/[A-Z0-9]+(?:/[A-Z0-9_\-]+){1,8})",
    re.IGNORECASE,
)
_DATE_RE = re.compile(
    r"\b("
    r"\d{4}-\d{2}-\d{2}"
    r"|"
    r"\d{1,2}[./]\d{1,2}[./]\d{2,4}"
    r"|"
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},\s+\d{4}"
    r")\b",
    re.IGNORECASE,
)
_LEADING_JUNK = re.compile(r"^[\W_\d]+", re.UNICODE)
_BULLET_PREFIX = re.compile(r"^(?:[\u2022\u25cf\u25a0\u25aa\uf0b7\uf071•●▪\-–—*]+)\s*")
_NC_LINE = re.compile(r"^(?:NC|NG)\b", re.IGNORECASE)
_PHOTO_NOISE = re.compile(r"\b(?:photo|file)\s*\d", re.IGNORECASE)
_PERSON_TAIL = re.compile(
    r"\s+(?:[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,3}|[A-Z]\.\s*[A-Z][A-Za-z\-]+)"
    r"(?:\s+\d{1,2}[./\-]\d{1,2}[./\-]\d{2,4}|\s+\d{4}-\d{2}-\d{2})?\s*$"
)


def summarize_pdf(path: Path, *, max_pages: int = 6) -> str:
    """Return one natural-language Scenario sentence for ``path``, or ``\"\"``."""
    text = extract_pdf_text(path, max_pages=max_pages)
    if len(text) < _MIN_CHARS:
        log.debug("Insufficient text for scenario: %s (%d chars)", path.name, len(text))
        return ""
    return summarize_text(text)


def extract_pdf_text(path: Path, *, max_pages: int = 6) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        log.warning("pypdf is not installed; cannot extract PDF text for Scenario")
        return ""

    try:
        reader = PdfReader(str(path))
    except Exception as exc:
        log.warning("Failed reading PDF %s: %s", path, exc)
        return ""

    chunks: List[str] = []
    pages = list(getattr(reader, "pages", []) or [])
    for page in pages[: max(1, max_pages)]:
        try:
            chunk = page.extract_text() or ""
        except Exception:
            chunk = ""
        if chunk:
            chunks.append(chunk)
    return _normalize_text("\n".join(chunks))


def summarize_text(text: str) -> str:
    """Heuristic Scenario builder for known IW22 report layouts."""
    text = _normalize_text(text)
    if len(text) < _MIN_CHARS:
        return ""

    lower = text.lower()
    if "observation report" in lower or "description of the observation" in lower:
        return _scenario_observation(text)
    survey = _scenario_handrail_survey(text)
    if survey:
        return survey
    if (
        "inspection report" in lower
        or "inspection summary" in lower
        or re.search(r"\bfindings\b", lower)
        or "general comments" in lower
    ):
        return _scenario_inspection(text)
    if "inspection workbook" in lower or "coating external condition" in lower:
        return _scenario_workbook(text)
    return _scenario_generic(text)


def _scenario_observation(text: str) -> str:
    description = _field_value(text, "Description of the observation")
    location = _join_nonempty(
        [
            _field_value(text, "Location"),
            _field_value(text, "Sub-Location"),
            _field_value(text, "Location Details"),
        ],
        sep=", ",
    )
    site = _field_value(text, "SITE")
    action = _field_value(text, "IMMEDIATE ACTIONS IMPLEMENTED")
    if not action:
        action = _field_value(text, "SUGGESTION OF ACTION TO BE IMPLEMENTED")
    date = _first_date(text)

    where = location or _first_floc(text) or site or "the reported location"
    finding = description or "an anomaly was recorded"
    parts = [f"Observation at {where} noted {_lead_lower(finding)}"]
    if action and action.lower() not in ("yes", "no", "n/a", "na", "?"):
        parts.append(f"immediate action taken: {_lead_lower(action)}")
    if date:
        parts.append(f"recorded {date}")
    return _finalize("; ".join(parts))


def _scenario_inspection(text: str) -> str:
    floc = _first_floc(text)
    module = _inspection_subject(text)
    where = module or floc or "the inspected area"
    date = _inspection_date(text) or _first_date(text)

    critical = _section_after_label(
        text,
        ("Critical Safety Concern", "CRITICAL SAFETY CONCERN"),
        max_chars=220,
    )
    findings = _collect_findings(text)
    general = _section_after_label(
        text,
        ("INSPECTION SUMMARY", "General Comments", "GENERAL COMMENTS"),
        max_chars=220,
    )

    # Prefer the most decision-useful body.
    if critical:
        body = critical
        if findings and findings.lower() not in critical.lower():
            body = f"{critical}; also {_lead_lower(findings)}"
    elif findings:
        body = findings
    elif general:
        body = general
    else:
        body = _workbook_comments(text) or ""

    if not body:
        return ""

    opener = f"Inspection of {where}"
    if date:
        opener += f" on {date}"
    sentence = f"{opener} found {_lead_lower(body)}"
    if floc and module and floc.lower() not in sentence.lower():
        # Keep floc short and clean in the parenthetical.
        sentence = f"{sentence} (at {floc})"
    return _finalize(sentence)


def _scenario_workbook(text: str) -> str:
    # Many workbooks embed a full inspection report — prefer that path.
    if re.search(r"\bfindings\b|general comments|inspection report", text, re.I):
        return _scenario_inspection(text)

    floc = _first_floc(text)
    date = _first_date(text)
    comments = _workbook_comments(text)
    subject = _inspection_subject(text) or floc or "the inspected structure"
    body = comments or "inspection workbook readings and defect codes were recorded"
    opener = f"Inspection workbook for {subject}"
    if date:
        opener += f" dated {date}"
    return _finalize(f"{opener} notes {_lead_lower(body)}")


def _scenario_generic(text: str) -> str:
    survey = _scenario_handrail_survey(text)
    if survey:
        return survey
    floc = _first_floc(text)
    date = _first_date(text)
    findings = _collect_findings(text)
    snippet = findings or _first_substantive_sentence(text)
    if not snippet or _looks_like_table_header(snippet):
        return ""
    where = floc or "the notification attachment"
    opener = f"Report for {where}"
    if date:
        opener += f" dated {date}"
    return _finalize(f"{opener}: {_lead_lower(snippet)}")


def _scenario_handrail_survey(text: str) -> str:
    """Summarise Girassol-style handrail status survey spreadsheets."""
    if not re.search(r"handrail status survey|carbon steel handrail", text, re.I):
        return ""
    severities = {"severe": 0, "moderate": 0, "minor": 0, "good": 0}
    zones = set()
    for line in _iter_clean_lines(text):
        m = re.match(
            r"^(Severe|Moderate|Minor|Good)\s+"
            r"(\d{1,2}[./\-]\d{1,2}[./\-]\d{2,4}|\d{4}-\d{2}-\d{2})\s+"
            r"Carbon Steel handrail(?:\s+(Zone\s+\d+)|\s+NC\b|\s|$)",
            line,
            re.IGNORECASE,
        )
        if not m:
            continue
        severities[m.group(1).lower()] = severities.get(m.group(1).lower(), 0) + 1
        if m.group(3):
            zones.add(m.group(3))
    total = sum(severities.values())
    if total == 0:
        return ""
    if zones:
        zone_txt = ", ".join(
            sorted(zones, key=lambda z: int(re.search(r"\d+", z).group()))
        )
        zone_clause = f" across {zone_txt}"
    else:
        zone_clause = ""
    bits = []
    for key in ("severe", "moderate", "minor"):
        if severities.get(key):
            bits.append(f"{severities[key]} {key}")
    severity_txt = ", ".join(bits) if bits else f"{total} recorded"
    title = "Girassol handrail status survey"
    return _finalize(
        f"{title} identified {severity_txt} carbon-steel handrail condition(s)"
        f"{zone_clause}, with corroded sections flagged for replacement"
    )


def _collect_findings(text: str) -> str:
    body = _section_after_label(text, ("Findings", "FINDINGS"), max_chars=320)
    if not body:
        return ""
    # Elevate embedded critical-safety sentences.
    critical = re.search(
        r"Critical Safety Concern:\s*(.+?)(?:;|$)",
        body,
        re.IGNORECASE | re.DOTALL,
    )
    if critical:
        crit = _clean_phrase(critical.group(1))
        rest = _clean_phrase(body.replace(critical.group(0), " "))
        rest = re.sub(r"^General Condition:\s*", "", rest, flags=re.I)
        if rest and "good general condition" in rest.lower():
            body = f"{crit} (module otherwise in good general condition)"
        else:
            body = crit
    body = re.split(
        r"\b(?:the following nc'?s|below is a list|recommendations?)\b",
        body,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    return _clean_phrase(body)


def _inspection_subject(text: str) -> str:
    """Best-effort module / area name from inspection headers."""
    patterns = (
        r"\b(BOAT LANDING(?:\s+PORT(?:\s+FWD|\s+AFT)?)?)\b",
        r"\b(FORWARD BALCON(?:Y|IES)|FWD[\-\s]?BALCON(?:Y|IES)?)\b",
        r"\b(SEA WATER[\-\s]?TREAT(?:MENT)?)\b",
        r"\b(MOD[\-\s]?P?\d[\-A-Z0-9]*)\b",
        r"\b(P\d(?:\s+Structure)?)\b",
        r"\b(S\d(?:\s+(?:Module|Structure))?)\b",
        r"\b(R\d(?:\s+Module)?)\b",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            subject = _clean_phrase(match.group(1))
            subject = re.sub(r"\s+N/?A\b", "", subject, flags=re.IGNORECASE)
            subject = re.sub(r"\s+N$", "", subject)
            subject = re.split(
                r"[\s/\-]+TER[\-\s]?STR.*$", subject, maxsplit=1, flags=re.I
            )[0]
            subject = _strip_person_tail(subject)
            if len(subject) >= 2:
                return subject

    loc = _field_value(text, "Location")
    if loc and not re.match(r"^(?:np|n/?a|\d+$)", loc, re.I):
        return _strip_person_tail(_clean_phrase(loc))
    return ""


def _inspection_date(text: str) -> str:
    for line in _iter_clean_lines(text):
        if _FLOC_RE.search(line) or re.search(r"inspected by|inspection date", line, re.I):
            found = _DATE_RE.search(line)
            if found:
                return found.group(1)
    # Date often sits on the WO / module header line.
    for line in _iter_clean_lines(text):
        if re.search(r"inspection report|serial number", line, re.I):
            continue
        if re.search(r"\b(?:P\d|S\d|MOD|BOAT LANDING|BALCON)", line, re.I):
            found = _DATE_RE.search(line)
            if found:
                return found.group(1)
    return ""


def _workbook_comments(text: str) -> str:
    general = _section_after_label(
        text, ("GENERAL COMMENTS", "General Comments"), max_chars=200
    )
    hits: List[str] = []
    if general:
        hits.append(general)
    for line in _iter_clean_lines(text):
        low = line.lower()
        if _NC_LINE.match(line) or _PHOTO_NOISE.search(line):
            continue
        if "paint touch" in low or "handrail" in low or "coating" in low:
            if 20 < len(line) < 180:
                hits.append(_clean_phrase(line))
        if len(hits) >= 3:
            break
    return _join_nonempty(hits[:3], sep="; ")


def _field_value(text: str, label: str) -> str:
    """Read a label's value from the same line or the next non-empty line."""
    for idx, line in enumerate(_iter_clean_lines(text)):
        match = re.match(
            rf"^{re.escape(label)}\s*:?\s*(.*)$",
            line,
            re.IGNORECASE,
        )
        if not match:
            continue
        same = match.group(1).strip(" ?")
        if same and not _is_section_label(same):
            return _clean_phrase(same)
        # Look ahead
        lines = list(_iter_clean_lines(text))
        for nxt in lines[idx + 1 : idx + 4]:
            if _is_section_label(nxt):
                break
            return _clean_phrase(nxt)
    return ""


def _section_after_label(
    text: str,
    labels: Sequence[str],
    *,
    max_chars: int = 240,
) -> str:
    """Collect prose/bullets after a section heading until the next section."""
    lines = list(_iter_clean_lines(text))
    label_res = [
        re.compile(rf"^{re.escape(label.rstrip(':'))}\s*:?\s*(.*)$", re.IGNORECASE)
        for label in labels
    ]
    for idx, line in enumerate(lines):
        remainder = ""
        matched = False
        for cre in label_res:
            m = cre.match(line)
            if not m:
                continue
            rem = (m.group(1) or "").strip()
            # Skip narrative that merely contains the label word.
            if rem and not rem[:1].isupper() and not rem.startswith(("-", "•", "–")):
                if not re.match(
                    r"^(?:general condition|critical|coating|close visual|multiple)\b",
                    rem,
                    re.I,
                ):
                    continue
            matched = True
            remainder = rem
            break
        if not matched:
            continue

        parts: List[str] = []
        if remainder and not _is_noise_line(remainder):
            parts.append(_strip_bullet(remainder))

        for nxt in lines[idx + 1 : idx + 30]:
            if _is_section_label(nxt) or _is_hard_stop(nxt):
                break
            if _is_noise_line(nxt):
                continue
            item = _strip_bullet(nxt)
            if not item:
                continue
            # Collapse "noted on:" + item list into one phrase.
            if parts and parts[-1].rstrip(":").lower().endswith(
                ("noted on", "noted on the following items", "including")
            ):
                parts[-1] = f"{parts[-1].rstrip(':')} {item}"
            else:
                parts.append(item)
            if sum(len(p) for p in parts) >= max_chars:
                break

        joined = _join_nonempty(parts, sep="; ")
        if joined:
            return _clean_phrase(joined[:max_chars])
    return ""


def _first_floc(text: str) -> str:
    match = _FLOC_RE.search(text)
    if not match:
        return ""
    floc = match.group(1)
    # Drop trailing defect-code / reading noise if the regex over-captured.
    floc = re.split(r"\s{2,}|\s+X\s+\d", floc)[0]
    return _clean_phrase(floc.strip(" /"))


def _first_date(text: str) -> str:
    match = _DATE_RE.search(text)
    return match.group(1) if match else ""


def _first_substantive_sentence(text: str) -> str:
    for line in _iter_clean_lines(text):
        if len(line) < 40 or _looks_like_table_header(line) or _is_noise_line(line):
            continue
        if re.search(
            r"inspect|finding|corrosion|coating|observation|handrail|damage|leak",
            line,
            re.I,
        ):
            return _clean_phrase(line)
    return ""


def _iter_clean_lines(text: str):
    for raw in text.splitlines():
        line = _clean_line(raw)
        if line:
            yield line


def _clean_line(line: str) -> str:
    line = line.replace("\x00", " ").strip()
    # PDF symbol fonts often emit private-use glyphs before headings/bullets.
    line = _LEADING_JUNK.sub("", line).strip()
    line = _BULLET_PREFIX.sub("", line).strip()
    line = re.sub(r"\s+", " ", line)
    return line


def _strip_bullet(value: str) -> str:
    return _BULLET_PREFIX.sub("", value).strip(" -–—•")


def _is_section_label(line: str) -> bool:
    return bool(
        re.match(
            r"^(?:"
            r"findings|recommendations?|legend|inspection summary|"
            r"general comments?|critical safety concern|"
            r"general informations?|photo\(s\)|activity|historical|"
            r"immediate actions?|suggestion of action|"
            r"below is a list|work order route|validation\s*\d|"
            r"page\s+\d+\s+of"
            r")\s*:?\s*$",
            line,
            re.IGNORECASE,
        )
    )


def _is_hard_stop(line: str) -> bool:
    return bool(
        re.match(
            r"^(?:"
            r"recommendations?|legend|page\s+\d+|below is a list|"
            r"the following nc'?s|total (?:worked|hours)|number of persons|"
            r"work order route|validation\s*\d"
            r")\b",
            line,
            re.IGNORECASE,
        )
    )


def _is_noise_line(line: str) -> bool:
    if _NC_LINE.match(line) or _PHOTO_NOISE.search(line):
        return True
    if re.match(r"^(?:wo|np|item|serial number|functional location)\b", line, re.I):
        return True
    if _looks_like_table_header(line):
        return True
    if re.match(r"^(?:on tertiary structures?|classe and position)\b", line, re.I):
        return True
    return False


def _looks_like_table_header(line: str) -> bool:
    low = line.lower()
    hits = sum(
        1
        for key in (
            "module",
            "corrosion severity",
            "material",
            "photo folder",
            "reference",
            "recommendation",
            "notification",
            "work order route",
            "qual reading",
        )
        if key in low
    )
    return hits >= 3 or low.startswith("rows module")


def _strip_person_tail(value: str) -> str:
    value = _DATE_RE.sub("", value)
    value = _PERSON_TAIL.sub("", value)
    value = re.sub(r"\s{2,}", " ", value).strip(" -/,")
    return value


def _normalize_text(text: str) -> str:
    text = text.replace("\x00", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _clean_phrase(value: str) -> str:
    value = re.sub(r"\s+", " ", value).strip(" \t-–—:;,.|")
    value = re.sub(r"\s+([,.;:])", r"\1", value)
    value = re.sub(r"(?:\s*;\s*){2,}", "; ", value)
    return value


def _lead_lower(value: str) -> str:
    value = _clean_phrase(value)
    if not value:
        return value
    if value[:3].upper() in {"GIR", "DAL", "PAZ", "CLV"}:
        return value
    if value[:1].isupper() and value[1:2].islower():
        return value[0].lower() + value[1:]
    return value


def _join_nonempty(parts: Sequence[str], *, sep: str = ", ") -> str:
    return sep.join(p for p in parts if p and p.strip())


def _finalize(sentence: str) -> str:
    sentence = _clean_phrase(sentence)
    sentence = re.sub(r"\s+;", ";", sentence)
    sentence = re.sub(r";\s*;", ";", sentence)
    if not sentence:
        return ""
    if sentence[0].islower():
        sentence = sentence[0].upper() + sentence[1:]
    if len(sentence) > _MAX_SENTENCE:
        cut = sentence[: _MAX_SENTENCE - 1]
        for sep in ("; ", ", ", " "):
            pos = cut.rfind(sep)
            if pos > _MAX_SENTENCE // 2:
                cut = cut[:pos]
                break
        sentence = cut.rstrip(" ,;") + "…"
    if sentence[-1] not in ".!?…":
        sentence += "."
    return sentence
