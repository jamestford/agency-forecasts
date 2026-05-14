#!/usr/bin/env python3
"""Fetch Treasury's forecast via Salesforce Lightning app.

The data lives in a Salesforce Lightning Web Components app at
osdbu.forecast.treasury.gov. No static file URL — the "Download
Opportunity Data" button generates an HTML-payload .xls download
on click. We drive it with Camoufox.

The downloaded file claims to be .xls but is actually an HTML <table>
(common "Export to Excel" trick). We parse it with BeautifulSoup.

Key column: `ShopCart/req` (100% unique across 202 rows as of probe).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup
from camoufox.async_api import AsyncCamoufox

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agency_common as common
import diff_lib

AGENCY = "treasury"
PAGE_URL = "https://osdbu.forecast.treasury.gov/"
DOWNLOAD_BUTTON_TEXT = "Download Opportunity Data"
OUTPUT_FILENAME = "Opportunity Data.xls"  # what Treasury suggests

KEY_COLUMN = "ShopCart/req"
TITLE_COLUMN = "Description"
DIFF_FIELDS = [
    "Bureau",
    "Description",
    "Type of Requirement",
    "Status",
    "Place of Performance",
    "Award Type",
    "Contract Type",
    "NAICS",
    "PSC",
    "Contract Number",
    "Agency",
    "Program Office",
    "Estimated Total Contract Value",
    "Type of Small Business Set-aside",
    "Small Business Set-aside",
    "Competition",
    "Extent Competed",
    "Projected Award FY_Qtr",
    "Projected Contract Vehicle",
    "Project Period of Performance Start",
    "Project Period of Performance End",
]
PREVIEW_COLUMNS = [
    "Description",
    "Bureau",
    "Status",
    "Estimated Total Contract Value",
    "Projected Award FY_Qtr",
    "NAICS",
]


def parse_html_xls(body: bytes) -> tuple[dict, list[str]]:
    """Parse the HTML-wrapped 'xls' download into {ShopCart/req: row dict}."""
    soup = BeautifulSoup(body, "html.parser")
    rows = soup.find_all("tr")
    if not rows:
        return {}, []
    headers = [c.get_text(strip=True) for c in rows[0].find_all(["th", "td"])]
    out: dict = {}
    for r in rows[1:]:
        cells = [c.get_text(strip=True) for c in r.find_all(["th", "td"])]
        if len(cells) != len(headers):
            continue
        row_dict = dict(zip(headers, cells))
        key = row_dict.get(KEY_COLUMN)
        if not key:
            continue
        out[key] = row_dict
    return out, headers


async def download_via_camoufox() -> bytes:
    async with AsyncCamoufox(
        headless=True,
        humanize=True,
        locale="en-US",
        geoip=True,
        os=["macos", "windows"],
        i_know_what_im_doing=True,
    ) as browser:
        page = await browser.new_page()
        print(f"[fetch] GET {PAGE_URL}", flush=True)
        await page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=90000)
        # Salesforce Lightning takes a beat to render
        await asyncio.sleep(10)
        title = await page.title()
        print(f"[fetch] page loaded, title={title!r}", flush=True)

        async with page.expect_download(timeout=30000) as dl_info:
            await page.get_by_text(DOWNLOAD_BUTTON_TEXT, exact=False).first.click(timeout=15000)
        dl = await dl_info.value
        local_path = await dl.path()
        body = Path(local_path).read_bytes()
        print(f"[fetch] downloaded {dl.suggested_filename!r}: {len(body):,} bytes", flush=True)
        return body


def main() -> int:
    body = asyncio.run(download_via_camoufox())

    rows, headers = parse_html_xls(body)
    print(f"[fetch] parsed {len(rows)} rows × {len(headers)} cols", flush=True)
    if not rows:
        raise RuntimeError("Parsed zero rows from Treasury download")

    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    now_iso = now.isoformat()

    # Content SHA over normalized row set (independent of fetch timestamp)
    content_sha = hashlib.sha256(
        json.dumps(rows, indent=2, default=str, sort_keys=True).encode("utf-8")
    ).hexdigest()

    current_dir, archive_dir, meta_path = common.agency_paths(AGENCY)
    current_dir.mkdir(parents=True, exist_ok=True)

    # Fast path: content SHA match
    prior_meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    prior_files = prior_meta.get("files") or []
    prior = prior_files[0] if prior_files else None
    if prior and prior.get("content_sha256") == content_sha:
        print(f"  -> unchanged (content_sha matches; last changed {prior.get('last_changed_utc')})", flush=True)
        common.emit_output(
            changed="false", file_count="1", filenames=OUTPUT_FILENAME,
            diff_summary="", archive_date="", record_count=str(len(rows)),
        )
        return 0

    # Diff vs current/treasury/Opportunity Data.xls
    current_file = current_dir / OUTPUT_FILENAME
    prior_rows: dict = {}
    if current_file.exists():
        try:
            prior_rows, _ = parse_html_xls(current_file.read_bytes())
        except Exception as e:
            print(f"[fetch] (warn) failed to parse prior: {e}", flush=True)

    d = diff_lib.diff(
        prior_rows, rows, DIFF_FIELDS,
        title_column=TITLE_COLUMN, preview_columns=PREVIEW_COLUMNS,
    )
    diff_summary = diff_lib.summarize_one_liner(d)

    archive_day = archive_dir / today
    archive_day.mkdir(parents=True, exist_ok=True)
    (archive_day / OUTPUT_FILENAME).write_bytes(body)
    if prior_rows:
        md = diff_lib.render_markdown(
            d, old_date=(prior_meta.get("last_checked_utc", "previous") or "previous")[:10], new_date=today,
        )
        (archive_day / "changes.md").write_text(md + "\n")
    else:
        (archive_day / "changes.md").write_text(
            f"# Initial snapshot — {today}\n\n{len(rows)} Treasury opportunities recorded. No prior to diff against.\n"
        )
    (current_dir / OUTPUT_FILENAME).write_bytes(body)

    meta_path.write_text(
        json.dumps(
            {
                "source_page_url": PAGE_URL,
                "source": "Treasury OSDBU Dynamic Forecast (Salesforce Lightning)",
                "last_checked_utc": now_iso,
                "files": [
                    {
                        "filename": OUTPUT_FILENAME,
                        "content_sha256": content_sha,
                        "bytes": len(body),
                        "count": len(rows),
                        "diff_summary": diff_summary,
                        "last_changed_utc": now_iso,
                    }
                ],
            },
            indent=2,
        )
        + "\n"
    )

    print(f"  -> CHANGED. diff: {diff_summary or '(initial)'}", flush=True)
    common.emit_output(
        changed="true",
        file_count="1",
        filenames=OUTPUT_FILENAME,
        diff_summary=diff_summary,
        archive_date=today,
        record_count=str(len(rows)),
    )
    print(f"[fetch] done.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
