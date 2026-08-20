"""Merge split notification PDFs: 13332833(1).pdf + (2).pdf → 13332833.pdf."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .errors import ExportError
from .logging_setup import get_logger

log = get_logger("pdf_merge")

_PART_PDF = re.compile(
    r"^([A-Za-z0-9][A-Za-z0-9._-]{0,120})\((\d+)\)\.pdf$",
    re.IGNORECASE,
)


@dataclass
class MergeResult:
    folder: Path
    merged: List[Path] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    @property
    def merged_count(self) -> int:
        return len(self.merged)


def merge_folder(
    folder: Path,
    *,
    keep_parts: bool = True,
    notifications: Optional[Sequence[str]] = None,
) -> MergeResult:
    """Merge ``notif(n).pdf`` groups under ``folder`` into ``notif.pdf``."""
    folder = Path(folder)
    result = MergeResult(folder=folder)
    if not folder.is_dir():
        result.errors.append(f"not a folder: {folder}")
        return result

    wanted = {str(n).strip() for n in (notifications or []) if str(n).strip()}
    groups = _group_parts(folder)
    for number, parts in sorted(groups.items()):
        if wanted and number not in wanted:
            continue
        target = folder / f"{number}.pdf"
        try:
            if len(parts) >= 2:
                _merge_pdfs([path for _, path in parts], target)
                log.info(
                    "Merged %s → %s (%d parts)",
                    number,
                    target.name,
                    len(parts),
                )
            elif len(parts) == 1:
                # Canonicalise a lone notif(1).pdf → notif.pdf so folders stay tidy.
                only = parts[0][1]
                if only.resolve() == target.resolve():
                    result.skipped.append(f"{number}: already canonical")
                    continue
                if target.exists():
                    # Merged file already present; drop the leftover split part.
                    if not keep_parts:
                        only.unlink(missing_ok=True)
                        result.merged.append(target)
                        log.info(
                            "Removed leftover part %s (canonical %s exists)",
                            only.name,
                            target.name,
                        )
                    else:
                        result.skipped.append(
                            f"{number}: canonical exists, kept {only.name}"
                        )
                    continue
                _copy_pdf(only, target)
                log.info("Canonicalised %s → %s", only.name, target.name)
            else:
                result.skipped.append(f"{number}: no parts")
                continue
        except Exception as exc:
            log.exception("Failed merging %s", number)
            result.errors.append(f"{number}: {exc}")
            continue
        result.merged.append(target)
        if not keep_parts:
            for _, path in parts:
                try:
                    if path.resolve() != target.resolve():
                        path.unlink(missing_ok=True)
                except OSError as exc:
                    result.errors.append(f"could not remove {path.name}: {exc}")
    return result


def merge_folders(
    folders: Sequence[Path],
    *,
    keep_parts: bool = True,
) -> List[MergeResult]:
    return [merge_folder(folder, keep_parts=keep_parts) for folder in folders]


def _group_parts(folder: Path) -> Dict[str, List[Tuple[int, Path]]]:
    groups: Dict[str, List[Tuple[int, Path]]] = defaultdict(list)
    try:
        entries = list(folder.iterdir())
    except OSError:
        return groups
    for path in entries:
        if not path.is_file():
            continue
        match = _PART_PDF.match(path.name)
        if not match:
            continue
        number, seq = match.group(1), int(match.group(2))
        groups[number].append((seq, path))
    for number in groups:
        groups[number].sort(key=lambda item: item[0])
    return groups


def _merge_pdfs(sources: Sequence[Path], destination: Path) -> Path:
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError as exc:
        raise ExportError(
            "Merging notification PDFs needs pypdf "
            "(pip install pypdf). Add it via requirements-optional.txt."
        ) from exc

    writer = PdfWriter()
    for source in sources:
        reader = PdfReader(str(source))
        if getattr(reader, "is_encrypted", False):
            try:
                reader.decrypt("")
            except Exception:
                pass
        for page in reader.pages:
            writer.add_page(page)

    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_suffix(destination.suffix + ".tmp")
    with tmp.open("wb") as handle:
        writer.write(handle)
    tmp.replace(destination)
    return destination


def _copy_pdf(source: Path, destination: Path) -> Path:
    import shutil

    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_suffix(destination.suffix + ".tmp")
    shutil.copy2(source, tmp)
    tmp.replace(destination)
    return destination
