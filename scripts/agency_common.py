#!/usr/bin/env python3
"""Shared utilities for agency forecast fetchers."""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
DEFAULT_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def agency_paths(agency: str) -> tuple[Path, Path, Path]:
    """Return (current_dir, archive_dir, metadata_path) for an agency."""
    current = REPO_ROOT / "current" / agency
    archive = REPO_ROOT / "archive" / agency
    meta = current / "metadata.json"
    return current, archive, meta


def find_prior_file(archive_dir: Path, extension: str, today: str) -> Path | None:
    """Most recent prior archived file (extension like '.xlsx'/'.pdf'), or None."""
    if not archive_dir.exists():
        return None
    dated = sorted(
        d for d in archive_dir.iterdir()
        if d.is_dir() and DATE_RE.match(d.name) and d.name < today
    )
    for d in reversed(dated):
        for p in d.glob(f"*{extension}"):
            return p
    return None


def emit_output(**kv: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a") as f:
        for k, v in kv.items():
            f.write(f"{k}={v}\n")


def http_get(url: str, *, timeout: int = 60) -> requests.Response:
    r = requests.get(url, headers=DEFAULT_HEADERS, timeout=timeout)
    r.raise_for_status()
    return r


def write_archived_copy(
    agency: str,
    body: bytes,
    filename: str,
    today: str,
) -> Path:
    """Write the new file to archive/<agency>/<today>/ and return the path."""
    _, archive_dir, _ = agency_paths(agency)
    day_dir = archive_dir / today
    day_dir.mkdir(parents=True, exist_ok=True)
    target = day_dir / filename
    target.write_bytes(body)
    return target


def overwrite_current(
    agency: str,
    body: bytes,
    filename: str,
    extension: str,
) -> Path:
    """Write the new file to current/<agency>/<filename>, removing other files of the same extension."""
    current_dir, _, _ = agency_paths(agency)
    current_dir.mkdir(parents=True, exist_ok=True)
    for stale in current_dir.glob(f"*{extension}"):
        if stale.name != filename:
            stale.unlink()
    target = current_dir / filename
    target.write_bytes(body)
    return target


def process_download(
    *,
    agency: str,
    page_url: str,
    file_url: str,
    body: bytes,
    headers: dict,
    extension: str,
    extra_meta: dict | None = None,
    parse_rows: Callable[[Path], "tuple[dict, list[str]]"] | None = None,
    title_column: str | None = None,
    preview_columns: list[str] | None = None,
    pdf_note: bool = False,
) -> tuple[bool, dict, str]:
    """Common path: archive on change, diff vs prior, write current + metadata.

    `parse_rows(path)` must return `(rows_dict, columns_list)`.
    Returns (changed: bool, meta_for_outputs: dict, diff_summary: str).
    """
    import diff_lib

    now_iso = datetime.now(timezone.utc).isoformat()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    current_dir, archive_dir, meta_path = agency_paths(agency)
    current_dir.mkdir(parents=True, exist_ok=True)

    import urllib.parse as _u
    filename = _u.unquote(file_url.rsplit("/", 1)[-1])

    sha = hashlib.sha256(body).hexdigest()
    prior_meta: dict = {}
    if meta_path.exists():
        prior_meta = json.loads(meta_path.read_text())
    prior_files = prior_meta.get("files", [])
    prior = prior_files[0] if prior_files else None
    changed = (prior is None) or (prior.get("sha256") != sha)

    meta = {
        "filename": filename,
        "source_url": file_url,
        "sha256": sha,
        "bytes": len(body),
        "source_last_modified": headers.get("last-modified") or headers.get("Last-Modified"),
        "source_etag": headers.get("etag") or headers.get("ETag"),
        **(extra_meta or {}),
    }

    diff_summary = ""
    if changed:
        archive_path = write_archived_copy(agency, body, filename, today)
        prior_file = find_prior_file(archive_dir, extension, today)

        if prior_file is not None and parse_rows is not None:
            try:
                old_rows, _ = parse_rows(prior_file)
                new_rows, columns = parse_rows(archive_path)
                d = diff_lib.diff(
                    old_rows,
                    new_rows,
                    columns,
                    title_column=title_column,
                    preview_columns=preview_columns,
                )
                md = diff_lib.render_markdown(
                    d,
                    old_date=prior_file.parent.name,
                    new_date=today,
                    pdf_note=pdf_note,
                )
                (archive_path.parent / "changes.md").write_text(md + "\n")
                diff_summary = diff_lib.summarize_one_liner(d)
                meta["diff_summary"] = diff_summary
            except Exception as e:
                (archive_path.parent / "changes.md").write_text(
                    f"# Changes — {today}\n\nDiff failed: {e}\n"
                )
                meta["diff_error"] = str(e)
        else:
            (archive_path.parent / "changes.md").write_text(
                f"# Initial snapshot — {today}\n\nFirst recorded version; no prior to diff against.\n"
            )

        overwrite_current(agency, body, filename, extension)
        meta["last_changed_utc"] = now_iso
        metadata_doc = {
            "source_page_url": page_url,
            "last_checked_utc": now_iso,
            "files": [meta],
        }
        meta_path.write_text(json.dumps(metadata_doc, indent=2) + "\n")
    else:
        meta["last_changed_utc"] = prior.get("last_changed_utc")

    return changed, meta, diff_summary
