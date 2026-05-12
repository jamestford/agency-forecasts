#!/usr/bin/env python3
"""Download the DOC procurement forecast using Camoufox.

commerce.gov is behind Cloudflare's managed challenge. Plain HTTP fetchers
and even stock headless Chromium get blocked. Camoufox is a custom-patched
Firefox build designed to defeat CF's automation detection.

Writes the latest file to current/ and emits GitHub Actions outputs the
workflow uses to decide whether to commit.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import shutil
import sys
import urllib.parse
import zipfile
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree as ET

from camoufox.async_api import AsyncCamoufox

sys.path.insert(0, str(Path(__file__).resolve().parent))
import xlsx_diff

PAGE_URL = "https://www.commerce.gov/oam/industry/procurement-forecasts"
ALLOWED_HOST = "www.commerce.gov"
DOC_EXTENSIONS = (".xlsx", ".xls", ".pdf", ".doc", ".docx", ".csv")
FORECAST_KEYWORDS = ("forecast",)

AGENCY = "doc"
REPO_ROOT = Path(__file__).resolve().parent.parent
CURRENT_DIR = REPO_ROOT / "current" / AGENCY
ARCHIVE_DIR = REPO_ROOT / "archive" / AGENCY
METADATA_PATH = CURRENT_DIR / "metadata.json"
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_xlsx_modified(data: bytes) -> str | None:
    try:
        with zipfile.ZipFile(BytesIO(data)) as zf:
            with zf.open("docProps/core.xml") as f:
                root = ET.parse(f).getroot()
        el = root.find("{http://purl.org/dc/terms/}modified")
        return el.text if el is not None else None
    except Exception as e:
        print(f"[fetch] (warn) could not parse XLSX modified date: {e}", flush=True)
        return None


_FETCH_JS = """
async (url) => {
  try {
    const r = await fetch(url, {credentials: 'include', redirect: 'follow'});
    const lastModified = r.headers.get('last-modified');
    const etag = r.headers.get('etag');
    const contentType = r.headers.get('content-type');
    if (!r.ok) {
      return {error: true, status: r.status, statusText: r.statusText,
              lastModified, etag, contentType};
    }
    const blob = await r.blob();
    const base64 = await new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => {
        const result = String(reader.result);
        const comma = result.indexOf(',');
        resolve(comma >= 0 ? result.slice(comma + 1) : result);
      };
      reader.onerror = () => reject(reader.error);
      reader.readAsDataURL(blob);
    });
    return {ok: true, base64, size: blob.size,
            lastModified, etag, contentType};
  } catch (e) {
    return {error: true, message: String(e && e.message ? e.message : e)};
  }
}
"""


async def fetch_via_browser(page, url: str) -> tuple[bytes, dict]:
    """Download a URL using the browser's own fetch — inherits TLS fingerprint + cookies."""
    result = await page.evaluate(_FETCH_JS, url)
    if result.get("error"):
        detail = (
            f"HTTP {result['status']} {result.get('statusText','')}"
            if "status" in result else f"exception: {result.get('message','?')}"
        )
        raise RuntimeError(f"Download failed for {url}: {detail}")
    headers = {
        "last-modified": result.get("lastModified"),
        "etag": result.get("etag"),
        "content-type": result.get("contentType"),
    }
    return base64.b64decode(result["base64"]), headers


def _find_prior_xlsx(filename: str, today: str) -> Path | None:
    """Return the path to the most recent prior archive copy of this file, or None."""
    if not ARCHIVE_DIR.exists():
        return None
    dated = sorted(
        d for d in ARCHIVE_DIR.iterdir()
        if d.is_dir() and DATE_RE.match(d.name) and d.name < today
    )
    for d in reversed(dated):
        candidate = d / filename
        if candidate.exists():
            return candidate
    return None


def emit_output(**kv: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a") as f:
        for k, v in kv.items():
            f.write(f"{k}={v}\n")


async def main() -> int:
    CURRENT_DIR.mkdir(parents=True, exist_ok=True)
    now_iso = datetime.now(timezone.utc).isoformat()

    prior_meta: dict = {}
    if METADATA_PATH.exists():
        prior_meta = json.loads(METADATA_PATH.read_text())
    prior_by_name = {f["filename"]: f for f in prior_meta.get("files", [])}

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
        await page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=120000)

        title = ""
        for attempt in range(90):
            title = await page.title()
            t = title.lower()
            if title and "moment" not in t and "challenge" not in t:
                link_count = await page.evaluate("document.querySelectorAll('a[href]').length")
                if link_count > 5:
                    break
                if attempt % 5 == 0:
                    print(f"[fetch] title cleared ({title!r}) but only {link_count} links — waiting", flush=True)
            elif attempt % 5 == 0:
                print(f"[fetch] waiting for CF (attempt {attempt+1}/90, title={title!r})", flush=True)
            await asyncio.sleep(2)
        else:
            html = await page.content()
            (CURRENT_DIR / "_blocked-snapshot.html").write_text(html)
            raise RuntimeError(f"Did not reach forecast page; final title={title!r}.")

        print(f"[fetch] page loaded, title={title!r}", flush=True)

        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass  # not critical; we have content

        hrefs: list[str] = await page.eval_on_selector_all(
            "a[href]", "els => els.map(e => e.href)"
        )

        doc_links: list[str] = []
        for href in hrefs:
            parsed = urllib.parse.urlparse(href)
            if parsed.netloc != ALLOWED_HOST:
                continue
            decoded = urllib.parse.unquote(parsed.path).lower()
            if not decoded.endswith(DOC_EXTENSIONS):
                continue
            if not any(k in decoded for k in FORECAST_KEYWORDS):
                continue
            doc_links.append(href)
        doc_links = sorted(set(doc_links))

        print(f"[fetch] found {len(doc_links)} forecast file(s)", flush=True)
        for u in doc_links:
            print(f"  - {u}", flush=True)

        if not doc_links:
            html = await page.content()
            (CURRENT_DIR / "_no-links-snapshot.html").write_text(html)
            raise RuntimeError("No forecast files linked on the page.")

        files_meta: list[dict] = []
        any_changed = False

        for url in doc_links:
            filename = urllib.parse.unquote(url.rsplit("/", 1)[-1])
            print(f"[fetch] downloading {filename}", flush=True)
            body, headers = await fetch_via_browser(page, url)
            sha = hashlib.sha256(body).hexdigest()

            old = prior_by_name.get(filename)
            file_changed = old is None or old.get("sha256") != sha

            meta = {
                "filename": filename,
                "source_url": url,
                "sha256": sha,
                "bytes": len(body),
                "source_last_modified": headers.get("last-modified"),
                "source_etag": headers.get("etag"),
            }
            if filename.lower().endswith(".xlsx"):
                meta["xlsx_dcterms_modified"] = parse_xlsx_modified(body)

            if file_changed:
                any_changed = True
                meta["last_changed_utc"] = now_iso
                today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                archive_day = ARCHIVE_DIR / today
                archive_day.mkdir(parents=True, exist_ok=True)

                prior_xlsx = _find_prior_xlsx(filename, today)
                (archive_day / filename).write_bytes(body)
                if prior_xlsx is not None and filename.lower().endswith(".xlsx"):
                    diff_result = xlsx_diff.diff(prior_xlsx, archive_day / filename)
                    md = xlsx_diff.render_markdown(
                        diff_result,
                        old_date=prior_xlsx.parent.name if prior_xlsx.parent.name != "current" else "previous",
                        new_date=today,
                    )
                    (archive_day / "changes.md").write_text(md + "\n")
                    meta["diff_summary"] = xlsx_diff.summarize_one_liner(diff_result)
                    print(f"  -> diff: {meta['diff_summary']}", flush=True)
                elif prior_xlsx is None:
                    (archive_day / "changes.md").write_text(
                        f"# Initial snapshot — {today}\n\nFirst recorded version; no prior to diff against.\n"
                    )
                    print("  -> initial archive snapshot (no prior to diff)", flush=True)

                (CURRENT_DIR / filename).write_bytes(body)
                print(
                    f"  -> CHANGED: {len(body):,} bytes  sha={sha[:12]}  "
                    f"source-modified={meta.get('source_last_modified')}  "
                    f"xlsx-modified={meta.get('xlsx_dcterms_modified')}",
                    flush=True,
                )
            else:
                meta["last_changed_utc"] = old.get("last_changed_utc")
                print(f"  -> unchanged (sha matches prior; last changed {meta['last_changed_utc']})", flush=True)

            files_meta.append(meta)

    if any_changed:
        metadata = {
            "source_page_url": PAGE_URL,
            "last_checked_utc": now_iso,
            "files": files_meta,
        }
        METADATA_PATH.write_text(json.dumps(metadata, indent=2) + "\n")

    primary = files_meta[0] if files_meta else {}
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    emit_output(
        changed="true" if any_changed else "false",
        file_count=str(len(files_meta)),
        filenames=", ".join(m["filename"] for m in files_meta),
        source_last_modified=primary.get("source_last_modified") or "",
        xlsx_modified=primary.get("xlsx_dcterms_modified") or "",
        diff_summary=primary.get("diff_summary") or "",
        archive_date=today if any_changed else "",
    )

    print(f"[fetch] done. any_changed={any_changed}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
