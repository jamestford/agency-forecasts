#!/usr/bin/env python3
"""Download the HUD procurement forecast XLSX.

HUD publishes a single XLSX with all planned procurements (FY26-27 as of
2026-05-13). Direct URL, no CF/Akamai. Header row 4, key = `Plan Number`.
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

AGENCY = "hud"
# Original /program_offices/sdb/4cast meta-refreshes to this; requests
# follows HTTP redirects but not <meta http-equiv="refresh">.
PAGE_URL = "https://www.hud.gov/stat/sdb/forecast"
# Direct file URL (filename embeds FY range, may change between fiscal years).
# Page uses a relative href like /sites/dfiles/SDB/documents/HUD-Forecast-FY26-27.xlsx.
LINK_RE = re.compile(
    r'href="((?:https?://[^"]*)?/[^"]*HUD-Forecast[^"]*\.xlsx)"', re.IGNORECASE
)

HEADER_ROW = 4
KEY_COLUMN = "Plan Number"
TITLE_COLUMN = "Description"
PREVIEW_COLUMNS = [
    "Description",
    "HUD Office",
    "Type of Competition",
    "Total Contract Value Dollar Range",
    "Solicitation Release Date (Month)",
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
        raise RuntimeError(f"No HUD forecast XLSX link found on {PAGE_URL}")
    file_url = m.group(1)
    if file_url.startswith("/"):
        file_url = "https://www.hud.gov" + file_url
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
