#!/usr/bin/env python3
"""Download the DOC procurement forecast(s) using Playwright.

Plain HTTP fetchers get 403'd by Cloudflare bot challenges on commerce.gov.
A real browser (headless Chromium) clears the challenge and reuses those
cookies/TLS fingerprint when fetching each linked document.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import async_playwright

PAGE_URL = "https://www.commerce.gov/oam/industry/procurement-forecasts"
ALLOWED_HOST = "www.commerce.gov"
DOC_EXTENSIONS = (".xlsx", ".xls", ".pdf", ".doc", ".docx", ".csv")
FORECAST_KEYWORDS = ("forecast",)

REPO_ROOT = Path(__file__).resolve().parent.parent
FORECASTS_DIR = REPO_ROOT / "forecasts"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


async def main() -> int:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_dir = FORECASTS_DIR / today
    out_dir.mkdir(parents=True, exist_ok=True)

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
            print(f"[fetch] waiting for CF challenge to clear (attempt {attempt+1}, title={title!r})", flush=True)
            await asyncio.sleep(2)
        else:
            html = await page.content()
            snap = out_dir / "page-snapshot-blocked.html"
            snap.write_text(html)
            raise RuntimeError(
                f"CF challenge did not clear; final title={title!r}. "
                f"Snapshot at {snap.relative_to(REPO_ROOT)}"
            )

        print(f"[fetch] page loaded, title={title!r}", flush=True)

        hrefs: list[str] = await page.eval_on_selector_all(
            "a[href]", "els => els.map(e => e.href)"
        )

        doc_links: list[str] = []
        for href in hrefs:
            parsed = urllib.parse.urlparse(href)
            if parsed.netloc != ALLOWED_HOST:
                continue
            decoded_path = urllib.parse.unquote(parsed.path).lower()
            if not decoded_path.endswith(DOC_EXTENSIONS):
                continue
            if not any(k in decoded_path for k in FORECAST_KEYWORDS):
                continue
            doc_links.append(href)
        doc_links = sorted(set(doc_links))

        print(f"[fetch] found {len(doc_links)} forecast file(s)", flush=True)
        for u in doc_links:
            print(f"  - {u}", flush=True)

        if not doc_links:
            html = await page.content()
            (out_dir / "page-snapshot.html").write_text(html)
            raise RuntimeError(
                "No forecast files linked on the page. "
                f"Snapshot saved to forecasts/{today}/page-snapshot.html"
            )

        files_meta: list[dict] = []
        for url in doc_links:
            filename = urllib.parse.unquote(url.rsplit("/", 1)[-1])
            print(f"[fetch] downloading {filename}", flush=True)
            response = await context.request.get(url)
            if not response.ok:
                raise RuntimeError(f"Download failed for {url}: HTTP {response.status}")
            body = await response.body()
            (out_dir / filename).write_bytes(body)
            sha = hashlib.sha256(body).hexdigest()
            files_meta.append({
                "filename": filename,
                "source_url": url,
                "sha256": sha,
                "bytes": len(body),
            })
            print(f"  -> {len(body):,} bytes, sha256={sha[:12]}...", flush=True)

        metadata = {
            "source_page_url": PAGE_URL,
            "fetch_timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "files": files_meta,
        }
        (out_dir / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")

        prior_dirs = sorted(
            d for d in FORECASTS_DIR.iterdir()
            if d.is_dir()
            and d.name != today
            and re.fullmatch(r"\d{4}-\d{2}-\d{2}", d.name)
        )
        change_summary = ""
        if prior_dirs:
            prior = prior_dirs[-1]
            prior_meta_path = prior / "metadata.json"
            prior_files = {}
            if prior_meta_path.exists():
                prior_meta = json.loads(prior_meta_path.read_text())
                prior_files = {f["filename"]: f for f in prior_meta.get("files", [])}
            lines = [f"Comparison vs forecasts/{prior.name}:"]
            for f in files_meta:
                old = prior_files.get(f["filename"])
                if old is None:
                    lines.append(f"  NEW     {f['filename']} ({f['bytes']:,} bytes)")
                elif old["sha256"] != f["sha256"]:
                    lines.append(
                        f"  CHANGED {f['filename']} "
                        f"({old['bytes']:,} -> {f['bytes']:,} bytes)"
                    )
                else:
                    lines.append(f"  same    {f['filename']} ({f['bytes']:,} bytes)")
            change_summary = "\n".join(lines)
            (out_dir / "changes.txt").write_text(change_summary + "\n")
            print(f"[fetch] {change_summary}", flush=True)

        await browser.close()

    if "GITHUB_OUTPUT" in os.environ:
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write(f"date={today}\n")
            f.write(f"file_count={len(files_meta)}\n")
            f.write(f"filenames={', '.join(m['filename'] for m in files_meta)}\n")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
