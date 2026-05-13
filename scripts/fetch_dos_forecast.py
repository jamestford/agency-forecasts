#!/usr/bin/env python3
"""Download the DOS (Department of State) procurement forecast XLSX.

Page works with proper User-Agent (returns 'Technical Difficulties' for
bare curl). XLSX URL changes per fiscal year + quarter, e.g.
FY26-Procurement-Forecast-4.xlsx.
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

AGENCY = "dos"
PAGE_URL = "https://www.state.gov/Procurement-Forecast"
LINK_RE = re.compile(
    r'href="(https?://[^"]*state\.gov/[^"]*[Pp]rocurement[-_][Ff]orecast[^"]*\.xlsx)"'
)

HEADER_ROW = 1
TITLE_COLUMN = "Requirement Title"
PREVIEW_COLUMNS = [
    "Requirement Title",
    "Contracting Office",
    "NAICS Code",
    "Estimated Contract Value*",
    "Estimated Award FY Quarter",
]
# DOS has no single unique ID column. Composite key on 5 fields covers
# 99%+; the remaining ambiguous rows just won't be detected as "modified"
# (they'll appear as added+removed pairs if any field changes).
KEY_COMPOSITE = (
    "Requirement Title",
    "Contracting Office",
    "NAICS Code",
    "Estimated Award FY Quarter",
    "POC Name",
)


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
    headers = [str(h).strip() if h is not None else f"_col{i}" for i, h in enumerate(rows[HEADER_ROW - 1])]
    out: dict = {}
    for row in rows[HEADER_ROW:]:
        if not row or all(c is None or (isinstance(c, str) and not c.strip()) for c in row):
            continue
        row_dict = {h: _normalize(row[i]) if i < len(row) else None for i, h in enumerate(headers)}
        key_parts = tuple(str(row_dict.get(c, "") or "") for c in KEY_COMPOSITE)
        key = " | ".join(key_parts)
        if not key.strip(" |"):
            continue
        # On rare collisions (same composite), last-row-wins. Acceptable since
        # any change to a row that previously collided will still show up.
        out[key] = row_dict
    return out, headers


def main() -> int:
    print(f"[fetch] GET {PAGE_URL}", flush=True)
    page = common.http_get(PAGE_URL)
    m = LINK_RE.search(page.text)
    if not m:
        raise RuntimeError(f"No DOS forecast XLSX link found on {PAGE_URL}")
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
