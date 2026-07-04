#!/usr/bin/env python3
"""Download the ED (Department of Education) procurement forecast XLSX.

ED migrated from PDF to XLSX in mid-2026. The new file lives at
/media/document/us-department-of-education-procurement-forecast-<Mon>-<YYYY>-<ID>.xlsx
on ed.gov. Header row 3, key = `Tracking No.` (100% unique).

The ID format also changed with this migration:
  old (PDF):  FY26AP3235
  new (XLSX): FY26-AP-3235
so the first XLSX snapshot's diff vs the last PDF snapshot will show
every row as removed+added. Real row-level diffs resume after that.
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

AGENCY = "ed"
# ed.gov's older URL /about/doing-business-ed/forecast-of-ed-contract-opportunities
# 301-redirects to this longer path as of mid-2026.
PAGE_URL = "https://www.ed.gov/about/doing-business-ed/contract-opportunities/forecast-of-ed-contract-opportunities"
LINK_RE = re.compile(
    r'href="((?:https?://[^"]*)?/media/document/[^"]*procurement[-_]forecast[^"]*\.xlsx)"',
    re.IGNORECASE,
)

HEADER_ROW = 3
KEY_COLUMN = "Tracking No."
TITLE_COLUMN = "Contract Name"
PREVIEW_COLUMNS = [
    "Contract Name",
    "Funding Office",
    "Requirement Type",
    "Estimated Value of Contract $ Range",
    "Target Award Quarter",
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
        # Save snapshot for troubleshooting (workflow uploads it as an artifact)
        common.agency_paths(AGENCY)[0].mkdir(parents=True, exist_ok=True)
        (common.agency_paths(AGENCY)[0] / "_no-xlsx-snapshot.html").write_text(page.text)
        raise RuntimeError(f"No ED forecast XLSX link found on {PAGE_URL}. Snapshot saved.")
    file_url = m.group(1)
    if file_url.startswith("/"):
        file_url = "https://www.ed.gov" + file_url
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
