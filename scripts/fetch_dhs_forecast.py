#!/usr/bin/env python3
"""Fetch DHS APFS (Acquisition Planning Forecast System) records via public JSON API.

The APFS public listing is served as a single JSON array at
https://apfs-cloud.dhs.gov/api/forecast/ — no auth, no pagination needed.
~718 records as of 2026-05-13, each ~2.5 KB. Key = `apfs_number`
(e.g., "F2026073112"). Data is more upstream than SAM Presolicitation —
zero overlap observed at probe time.

Stripped to a useful core (~400 bytes/record) to keep snapshots lean.
"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agency_common as common
import diff_lib

AGENCY = "dhs"
PAGE_URL = "https://apfs-cloud.dhs.gov/forecast/"
API_URL = "https://apfs-cloud.dhs.gov/api/forecast/"

KEEP_FIELDS = (
    "apfs_number",
    "requirements_title",
    "organization",
    "contracting_office",
    "current_state",
    "contract_status",
    "competitive",
    "small_business_program",
    "small_business_set_aside",
    "contract_vehicle",
    "contract_type",
    "naics",
    "fiscal_year",
    "award_quarter",
    "anticipated_award_date",
    "estimated_solicitation_release_date",
    "estimated_period_of_performance_start",
    "estimated_period_of_performance_end",
    "place_of_performance_city",
    "place_of_performance_state",
    "publish_date",
    "last_updated_date",
    "contract_number",
    "contractor",
)
DIFF_FIELDS = [
    "requirements_title",
    "organization",
    "current_state",
    "contract_status",
    "small_business_program",
    "small_business_set_aside",
    "fiscal_year",
    "award_quarter",
    "anticipated_award_date",
    "estimated_solicitation_release_date",
    "naics",
    "contract_number",
    "contractor",
]
PREVIEW_COLUMNS = [
    "requirements_title",
    "organization",
    "anticipated_award_date",
    "small_business_program",
    "naics",
    "dollar_range_display",
]
TITLE_COLUMN = "requirements_title"


def normalize(rec: dict) -> dict:
    out = {k: rec.get(k) for k in KEEP_FIELDS}
    dr = rec.get("dollar_range") or {}
    if isinstance(dr, dict):
        out["dollar_range_display"] = dr.get("display_name")
    # POCs (primary contact)
    out["poc_name"] = (
        f"{rec.get('requirements_contact_first_name') or ''} "
        f"{rec.get('requirements_contact_last_name') or ''}"
    ).strip() or None
    out["poc_email"] = rec.get("requirements_contact_email")
    out["sbs_email"] = rec.get("sbs_coordinator_email")
    return out


def main() -> int:
    print(f"[fetch] GET {API_URL}", flush=True)
    r = requests.get(
        API_URL,
        headers={"User-Agent": common.UA, "Accept": "application/json"},
        timeout=60,
    )
    r.raise_for_status()
    raw = r.json()
    print(f"[fetch] {len(raw)} records from API", flush=True)

    rows = {}
    for rec in raw:
        key = rec.get("apfs_number")
        if not key:
            continue
        rows[key] = normalize(rec)
    print(f"[fetch] {len(rows)} unique apfs_numbers", flush=True)

    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    now_iso = now.isoformat()

    sorted_rows = sorted(
        rows.values(),
        key=lambda r: (r.get("apfs_number") or ""),
    )
    content_sha = hashlib.sha256(
        json.dumps(sorted_rows, indent=2, default=str, sort_keys=True).encode("utf-8")
    ).hexdigest()
    payload = {
        "source": "DHS APFS public API",
        "source_url": API_URL,
        "fetched_utc": now_iso,
        "content_sha256": content_sha,
        "count": len(rows),
        "records": sorted_rows,
    }
    new_body = (json.dumps(payload, indent=2, default=str) + "\n").encode("utf-8")
    print(f"[fetch] payload: {len(new_body):,} bytes, content_sha={content_sha[:12]}", flush=True)

    current_dir, archive_dir, meta_path = common.agency_paths(AGENCY)
    current_dir.mkdir(parents=True, exist_ok=True)

    # SHA fast path
    prior_meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    prior_files = prior_meta.get("files") or []
    prior = prior_files[0] if prior_files else None
    if prior and prior.get("content_sha256") == content_sha:
        print(f"  -> unchanged (content_sha matches; last changed {prior.get('last_changed_utc')})", flush=True)
        common.emit_output(
            changed="false", file_count="1", filenames="forecast.json",
            diff_summary="", archive_date="", record_count=str(len(rows)),
        )
        return 0

    # Content differs — diff vs current/dhs/forecast.json
    current_file = current_dir / "forecast.json"
    prior_rows: dict = {}
    if current_file.exists():
        try:
            prev = json.loads(current_file.read_text())
            for o in prev.get("records", []):
                if o.get("apfs_number"):
                    prior_rows[o["apfs_number"]] = o
        except Exception as e:
            print(f"[fetch] (warn) failed to parse current: {e}", flush=True)

    d = diff_lib.diff(
        prior_rows, rows, DIFF_FIELDS,
        title_column=TITLE_COLUMN, preview_columns=PREVIEW_COLUMNS,
    )
    diff_summary = diff_lib.summarize_one_liner(d)

    # Write archive + current
    archive_day = archive_dir / today
    archive_day.mkdir(parents=True, exist_ok=True)
    (archive_day / "forecast.json").write_bytes(new_body)
    if prior_rows:
        md = diff_lib.render_markdown(
            d, old_date=(prior_meta.get("last_checked_utc", "previous") or "previous")[:10], new_date=today,
        )
        (archive_day / "changes.md").write_text(md + "\n")
    else:
        (archive_day / "changes.md").write_text(
            f"# Initial snapshot — {today}\n\n{len(rows)} DHS APFS records recorded. No prior to diff against.\n"
        )
    (current_dir / "forecast.json").write_bytes(new_body)

    meta_path.write_text(
        json.dumps(
            {
                "source": "DHS APFS public API",
                "source_page_url": PAGE_URL,
                "source_api_url": API_URL,
                "last_checked_utc": now_iso,
                "files": [
                    {
                        "filename": "forecast.json",
                        "content_sha256": content_sha,
                        "bytes": len(new_body),
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
        filenames="forecast.json",
        diff_summary=diff_summary,
        archive_date=today,
        record_count=str(len(rows)),
    )
    print(f"[fetch] done.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
