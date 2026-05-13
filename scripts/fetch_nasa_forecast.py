#!/usr/bin/env python3
"""Download the NASA procurement forecast XLSX.

NASA's Agency-Wide Forecast lives at a stable URL (AcqForecastNew.xlsx)
and is regenerated when the underlying forecast is updated. Header row
1, key = `SourceID`.
"""
from __future__ import annotations

import datetime as _dt
import sys
import urllib.parse
from pathlib import Path

import openpyxl

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agency_common as common

AGENCY = "nasa"
PAGE_URL = "https://www.hq.nasa.gov/office/procurement/forecast/NAF.html"
FILE_URL = "https://www.hq.nasa.gov/office/procurement/forecast/AcqForecastNew.xlsx"

HEADER_ROW = 1
KEY_COLUMN = "SourceID"
TITLE_COLUMN = "TitleOfRequirement"
PREVIEW_COLUMNS = [
    "TitleOfRequirement",
    "BuyingOffice",
    "AcquisitionPhase",
    "EstimatedContractValue",
    "Anticipated Qtr of Award",
    "AnticipatedFYAward",
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
    if not rows:
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
        if key is None or (isinstance(key, str) and not str(key).strip()):
            continue
        out[key] = {h: _normalize(row[i]) if i < len(row) else None for i, h in enumerate(headers)}
    return out, headers


def main() -> int:
    print(f"[fetch] downloading {FILE_URL}", flush=True)
    dl = common.http_get(FILE_URL, timeout=120)
    body = dl.content
    headers = {k.lower(): v for k, v in dl.headers.items()}
    filename = urllib.parse.unquote(FILE_URL.rsplit("/", 1)[-1])
    print(f"  -> {len(body):,} bytes  last-modified={headers.get('last-modified')}", flush=True)

    changed, meta, diff_summary = common.process_download(
        agency=AGENCY,
        page_url=PAGE_URL,
        file_url=FILE_URL,
        body=body,
        headers=headers,
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
        source_last_modified=headers.get("last-modified") or "",
        diff_summary=diff_summary,
        archive_date=today if changed else "",
    )
    print(f"[fetch] done. changed={changed}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
