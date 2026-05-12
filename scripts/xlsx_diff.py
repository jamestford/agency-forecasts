#!/usr/bin/env python3
"""Row-level diff for the DOC procurement forecast XLSX.

Schema (Sheet1):
  Row 1: top-level title (e.g. 'BAS PRISM AAP Procurement Forecast')
  Row 2: subtitle ('Submission Details')
  Row 3: column headers (30 cols)
  Row 4+: data rows, keyed by `Forecast ID` (col A)
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import openpyxl

KEY_COLUMN = "Forecast ID"
HEADER_ROW = 3
DATA_START_ROW = 4

SAMPLE_LIMIT = 25
ADDED_PREVIEW_COLS = ("Title", "Office", "Estimated Value Range", "Estimated Solicitation Fiscal Quarter", "Estimated Solicitation Fiscal Year")


def _normalize(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, dt.datetime):
        return v.isoformat()
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v


def _md_escape(v: Any) -> str:
    if v is None:
        return ""
    s = str(v)
    return s.replace("|", "\\|").replace("\n", " ").strip()


def load_rows(path: Path) -> tuple[dict, list[str]]:
    """Return ({forecast_id: {col_name: value}}, headers)."""
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if len(rows) < DATA_START_ROW:
        return {}, []
    header_row = rows[HEADER_ROW - 1]
    headers = [str(h) if h is not None else f"_col{i}" for i, h in enumerate(header_row)]
    try:
        key_idx = headers.index(KEY_COLUMN)
    except ValueError:
        key_idx = 0
    out: dict[Any, dict[str, Any]] = {}
    for row in rows[DATA_START_ROW - 1:]:
        if not row:
            continue
        key = row[key_idx]
        if key is None:
            continue
        out[key] = {h: _normalize(row[i]) if i < len(row) else None for i, h in enumerate(headers)}
    return out, headers


def diff(old_path: Path, new_path: Path) -> dict:
    old, _ = load_rows(old_path)
    new, headers = load_rows(new_path)

    old_ids = set(old)
    new_ids = set(new)
    added = sorted(new_ids - old_ids, key=lambda x: str(x))
    removed = sorted(old_ids - new_ids, key=lambda x: str(x))
    common = old_ids & new_ids

    changed: list[dict] = []
    unchanged = 0
    for fid in common:
        cell_diffs = []
        for col in headers:
            ov = old[fid].get(col)
            nv = new[fid].get(col)
            if ov != nv:
                cell_diffs.append({"column": col, "old": ov, "new": nv})
        if cell_diffs:
            changed.append({
                "id": fid,
                "title": new[fid].get("Title"),
                "office": new[fid].get("Office"),
                "changes": cell_diffs,
            })
        else:
            unchanged += 1
    changed.sort(key=lambda c: str(c["id"]))

    def added_row(fid):
        r = new[fid]
        return {"id": fid, **{c: r.get(c) for c in ADDED_PREVIEW_COLS}}

    def removed_row(fid):
        return {"id": fid, "title": old[fid].get("Title"), "office": old[fid].get("Office")}

    return {
        "added": [added_row(fid) for fid in added],
        "removed": [removed_row(fid) for fid in removed],
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

    if d["added"]:
        lines.append("## Added forecasts")
        lines.append("")
        lines.append("| Forecast ID | Title | Office | Estimated Value | FY Q | FY |")
        lines.append("|---|---|---|---|---|---|")
        for a in d["added"][:SAMPLE_LIMIT]:
            lines.append(
                f"| {_md_escape(a['id'])} | {_md_escape(a.get('Title'))} | "
                f"{_md_escape(a.get('Office'))} | {_md_escape(a.get('Estimated Value Range'))} | "
                f"{_md_escape(a.get('Estimated Solicitation Fiscal Quarter'))} | "
                f"{_md_escape(a.get('Estimated Solicitation Fiscal Year'))} |"
            )
        if len(d["added"]) > SAMPLE_LIMIT:
            lines.append("")
            lines.append(f"_…and {len(d['added']) - SAMPLE_LIMIT} more added rows._")
        lines.append("")

    if d["removed"]:
        lines.append("## Removed forecasts")
        lines.append("")
        lines.append("| Forecast ID | Title | Office |")
        lines.append("|---|---|---|")
        for r in d["removed"][:SAMPLE_LIMIT]:
            lines.append(f"| {_md_escape(r['id'])} | {_md_escape(r.get('title'))} | {_md_escape(r.get('office'))} |")
        if len(d["removed"]) > SAMPLE_LIMIT:
            lines.append("")
            lines.append(f"_…and {len(d['removed']) - SAMPLE_LIMIT} more removed rows._")
        lines.append("")

    if d["changed"]:
        lines.append("## Modified forecasts")
        lines.append("")
        for c in d["changed"][:SAMPLE_LIMIT]:
            title = c.get("title") or "(no title)"
            office = c.get("office") or ""
            lines.append(f"### {c['id']} — {_md_escape(title)}  ")
            if office:
                lines.append(f"*{_md_escape(office)}*")
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
    """Short summary suitable for commit messages."""
    return (
        f"+{len(d['added'])} added, "
        f"-{len(d['removed'])} removed, "
        f"~{len(d['changed'])} modified, "
        f"={d['unchanged_count']} unchanged "
        f"({d['old_total']}→{d['new_total']} rows)"
    )
