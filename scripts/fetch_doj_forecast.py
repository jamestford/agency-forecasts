#!/usr/bin/env python3
"""Download the DOJ procurement forecast XLSX.

DOJ publishes a single XLSX with all anticipated procurements. Header
row is at row 13 (rows 1-12 are title/preamble blocks), data from row
14. Key = `Action Tracking Number` (~96% unique; ~19 rows collapse
under this key — acceptable for an action-tracker scope).
"""
from __future__ import annotations

import datetime as _dt
import re
import sys
import urllib.parse
from pathlib import Path

import openpyxl

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agency_common as common

AGENCY = "doj"
PAGE_URL = "https://www.justice.gov/jmd/doj-forecast-contracting-opportunities"
# Match the "Download the Excel File" link. On the page it appears as a
# relative path like /media/1381791/dl — match that form and absolutize.
LINK_RE = re.compile(
    r'href="((?:https?://[^"]*)?/media/\d+/dl)(?:\?[^"]*)?"',
    re.IGNORECASE,
)

HEADER_ROW = 13
KEY_COLUMN = "Action Tracking Number"
TITLE_COLUMN = "Contract Name"
PREVIEW_COLUMNS = [
    "Contract Name",
    "Bureau",
    "NAICS Code",
    "Estimated Total Contract Value (Range)",
    "Target Solicitation Date",
    "Target Award Date",
]


def _normalize(v):
    if isinstance(v, _dt.datetime):
        return v.isoformat()
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v


def parse_rows(path: Path) -> tuple[dict, list[str]]:
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if len(rows) < HEADER_ROW:
        return {}, []
    headers = [
        str(h).strip() if h is not None else f"_col{i}"
        for i, h in enumerate(rows[HEADER_ROW - 1])
    ]
    try:
        key_idx = headers.index(KEY_COLUMN)
    except ValueError:
        return {}, headers
    out: dict = {}
    for row in rows[HEADER_ROW:]:
        if not row or all(c is None or (isinstance(c, str) and not c.strip()) for c in row):
            continue
        key = row[key_idx]
        if key is None or (isinstance(key, str) and not key.strip()):
            continue
        out[key] = {h: _normalize(row[i]) if i < len(row) else None for i, h in enumerate(headers)}
    return out, headers


def main() -> int:
    print(f"[fetch] GET {PAGE_URL}", flush=True)
    page = common.http_get(PAGE_URL)
    m = LINK_RE.search(page.text)
    if not m:
        raise RuntimeError(f"No DOJ forecast XLSX link found on {PAGE_URL}")
    file_url = m.group(1)
    if file_url.startswith("/"):
        file_url = "https://www.justice.gov" + file_url
    print(f"[fetch] file URL: {file_url}", flush=True)

    print(f"[fetch] downloading", flush=True)
    dl = common.http_get(file_url, timeout=120)
    body = dl.content
    headers_in = {k.lower(): v for k, v in dl.headers.items()}
    # Filename from Content-Disposition or default
    cd = headers_in.get("content-disposition") or ""
    m2 = re.search(r'filename="?([^";]+)', cd)
    filename = m2.group(1) if m2 else f"doj-forecast.xlsx"
    print(f"  -> {len(body):,} bytes, filename={filename!r}, last-modified={headers_in.get('last-modified')}", flush=True)

    changed, meta, diff_summary = common.process_download(
        agency=AGENCY,
        page_url=PAGE_URL,
        file_url=file_url,
        body=body,
        headers={**headers_in, "filename": filename},
        extension=".xlsx",
        parse_rows=parse_rows,
        title_column=TITLE_COLUMN,
        preview_columns=PREVIEW_COLUMNS,
    )

    if changed:
        print(f"  -> CHANGED. diff: {diff_summary or '(initial)'}", flush=True)
    else:
        print(f"  -> unchanged (sha matches; last changed {meta['last_changed_utc']})", flush=True)

    today = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
    common.emit_output(
        changed="true" if changed else "false",
        file_count="1",
        filenames=filename,
        source_last_modified=headers_in.get("last-modified") or "",
        diff_summary=diff_summary,
        archive_date=today if changed else "",
    )
    print(f"[fetch] done. changed={changed}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
