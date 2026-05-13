#!/usr/bin/env python3
"""Download the DOE (Department of Energy) procurement forecast XLSX.

Page is not behind Cloudflare. The XLSX link on the page embeds the
publication date in the filename, so we scrape the page each run to find
the current link.
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

AGENCY = "doe"
PAGE_URL = "https://www.energy.gov/osdbu/acquisition-forecast"
LINK_RE = re.compile(
    r'href="(https?://[^"]*energy\.gov/[^"]*[Aa]cquisition[^"]*[Ff]orecast[^"]*\.xlsx)"'
)

HEADER_ROW = 18
KEY_COLUMN = "Current Contract Number"
TITLE_COLUMN = "Acquisition Description"
PREVIEW_COLUMNS = [
    "Acquisition Description",
    "Program Office",
    "Estimated Value Range",
    "Performance End Date",
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
    headers = [str(h) if h is not None else f"_col{i}" for i, h in enumerate(rows[HEADER_ROW - 1])]
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
        raise RuntimeError(f"No DOE forecast XLSX link found on {PAGE_URL}")
    file_url = m.group(1)
    filename = urllib.parse.unquote(file_url.rsplit("/", 1)[-1])
    print(f"[fetch] file URL: {file_url}", flush=True)

    print(f"[fetch] downloading {filename}", flush=True)
    dl = common.http_get(file_url, timeout=120)
    body = dl.content
    headers = {k.lower(): v for k, v in dl.headers.items()}
    print(f"  -> {len(body):,} bytes  last-modified={headers.get('last-modified')}", flush=True)

    changed, meta, diff_summary = common.process_download(
        agency=AGENCY,
        page_url=PAGE_URL,
        file_url=file_url,
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
