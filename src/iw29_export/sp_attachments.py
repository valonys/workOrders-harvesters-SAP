"""Harvest SharePoint / Fame+ documents by equipment tag via Microsoft Graph.

Flow mirrors IW22 attachments:
  tag list → search site → download all hits → tag(n).ext → merge PDFs → tag.pdf
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import quote

import requests

from .config import Config
from .errors import ConfigError, ExportError
from .logging_setup import get_logger

log = get_logger("sp_attachments")

# Microsoft Graph Command Line Tools (public client, device-code friendly).
_DEFAULT_CLIENT_ID = "14d82eec-204b-4c2f-b7e8-296a70dab67e"
_GRAPH = "https://graph.microsoft.com/v1.0"
_SCOPES = (
    "https://graph.microsoft.com/Sites.Read.All",
    "https://graph.microsoft.com/Files.Read.All",
)

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
_TOKEN_CACHE_NAME = "msal_sp_attachments_token.bin"


@dataclass
class SpHit:
    name: str
    web_url: str
    download_url: str
    size: int
    drive_id: str
    item_id: str


@dataclass
class TagResult:
    tag: str
    status: str  # saved | skipped | failed
    files: List[Path] = field(default_factory=list)
    detail: str = ""


@dataclass
class SpHarvestResult:
    saved: int = 0
    skipped: int = 0
    failed: int = 0
    results: List[TagResult] = field(default_factory=list)


def run(
    config: Config,
    *,
    tags: Optional[Sequence[str]] = None,
    limit: int = 0,
) -> SpHarvestResult:
    cfg = config.sharepoint_attachments
    if not cfg.enabled:
        raise ConfigError("sharepoint_attachments.enabled is false.")
    if not cfg.site_url.strip():
        raise ConfigError("sharepoint_attachments.site_url must be set.")

    wanted = list(tags or [])
    if not wanted:
        if not cfg.list_path or not cfg.list_path.is_file():
            raise ConfigError(
                "No tags given and sharepoint_attachments.list_path is missing."
            )
        wanted = load_tags(cfg.list_path)
    if limit and limit > 0:
        wanted = wanted[:limit]
    if not wanted:
        raise ConfigError("Tag list is empty.")

    out_root = cfg.output_folder or (
        Path.home() / "OneDrive - TotalEnergies" / "Fame+" / "sp_attachments"
    )
    out_root.mkdir(parents=True, exist_ok=True)

    token = acquire_token(config)
    site_id = resolve_site_id(token, cfg.site_url)
    log.info(
        "SharePoint harvest: %d tag(s) on %s → %s",
        len(wanted),
        cfg.site_url,
        out_root,
    )

    summary = SpHarvestResult()
    for index, tag in enumerate(wanted, start=1):
        log.info("[%d/%d] tag %s", index, len(wanted), tag)
        try:
            item = _harvest_tag(
                token=token,
                site_id=site_id,
                tag=tag,
                out_root=out_root,
                max_results=cfg.max_results_per_tag,
                site_url=cfg.site_url,
            )
        except Exception as exc:  # noqa: BLE001 - surface per-tag
            log.exception("Failed on tag %s", tag)
            item = TagResult(tag=tag, status="failed", detail=str(exc))
        summary.results.append(item)
        if item.status == "saved":
            summary.saved += 1
        elif item.status == "skipped":
            summary.skipped += 1
        else:
            summary.failed += 1

    if cfg.merge_pdfs:
        from . import pdf_merge

        for folder in sorted({path.parent for r in summary.results for path in r.files}):
            merge = pdf_merge.merge_folder(folder, keep_parts=cfg.merge_keep_parts)
            log.info(
                "PDF merge %s: %d combined, %d skipped, %d error(s)",
                folder.name,
                merge.merged_count,
                len(merge.skipped),
                len(merge.errors),
            )

    log.info(
        "Done: %d saved, %d skipped, %d failed",
        summary.saved,
        summary.skipped,
        summary.failed,
    )
    return summary


def load_tags(path: Path) -> List[str]:
    """Parse Fame+ tag list; split ``A & B`` lines into separate tags."""
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    tags: List[str] = []
    seen = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.lower() in {"tag#", "tag", "tags"}:
            continue
        if line.startswith("#"):
            continue
        for part in re.split(r"\s*&\s*", line):
            tag = part.strip()
            if not tag:
                continue
            key = tag.upper()
            if key in seen:
                continue
            seen.add(key)
            tags.append(tag)
    return tags


def acquire_token(config: Config) -> str:
    try:
        import msal
    except ImportError as exc:
        raise ExportError(
            "SharePoint harvest needs msal (pip install msal)."
        ) from exc

    cfg = config.sharepoint_attachments
    client_id = cfg.client_id.strip() or _DEFAULT_CLIENT_ID
    tenant = cfg.tenant_id.strip() or "organizations"
    cache_path = (
        config.staging_folder / _TOKEN_CACHE_NAME
        if config.staging_folder
        else Path.home() / ".cache" / _TOKEN_CACHE_NAME
    )
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache = msal.SerializableTokenCache()
    if cache_path.is_file():
        cache.deserialize(cache_path.read_text(encoding="utf-8"))

    app = msal.PublicClientApplication(
        client_id,
        authority=f"https://login.microsoftonline.com/{tenant}",
        token_cache=cache,
    )
    accounts = app.get_accounts()
    result: Optional[Dict[str, Any]] = None
    if accounts:
        result = app.acquire_token_silent(list(_SCOPES), account=accounts[0])
    if not result:
        flow = app.initiate_device_flow(scopes=list(_SCOPES))
        if "user_code" not in flow:
            raise ExportError(f"Device flow failed: {json.dumps(flow)}")
        print(flow["message"], flush=True)
        log.info("%s", flow["message"])
        result = app.acquire_token_by_device_flow(flow)
    _persist_cache(cache, cache_path)
    if not result or "access_token" not in result:
        raise ExportError(
            f"Could not acquire Graph token: {result.get('error_description') if result else 'unknown'}"
        )
    return str(result["access_token"])


def _persist_cache(cache: Any, path: Path) -> None:
    if getattr(cache, "has_state_changed", False):
        path.write_text(cache.serialize(), encoding="utf-8")


def resolve_site_id(token: str, site_url: str) -> str:
    """Resolve https://host/sites/Name → Graph site id."""
    host, server_relative = _split_site_url(site_url)
    url = f"{_GRAPH}/sites/{host}:{server_relative}"
    data = _graph_get(token, url)
    site_id = data.get("id")
    if not site_id:
        raise ExportError(f"Could not resolve SharePoint site id for {site_url}")
    log.info("Resolved site id for %s", site_url)
    return str(site_id)


def search_tag(
    token: str,
    *,
    site_id: str,
    site_url: str,
    tag: str,
    max_results: int = 50,
) -> List[SpHit]:
    """Search drive items for ``tag`` scoped to the configured site."""
    # Prefer Graph search with path constraint; fall back to site drive search.
    hits = _search_via_graph_query(token, site_url=site_url, tag=tag, size=max_results)
    if hits:
        return hits
    return _search_via_site_drive(token, site_id=site_id, tag=tag, size=max_results)


def _harvest_tag(
    *,
    token: str,
    site_id: str,
    tag: str,
    out_root: Path,
    max_results: int,
    site_url: str,
) -> TagResult:
    hits = search_tag(
        token,
        site_id=site_id,
        site_url=site_url,
        tag=tag,
        max_results=max_results,
    )
    if not hits:
        return TagResult(tag=tag, status="skipped", detail="no SharePoint hits")

    folder = out_root / _safe_name(tag)
    folder.mkdir(parents=True, exist_ok=True)
    saved: List[Path] = []
    for index, hit in enumerate(hits, start=1):
        ext = Path(hit.name).suffix or ".bin"
        dest = folder / f"{_safe_name(tag)}({index}){ext.lower()}"
        _download(token, hit, dest)
        saved.append(dest)
        log.info("Saved %s → %s (%s)", tag, dest.name, hit.name)
    return TagResult(
        tag=tag,
        status="saved",
        files=saved,
        detail=f"{len(saved)} file(s)",
    )


def _search_via_graph_query(
    token: str, *, site_url: str, tag: str, size: int
) -> List[SpHit]:
    body = {
        "requests": [
            {
                "entityTypes": ["driveItem"],
                "query": {
                    "queryString": f'{tag} path:"{site_url.rstrip("/")}"'
                },
                "from": 0,
                "size": min(max(size, 1), 100),
                "fields": [
                    "name",
                    "webUrl",
                    "size",
                    "parentReference",
                    "id",
                ],
            }
        ]
    }
    try:
        data = _graph_post(token, f"{_GRAPH}/search/query", body)
    except ExportError as exc:
        log.info("Graph search query unavailable for %s: %s", tag, exc)
        return []

    hits: List[SpHit] = []
    for response in data.get("value") or []:
        for container in response.get("hitsContainers") or []:
            for hit in container.get("hits") or []:
                resource = hit.get("resource") or {}
                parsed = _hit_from_resource(resource)
                if parsed:
                    hits.append(parsed)
    return hits


def _search_via_site_drive(
    token: str, *, site_id: str, tag: str, size: int
) -> List[SpHit]:
    # Search across the site's default drive.
    q = quote(tag)
    url = (
        f"{_GRAPH}/sites/{site_id}/drive/root/search(q='{q}')"
        f"?$top={min(max(size, 1), 200)}"
    )
    try:
        data = _graph_get(token, url)
    except ExportError as exc:
        log.info("Drive search unavailable for %s: %s", tag, exc)
        return []
    hits: List[SpHit] = []
    for item in data.get("value") or []:
        if item.get("folder"):
            continue
        parsed = _hit_from_resource(item)
        if parsed:
            hits.append(parsed)
    return hits


def _hit_from_resource(resource: Dict[str, Any]) -> Optional[SpHit]:
    name = str(resource.get("name") or "").strip()
    if not name:
        return None
    parent = resource.get("parentReference") or {}
    drive_id = str(parent.get("driveId") or resource.get("parentReference", {}).get("driveId") or "")
    item_id = str(resource.get("id") or "")
    download = str(
        resource.get("@microsoft.graph.downloadUrl")
        or resource.get("@content.downloadUrl")
        or ""
    )
    web_url = str(resource.get("webUrl") or "")
    size = int(resource.get("size") or 0)
    if not drive_id or not item_id:
        # Search hits sometimes nest under listItem.
        list_item = resource.get("listItem") or {}
        fields = list_item.get("fields") or {}
        item_id = item_id or str(list_item.get("id") or fields.get("id") or "")
    if not item_id and not download:
        return None
    return SpHit(
        name=name,
        web_url=web_url,
        download_url=download,
        size=size,
        drive_id=drive_id,
        item_id=item_id,
    )


def _download(token: str, hit: SpHit, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_suffix(destination.suffix + ".tmp")
    if hit.download_url:
        resp = requests.get(hit.download_url, timeout=120)
    elif hit.drive_id and hit.item_id:
        url = f"{_GRAPH}/drives/{hit.drive_id}/items/{hit.item_id}/content"
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=120,
            allow_redirects=True,
        )
    else:
        raise ExportError(f"No download URL for {hit.name}")
    if resp.status_code >= 400:
        raise ExportError(
            f"Download failed for {hit.name}: HTTP {resp.status_code}"
        )
    tmp.write_bytes(resp.content)
    tmp.replace(destination)
    return destination


def _graph_get(token: str, url: str) -> Dict[str, Any]:
    resp = requests.get(
        url,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        timeout=60,
    )
    if resp.status_code >= 400:
        raise ExportError(f"Graph GET {url} → {resp.status_code}: {resp.text[:300]}")
    return resp.json()


def _graph_post(token: str, url: str, body: Dict[str, Any]) -> Dict[str, Any]:
    resp = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        json=body,
        timeout=60,
    )
    if resp.status_code >= 400:
        raise ExportError(f"Graph POST {url} → {resp.status_code}: {resp.text[:400]}")
    return resp.json()


def _split_site_url(site_url: str) -> Tuple[str, str]:
    text = site_url.strip().rstrip("/")
    match = re.match(
        r"^https?://([^/]+)(/sites/[^/]+|/teams/[^/]+)",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        raise ConfigError(
            f"sharepoint_attachments.site_url must look like "
            f"https://tenant.sharepoint.com/sites/Name, got {site_url!r}"
        )
    return match.group(1), match.group(2)


def _safe_name(tag: str) -> str:
    cleaned = _UNSAFE.sub("_", tag.strip())
    cleaned = cleaned.replace(" ", "_")
    return cleaned[:120] or "tag"
