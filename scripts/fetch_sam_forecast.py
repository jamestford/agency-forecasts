#!/usr/bin/env python3
"""Fetch active Presolicitation opportunities from SAM.gov public API.

We track Presolicitation only (ptype=p) — the "a solicitation is
coming" announcement that's most actionable for forecast tracking.
Excluded: Sources Sought (r, market research) and Special Notice
(s, mixed bag including sole-source intents and informational).

We paginate over a rolling 90-day posted-date window with active=true.
Each opportunity is stored as a stripped record (~400 bytes) — enough for
diffing/triage; the full notice (description, attachments) lives on
sam.gov and is linked via `uiLink`.

API key is provided via the SAM_GOV_API_KEY env var (GitHub Actions secret).
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agency_common as common
import diff_lib

AGENCY = "sam"
API_URL = "https://api.sam.gov/opportunities/v2/search"
PTYPE = "p"  # Presolicitation only — see module docstring
WINDOW_DAYS = 90
PAGE_LIMIT = 1000

KEEP_FIELDS = (
    "noticeId",
    "title",
    "type",
    "baseType",
    "fullParentPathName",
    "fullParentPathCode",
    "organizationType",
    "postedDate",
    "responseDeadLine",
    "archiveDate",
    "archiveType",
    "naicsCode",
    "classificationCode",
    "typeOfSetAside",
    "typeOfSetAsideDescription",
    "solicitationNumber",
    "active",
    "uiLink",
)
DIFF_FIELDS = [
    "title",
    "type",
    "fullParentPathName",
    "postedDate",
    "responseDeadLine",
    "archiveDate",
    "naicsCode",
    "typeOfSetAside",
    "typeOfSetAsideDescription",
    "active",
]
PREVIEW_COLUMNS = [
    "title",
    "type",
    "fullParentPathName",
    "postedDate",
    "responseDeadLine",
    "typeOfSetAsideDescription",
]
TITLE_COLUMN = "title"


def normalize(opp: dict) -> dict:
    out = {k: opp.get(k) for k in KEEP_FIELDS}
    poc = opp.get("pointOfContact") or []
    if isinstance(poc, list) and poc:
        primary = poc[0]
        out["pocName"] = primary.get("fullName")
        out["pocEmail"] = primary.get("email")
    pop = opp.get("placeOfPerformance") or {}
    if isinstance(pop, dict):
        city = (pop.get("city") or {}).get("name") if isinstance(pop.get("city"), dict) else pop.get("city")
        state = (pop.get("state") or {}).get("code") if isinstance(pop.get("state"), dict) else pop.get("state")
        out["popCityState"] = f"{city or ''}, {state or ''}".strip(", ")
    return out


def fetch_all(api_key: str) -> tuple[list[dict], datetime]:
    now = datetime.now(timezone.utc)
    posted_to = now.strftime("%m/%d/%Y")
    posted_from = (now - timedelta(days=WINDOW_DAYS)).strftime("%m/%d/%Y")
    print(f"[fetch] window {posted_from} → {posted_to}", flush=True)

    rows: list[dict] = []
    offset = 0
    total = None
    while True:
        params = {
            "api_key": api_key,
            "ptype": PTYPE,
            "active": "true",
            "limit": str(PAGE_LIMIT),
            "offset": str(offset),
            "postedFrom": posted_from,
            "postedTo": posted_to,
        }
        r = requests.get(API_URL, params=params, timeout=60)
        if r.status_code != 200:
            raise RuntimeError(f"SAM.gov API HTTP {r.status_code}: {r.text[:300]}")
        data = r.json()
        if total is None:
            total = int(data.get("totalRecords") or 0)
            print(f"[fetch] total active forecast-stage notices: {total:,}", flush=True)
        page = data.get("opportunitiesData") or []
        rows.extend(page)
        print(f"[fetch] page offset={offset}: +{len(page)} ({len(rows)}/{total})", flush=True)
        if not page or len(rows) >= total:
            break
        offset += PAGE_LIMIT
        time.sleep(0.5)
    return rows, now


def main() -> int:
    api_key = os.environ.get("SAM_GOV_API_KEY")
    if not api_key:
        raise RuntimeError("SAM_GOV_API_KEY env var is required")

    raw_opps, now = fetch_all(api_key)
    today = now.strftime("%Y-%m-%d")
    now_iso = now.isoformat()

    rows: dict[str, dict] = {}
    for op in raw_opps:
        nid = op.get("noticeId")
        if not nid:
            continue
        rows[nid] = normalize(op)
    print(f"[fetch] {len(rows):,} unique noticeIds", flush=True)

    sorted_rows = sorted(
        rows.values(),
        key=lambda r: (r.get("postedDate") or "", r.get("noticeId") or ""),
    )
    # Content SHA excludes fetched_utc so it's stable across runs that fetch
    # the same data.
    content_sha = hashlib.sha256(
        json.dumps(sorted_rows, indent=2, default=str, sort_keys=True).encode("utf-8")
    ).hexdigest()
    payload = {
        "source": "SAM.gov Opportunities API v2",
        "filter": "ptype=p (Presolicitation only), active=true",
        "window_days": WINDOW_DAYS,
        "fetched_utc": now_iso,
        "content_sha256": content_sha,
        "count": len(rows),
        "opportunities": sorted_rows,
    }
    new_body = (json.dumps(payload, indent=2, default=str) + "\n").encode("utf-8")
    print(f"[fetch] payload: {len(new_body):,} bytes, content_sha={content_sha[:12]}", flush=True)

    current_dir, archive_dir, meta_path = common.agency_paths(AGENCY)
    current_dir.mkdir(parents=True, exist_ok=True)

    # SHA-based fast path
    prior_meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    prior_files_meta = prior_meta.get("files") or []
    prior_meta_file = prior_files_meta[0] if prior_files_meta else None
    if prior_meta_file and prior_meta_file.get("content_sha256") == content_sha:
        print(f"  -> unchanged (content_sha matches; last changed {prior_meta_file.get('last_changed_utc')})", flush=True)
        common.emit_output(
            changed="false",
            file_count="1",
            filenames="opportunities.json",
            diff_summary="",
            archive_date="",
            record_count=str(len(rows)),
        )
        return 0

    # Content differs — compute row-level diff against current/sam/opportunities.json
    current_file = current_dir / "opportunities.json"
    prior_rows: dict[str, dict] = {}
    if current_file.exists():
        try:
            prior_payload = json.loads(current_file.read_text())
            for o in prior_payload.get("opportunities", []):
                if o.get("noticeId"):
                    prior_rows[o["noticeId"]] = o
        except Exception as e:
            print(f"[fetch] (warn) failed to parse current snapshot: {e}", flush=True)

    d = diff_lib.diff(
        prior_rows,
        rows,
        DIFF_FIELDS,
        title_column=TITLE_COLUMN,
        preview_columns=PREVIEW_COLUMNS,
    )
    changed = True  # we got here only because SHA differs

    diff_summary = ""
    archive_day = archive_dir / today
    archive_day.mkdir(parents=True, exist_ok=True)
    (archive_day / "opportunities.json").write_bytes(new_body)
    if prior_rows:
        md = diff_lib.render_markdown(
            d, old_date=prior_meta.get("last_checked_utc", "previous")[:10] or "previous", new_date=today
        )
        (archive_day / "changes.md").write_text(md + "\n")
        diff_summary = diff_lib.summarize_one_liner(d)
    else:
        (archive_day / "changes.md").write_text(
            f"# Initial snapshot — {today}\n\n{len(rows):,} active forecast-stage opportunities recorded. No prior to diff against.\n"
        )
    (current_dir / "opportunities.json").write_bytes(new_body)

    meta_path.write_text(
        json.dumps(
            {
                "source": "SAM.gov Opportunities API v2",
                "source_page_url": "https://sam.gov/opportunities",
                "filter": "ptype=p (Presolicitation only), active=true",
                "window_days": WINDOW_DAYS,
                "last_checked_utc": now_iso,
                "files": [
                    {
                        "filename": "opportunities.json",
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
        changed="true" if changed else "false",
        file_count="1",
        filenames="opportunities.json",
        diff_summary=diff_summary,
        archive_date=today if changed else "",
        record_count=str(len(rows)),
    )
    print(f"[fetch] done. changed={changed}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
