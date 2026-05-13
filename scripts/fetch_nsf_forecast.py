#!/usr/bin/env python3
"""Download the NSF (National Science Foundation) procurement forecast PDF.

Page is not behind Cloudflare. The PDF link sits at a stable-looking URL
(/2023-10/NSF-Acquisition-Forecast.pdf), but the *content* at that URL
changes when NSF updates the forecast — title page shows the actual
date (FY2025, July 24, 2025 as of this build).

Schema: 11 columns, key = `Contract No.` (e.g., '49100419F1050').
Data rows are interleaved with "Office of ..." section header rows that
have only the first cell populated.
"""
from __future__ import annotations

import datetime as _dt
import re
import sys
import urllib.parse
from pathlib import Path

import pdfplumber

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agency_common as common

AGENCY = "nsf"
PAGE_URL = "https://www.nsf.gov/about/contracting/forecast.jsp"
LINK_RE = re.compile(
    r'href="(https?://[^"]*nsf[^"]*[Aa]cquisition[-_][Ff]orecast[^"]*\.pdf)"'
)

CANONICAL_HEADERS = [
    "Division",
    "Title of Requirement",
    "Description of Requirement",
    "Previous Contract",
    "Contract No.",
    "Contract Expiration Date",
    "Expected Dollar Range",
    "NAICS",
    "Acquisition Announcement QTR",
    "Small Business Program",
    "Status",
]
KEY_COLUMN_INDEX = 4  # Contract No.
TITLE_COLUMN = "Title of Requirement"
PREVIEW_COLUMNS = [
    "Title of Requirement",
    "Division",
    "Expected Dollar Range",
    "Acquisition Announcement QTR",
    "Status",
]


def _is_data_row(row: list) -> bool:
    if not row or len(row) <= KEY_COLUMN_INDEX:
        return False
    key = row[KEY_COLUMN_INDEX]
    if not key or not isinstance(key, str):
        return False
    key = key.strip()
    # Contract numbers are alphanumeric, typically 10+ chars and contain digits
    return len(key) >= 8 and any(c.isdigit() for c in key)


def parse_rows(path: Path) -> tuple[dict, list[str]]:
    out: dict = {}
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            tables = page.extract_tables() or []
            for table in tables:
                for raw_row in table:
                    if not _is_data_row(raw_row):
                        continue
                    row = list(raw_row)
                    while len(row) < len(CANONICAL_HEADERS):
                        row.append(None)
                    row = row[: len(CANONICAL_HEADERS)]
                    key = (row[KEY_COLUMN_INDEX] or "").strip()
                    if not key:
                        continue
                    out[key] = {
                        h: (str(v).strip() if v is not None else "")
                        for h, v in zip(CANONICAL_HEADERS, row)
                    }
    return out, CANONICAL_HEADERS


def main() -> int:
    print(f"[fetch] GET {PAGE_URL}", flush=True)
    page = common.http_get(PAGE_URL)
    m = LINK_RE.search(page.text)
    if not m:
        raise RuntimeError(f"No NSF forecast PDF link found on {PAGE_URL}")
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
        extension=".pdf",
        parse_rows=parse_rows,
        title_column=TITLE_COLUMN,
        preview_columns=PREVIEW_COLUMNS,
        pdf_note=True,
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
