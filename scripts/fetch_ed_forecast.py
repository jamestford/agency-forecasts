#!/usr/bin/env python3
"""Download the ED procurement forecast PDF and commit on change.

ED's page is NOT behind Cloudflare, so we use plain requests rather than a
headless browser. The PDF filename embeds the publication date and a unique
ID, so we scrape the page each run to find the current link.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pdf_diff

PAGE_URL = "https://www.ed.gov/about/doing-business-ed/forecast-of-ed-contract-opportunities"
ALLOWED_HOST = "www.ed.gov"
AGENCY = "ed"

REPO_ROOT = Path(__file__).resolve().parent.parent
CURRENT_DIR = REPO_ROOT / "current" / AGENCY
ARCHIVE_DIR = REPO_ROOT / "archive" / AGENCY
METADATA_PATH = CURRENT_DIR / "metadata.json"
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PDF_HREF_RE = re.compile(
    r'href="(/media/document/[^"]*procurement[-_]forecast[^"]*\.pdf)"',
    re.IGNORECASE,
)

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _find_prior_pdf(filename_hint: str | None, today: str) -> Path | None:
    """Most recent prior PDF in archive/ed/, regardless of exact filename."""
    if not ARCHIVE_DIR.exists():
        return None
    dated = sorted(
        d for d in ARCHIVE_DIR.iterdir()
        if d.is_dir() and DATE_RE.match(d.name) and d.name < today
    )
    for d in reversed(dated):
        for p in d.glob("*.pdf"):
            return p
    return None


def emit_output(**kv: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a") as f:
        for k, v in kv.items():
            f.write(f"{k}={v}\n")


def main() -> int:
    CURRENT_DIR.mkdir(parents=True, exist_ok=True)
    now_iso = datetime.now(timezone.utc).isoformat()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    print(f"[fetch] GET {PAGE_URL}", flush=True)
    r = requests.get(PAGE_URL, headers={"User-Agent": UA}, timeout=60)
    r.raise_for_status()
    html = r.text

    matches = sorted(set(m.group(1) for m in PDF_HREF_RE.finditer(html)))
    if not matches:
        snap = CURRENT_DIR / "_no-pdf-snapshot.html"
        snap.write_text(html)
        raise RuntimeError(
            f"No procurement-forecast PDF link found on {PAGE_URL}. "
            f"Snapshot saved to {snap.relative_to(REPO_ROOT)}"
        )
    pdf_path = matches[0]
    pdf_url = urllib.parse.urljoin("https://www.ed.gov", pdf_path)
    filename = urllib.parse.unquote(pdf_path.rsplit("/", 1)[-1])
    print(f"[fetch] PDF link: {pdf_url}", flush=True)

    print(f"[fetch] downloading {filename}", flush=True)
    dl = requests.get(pdf_url, headers={"User-Agent": UA}, timeout=120, stream=False)
    dl.raise_for_status()
    body = dl.content
    sha = hashlib.sha256(body).hexdigest()
    headers = {k.lower(): v for k, v in dl.headers.items()}
    print(f"  -> {len(body):,} bytes, sha256={sha[:12]}, last-modified={headers.get('last-modified')}", flush=True)

    prior_meta: dict = {}
    if METADATA_PATH.exists():
        prior_meta = json.loads(METADATA_PATH.read_text())
    prior_files = {f["filename"]: f for f in prior_meta.get("files", [])}
    prior_any = next(iter(prior_meta.get("files", [])), None)

    file_changed = True
    if prior_any and prior_any.get("sha256") == sha:
        file_changed = False
    elif prior_any and prior_any.get("filename") == filename and prior_any.get("sha256") == sha:
        file_changed = False

    meta = {
        "filename": filename,
        "source_url": pdf_url,
        "sha256": sha,
        "bytes": len(body),
        "source_last_modified": headers.get("last-modified"),
        "source_etag": headers.get("etag"),
    }

    diff_summary = ""
    if file_changed:
        archive_day = ARCHIVE_DIR / today
        archive_day.mkdir(parents=True, exist_ok=True)
        (archive_day / filename).write_bytes(body)
        prior_pdf = _find_prior_pdf(filename, today)
        if prior_pdf is not None:
            try:
                diff_result = pdf_diff.diff(prior_pdf, archive_day / filename)
                md = pdf_diff.render_markdown(
                    diff_result,
                    old_date=prior_pdf.parent.name,
                    new_date=today,
                )
                (archive_day / "changes.md").write_text(md + "\n")
                diff_summary = pdf_diff.summarize_one_liner(diff_result)
                meta["diff_summary"] = diff_summary
                print(f"  -> diff: {diff_summary}", flush=True)
            except Exception as e:
                msg = f"# Changes — {today}\n\nDiff failed: {e}\n"
                (archive_day / "changes.md").write_text(msg)
                print(f"  -> diff failed: {e}", flush=True)
        else:
            (archive_day / "changes.md").write_text(
                f"# Initial snapshot — {today}\n\nFirst recorded version; no prior to diff against.\n"
            )
            print("  -> initial archive snapshot (no prior to diff)", flush=True)

        # Replace current/ contents (clean out any stale PDFs from prior runs
        # since ED's filename changes with each publication)
        for old_pdf in CURRENT_DIR.glob("*.pdf"):
            if old_pdf.name != filename:
                old_pdf.unlink()
        (CURRENT_DIR / filename).write_bytes(body)

        meta["last_changed_utc"] = now_iso
        metadata = {
            "source_page_url": PAGE_URL,
            "last_checked_utc": now_iso,
            "files": [meta],
        }
        METADATA_PATH.write_text(json.dumps(metadata, indent=2) + "\n")
    else:
        meta["last_changed_utc"] = prior_any.get("last_changed_utc")
        print(f"  -> unchanged (sha matches; last changed {meta['last_changed_utc']})", flush=True)

    emit_output(
        changed="true" if file_changed else "false",
        file_count="1",
        filenames=filename,
        source_last_modified=headers.get("last-modified") or "",
        diff_summary=diff_summary,
        archive_date=today if file_changed else "",
    )

    print(f"[fetch] done. changed={file_changed}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
