"""A minimal .xlsx writer built on zipfile, so the app needs no third-party wheel.

Corporate machines often cannot reach PyPI, and this is the only writing feature
the app needs: one sheet, a styled header row, frozen panes, an autofilter, and
real date/number cells.
"""

from __future__ import annotations

import re
import zipfile
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, List, Optional, Sequence

# Excel's 1900 date system counts from 1899-12-30 because of its leap-year bug.
_EPOCH = date(1899, 12, 30)

_STYLE_DEFAULT = 0
_STYLE_HEADER = 1
_STYLE_DATE = 2
_STYLE_NUMBER = 3

_ILLEGAL_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_MAX_CELL_CHARS = 32767

_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>"""

_ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""

_WORKBOOK_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""

_STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<numFmts count="1"><numFmt numFmtId="164" formatCode="yyyy\\-mm\\-dd"/></numFmts>
<fonts count="2">
<font><sz val="11"/><name val="Calibri"/></font>
<font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Calibri"/></font>
</fonts>
<fills count="3">
<fill><patternFill patternType="none"/></fill>
<fill><patternFill patternType="gray125"/></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FF1F4E79"/><bgColor indexed="64"/></patternFill></fill>
</fills>
<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="4">
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1" applyAlignment="1"><alignment vertical="center"/></xf>
<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>
<xf numFmtId="4" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>
</cellXfs>
</styleSheet>"""


def write(
    headers: Sequence[str],
    rows: Sequence[Sequence[Any]],
    destination: Path,
    sheet_name: str = "Sheet1",
    column_widths: Optional[Sequence[float]] = None,
) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    width = len(headers)
    last_cell = f"{column_letter(max(width, 1))}{len(rows) + 1}"

    sheet_xml = _sheet_xml(headers, rows, last_cell, column_widths)
    workbook_xml = _workbook_xml(sheet_name)

    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        archive.writestr("_rels/.rels", _ROOT_RELS)
        archive.writestr("xl/workbook.xml", workbook_xml)
        archive.writestr("xl/_rels/workbook.xml.rels", _WORKBOOK_RELS)
        archive.writestr("xl/styles.xml", _STYLES)
        archive.writestr("xl/worksheets/sheet1.xml", sheet_xml)
    return destination


def column_letter(index: int) -> str:
    """1 -> A, 27 -> AA."""
    if index < 1:
        raise ValueError("Column indexes start at 1.")
    letters = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def _workbook_xml(sheet_name: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<sheets><sheet name="{_escape(safe_sheet_name(sheet_name))}" sheetId="1" r:id="rId1"/></sheets>'
        "</workbook>"
    )


def _sheet_xml(
    headers: Sequence[str],
    rows: Sequence[Sequence[Any]],
    last_cell: str,
    column_widths: Optional[Sequence[float]],
) -> str:
    parts: List[str] = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
        f'<dimension ref="A1:{last_cell}"/>',
        '<sheetViews><sheetView tabSelected="1" workbookViewId="0">'
        '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
        "</sheetView></sheetViews>",
        '<sheetFormatPr defaultRowHeight="15"/>',
    ]

    if column_widths:
        cols = "".join(
            f'<col min="{index}" max="{index}" width="{width:.2f}" customWidth="1"/>'
            for index, width in enumerate(column_widths, start=1)
        )
        parts.append(f"<cols>{cols}</cols>")

    parts.append("<sheetData>")
    parts.append(_row_xml(1, headers, force_style=_STYLE_HEADER))
    for offset, row in enumerate(rows, start=2):
        parts.append(_row_xml(offset, row))
    parts.append("</sheetData>")
    parts.append(f'<autoFilter ref="A1:{last_cell}"/>')
    parts.append("</worksheet>")
    return "".join(parts)


def _row_xml(
    row_index: int, values: Iterable[Any], force_style: Optional[int] = None
) -> str:
    cells: List[str] = []
    for column_index, value in enumerate(values, start=1):
        reference = f"{column_letter(column_index)}{row_index}"
        cells.append(_cell_xml(reference, value, force_style))
    return f'<row r="{row_index}">' + "".join(cells) + "</row>"


def _cell_xml(reference: str, value: Any, force_style: Optional[int]) -> str:
    if value is None or value == "":
        style = f' s="{force_style}"' if force_style else ""
        return f'<c r="{reference}"{style}/>'

    if isinstance(value, bool):
        style = f' s="{force_style if force_style else _STYLE_DEFAULT}"'
        return f'<c r="{reference}"{style} t="b"><v>{int(value)}</v></c>'

    if isinstance(value, datetime):
        serial = (value.date() - _EPOCH).days + (
            value.hour * 3600 + value.minute * 60 + value.second
        ) / 86400.0
        style = force_style if force_style is not None else _STYLE_DATE
        return f'<c r="{reference}" s="{style}"><v>{serial:.6f}</v></c>'

    if isinstance(value, date):
        style = force_style if force_style is not None else _STYLE_DATE
        return f'<c r="{reference}" s="{style}"><v>{(value - _EPOCH).days}</v></c>'

    if isinstance(value, int):
        style = f' s="{force_style}"' if force_style else ""
        return f'<c r="{reference}"{style}><v>{value}</v></c>'

    if isinstance(value, float):
        style = force_style if force_style is not None else _STYLE_NUMBER
        return f'<c r="{reference}" s="{style}"><v>{value!r}</v></c>'

    style = f' s="{force_style}"' if force_style else ""
    text = _escape(_clip(str(value)))
    return (
        f'<c r="{reference}"{style} t="inlineStr">'
        f'<is><t xml:space="preserve">{text}</t></is></c>'
    )


def safe_sheet_name(name: str) -> str:
    cleaned = re.sub(r"[\[\]:*?/\\]", "-", (name or "Sheet1").strip()) or "Sheet1"
    return cleaned[:31]


def _clip(text: str) -> str:
    return text if len(text) <= _MAX_CELL_CHARS else text[: _MAX_CELL_CHARS - 3] + "..."


def _escape(text: str) -> str:
    cleaned = _ILLEGAL_XML.sub("", text)
    return (
        cleaned.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
