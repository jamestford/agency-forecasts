#!/usr/bin/env python3
"""Download the DOC procurement forecast using Playwright, only commit on change.

Plain HTTP fetchers get 403'd by Cloudflare bot challenges on commerce.gov.
A real browser (headless Chromium) clears the challenge and reuses those
cookies/TLS fingerprint when fetching each linked document.

Writes the latest file to current/ and emits GitHub Actions outputs the
workflow uses to decide whether to commit.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import urllib.parse
import zipfile
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree as ET

from playwright.async_api import async_playwright

PAGE_URL = "https://www.commerce.gov/oam/industry/procurement-forecasts"
ALLOWED_HOST = "www.commerce.gov"
DOC_EXTENSIONS = (".xlsx", ".xls", ".pdf", ".doc", ".docx", ".csv")
FORECAST_KEYWORDS = ("forecast",)

REPO_ROOT = Path(__file__).resolve().parent.parent
CURRENT_DIR = REPO_ROOT / "current"
METADATA_PATH = CURRENT_DIR / "metadata.json"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def parse_xlsx_modified(data: bytes) -> str | None:
    """Read dcterms:modified out of an XLSX's docProps/core.xml."""
    try:
        with zipfile.ZipFile(BytesIO(data)) as zf:
            with zf.open("docProps/core.xml") as f:
                root = ET.parse(f).getroot()
        el = root.find("{http://purl.org/dc/terms/}modified")
        return el.text if el is not None else None
    except Exception as e:
        print(f"[fetch] (warn) could not parse XLSX modified date: {e}", flush=True)
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

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        context = await browser.new_context(
            user_agent=UA,
            viewport={"width": 1280, "height": 800},
            locale="en-US",
        )
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = await context.new_page()

        print(f"[fetch] GET {PAGE_URL}", flush=True)
        await page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=60000)

        title = await page.title()
        for attempt in range(45):
            title = await page.title()
            if "moment" not in title.lower() and "challenge" not in title.lower():
                break
            print(f"[fetch] waiting for CF (attempt {attempt+1}, title={title!r})", flush=True)
            await asyncio.sleep(2)
        else:
            html = await page.content()
            (CURRENT_DIR / "_blocked-snapshot.html").write_text(html)
            raise RuntimeError(f"CF challenge did not clear; final title={title!r}.")

        print(f"[fetch] page loaded, title={title!r}", flush=True)

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
            response = await context.request.get(url)
            if not response.ok:
                raise RuntimeError(f"Download failed for {url}: HTTP {response.status}")
            body = await response.body()
            sha = hashlib.sha256(body).hexdigest()
            headers = response.headers

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
                (CURRENT_DIR / filename).write_bytes(body)
                meta["last_changed_utc"] = now_iso
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

        await browser.close()

    if any_changed:
        metadata = {
            "source_page_url": PAGE_URL,
            "last_checked_utc": now_iso,
            "files": files_meta,
        }
        METADATA_PATH.write_text(json.dumps(metadata, indent=2) + "\n")

    primary = files_meta[0] if files_meta else {}
    emit_output(
        changed="true" if any_changed else "false",
        file_count=str(len(files_meta)),
        filenames=", ".join(m["filename"] for m in files_meta),
        source_last_modified=primary.get("source_last_modified") or "",
        xlsx_modified=primary.get("xlsx_dcterms_modified") or "",
    )

    print(f"[fetch] done. any_changed={any_changed}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
