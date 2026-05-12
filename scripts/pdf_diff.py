#!/usr/bin/env python3
"""Row-level diff for ED procurement-forecast PDFs.

ED publishes a multi-page PDF where each page contains a 15-column table:
  col 0: Tracking No (the natural key, e.g. FY26AP3235)
  col 1: Funding Office
  col 2: Contracting Office
  col 3: Requirement Type
  col 4: Contract Name
  col 5: Primary NAICS Code
  col 6: Primary NAICS Code Description
  col 7: Contract Type
  col 8: Type of Competition
  col 9: Estimated Value of Contract
  col 10: Estimated Current Fiscal Year
  col 11: Incumbent Contractor Name
  col 12: Point of Contact Name
  col 13: Point of Contact Email
  col 14: Target Award Quarter

pdfplumber's table extraction is reliable for the Tracking No column but
some other cells get character-interleaved when the source PDF has
multi-line text in a single cell. So:
  - row identity (added/removed) is trusted
  - cell-level "before → after" is shown but may include extraction noise
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pdfplumber

CANONICAL_HEADERS = [
    "Tracking No",
    "Funding Office",
    "Contracting Office",
    "Requirement Type",
    "Contract Name",
    "Primary NAICS Code",
    "Primary NAICS Code Description",
    "Contract Type",
    "Type of Competition",
    "Estimated Value of Contract",
    "Estimated Current Fiscal Year",
    "Incumbent Contractor Name",
    "Point of Contact Name",
    "Point of Contact Email",
    "Target Award Quarter",
]
KEY_COL = "Tracking No"
SAMPLE_LIMIT = 25
ADDED_PREVIEW_COLS = (
    "Contract Name",
    "Funding Office",
    "Estimated Value of Contract",
    "Target Award Quarter",
)


def _is_data_row(row: list) -> bool:
    if not row:
        return False
    cell0 = (row[0] or "").strip() if isinstance(row[0], str) else ""
    # ED tracking numbers look like FY26AP1234 or similar — start with FY + 2-digit year
    return len(cell0) >= 6 and cell0[:2].upper() == "FY" and cell0[2:4].isdigit()


def extract_rows(path: Path) -> dict[str, dict[str, str]]:
    """Pull every data row across all pages, keyed on Tracking No."""
    out: dict[str, dict[str, str]] = {}
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
                    key = (row[0] or "").strip()
                    if not key:
                        continue
                    out[key] = {
                        h: (str(v).strip() if v is not None else "")
                        for h, v in zip(CANONICAL_HEADERS, row)
                    }
    return out


def _md_escape(v: Any) -> str:
    if v is None:
        return ""
    s = str(v)
    return s.replace("|", "\\|").replace("\n", " ").strip()


def diff(old_path: Path, new_path: Path) -> dict:
    old = extract_rows(old_path)
    new = extract_rows(new_path)

    old_keys = set(old)
    new_keys = set(new)
    added = sorted(new_keys - old_keys)
    removed = sorted(old_keys - new_keys)
    common = old_keys & new_keys

    changed: list[dict] = []
    unchanged = 0
    for k in common:
        cell_diffs = []
        for col in CANONICAL_HEADERS[1:]:
            ov = old[k].get(col, "")
            nv = new[k].get(col, "")
            if ov != nv:
                cell_diffs.append({"column": col, "old": ov, "new": nv})
        if cell_diffs:
            changed.append({"key": k, "name": new[k].get("Contract Name", ""), "changes": cell_diffs})
        else:
            unchanged += 1
    changed.sort(key=lambda c: c["key"])

    return {
        "added": [{"key": k, **{c: new[k].get(c, "") for c in ADDED_PREVIEW_COLS}} for k in added],
        "removed": [{"key": k, "name": old[k].get("Contract Name", "")} for k in removed],
        "changed": changed,
        "unchanged_count": unchanged,
        "old_total": len(old),
        "new_total": len(new),
    }


def render_markdown(d: dict, old_date: str, new_date: str) -> str:
    lines: list[str] = []
    lines.append(f"# Changes — {new_date}")
    lines.append("")
    lines.append(f"Compared against snapshot from `{old_date}`.")
    lines.append("")
    lines.append(f"- **Added:** {len(d['added'])} row(s)")
    lines.append(f"- **Removed:** {len(d['removed'])} row(s)")
    lines.append(f"- **Modified:** {len(d['changed'])} row(s)")
    lines.append(f"- **Unchanged:** {d['unchanged_count']} row(s)")
    lines.append(f"- Total rows: {d['old_total']} → {d['new_total']}")
    lines.append("")
    lines.append(
        "> Note: PDF table extraction occasionally interleaves characters when "
        "a cell holds multi-line text. Row counts and Tracking Nos are reliable; "
        "cell-level before→after may include extraction noise. The full archived "
        "PDF is authoritative."
    )
    lines.append("")

    if d["added"]:
        lines.append("## Added opportunities")
        lines.append("")
        lines.append("| Tracking No | Contract Name | Funding Office | Est. Value | Target Q |")
        lines.append("|---|---|---|---|---|")
        for a in d["added"][:SAMPLE_LIMIT]:
            lines.append(
                f"| {_md_escape(a['key'])} | {_md_escape(a.get('Contract Name'))} | "
                f"{_md_escape(a.get('Funding Office'))} | "
                f"{_md_escape(a.get('Estimated Value of Contract'))} | "
                f"{_md_escape(a.get('Target Award Quarter'))} |"
            )
        if len(d["added"]) > SAMPLE_LIMIT:
            lines.append("")
            lines.append(f"_…and {len(d['added']) - SAMPLE_LIMIT} more added rows._")
        lines.append("")

    if d["removed"]:
        lines.append("## Removed opportunities")
        lines.append("")
        lines.append("| Tracking No | Contract Name |")
        lines.append("|---|---|")
        for r in d["removed"][:SAMPLE_LIMIT]:
            lines.append(f"| {_md_escape(r['key'])} | {_md_escape(r.get('name'))} |")
        if len(d["removed"]) > SAMPLE_LIMIT:
            lines.append("")
            lines.append(f"_…and {len(d['removed']) - SAMPLE_LIMIT} more removed rows._")
        lines.append("")

    if d["changed"]:
        lines.append("## Modified opportunities")
        lines.append("")
        for c in d["changed"][:SAMPLE_LIMIT]:
            lines.append(f"### {c['key']} — {_md_escape(c.get('name')) or '(no name)'}")
            for cell in c["changes"]:
                ov = _md_escape(cell["old"]) or "(empty)"
                nv = _md_escape(cell["new"]) or "(empty)"
                lines.append(f"- **{_md_escape(cell['column'])}:** `{ov}` → `{nv}`")
            lines.append("")
        if len(d["changed"]) > SAMPLE_LIMIT:
            lines.append(f"_…and {len(d['changed']) - SAMPLE_LIMIT} more modified rows._")
            lines.append("")

    return "\n".join(lines)


def summarize_one_liner(d: dict) -> str:
    return (
        f"+{len(d['added'])} added, "
        f"-{len(d['removed'])} removed, "
        f"~{len(d['changed'])} modified, "
        f"={d['unchanged_count']} unchanged "
        f"({d['old_total']}→{d['new_total']} rows)"
    )
