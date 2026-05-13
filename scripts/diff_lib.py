#!/usr/bin/env python3
"""Shared row-level diff + Markdown rendering for already-parsed forecast rows.

Each agency fetcher is responsible for parsing its source file (XLSX or PDF)
into a `dict[key, dict[col, value]]` shape. This module diffs and renders.

This is the new shared helper used by DOE / DOS / NSF / future agencies.
DOC and ED still use their original xlsx_diff / pdf_diff helpers.
"""
from __future__ import annotations

from typing import Any


def _md_escape(v: Any) -> str:
    if v is None:
        return ""
    s = str(v)
    return s.replace("|", "\\|").replace("\n", " ").strip()


def diff(
    old: dict[Any, dict[str, Any]],
    new: dict[Any, dict[str, Any]],
    columns: list[str],
    *,
    title_column: str | None = None,
    preview_columns: list[str] | None = None,
) -> dict:
    """Compute add/remove/modify between two keyed row dicts.

    `columns` is the canonical list of all column names (used to detect cell
    changes within a row). `preview_columns` selects which columns appear in
    the "added" sample table. `title_column` is shown as the readable label
    for each row.
    """
    preview_columns = preview_columns or columns[:4]

    old_keys = set(old)
    new_keys = set(new)
    added_keys = sorted(new_keys - old_keys, key=str)
    removed_keys = sorted(old_keys - new_keys, key=str)
    common = old_keys & new_keys

    changed: list[dict] = []
    unchanged = 0
    for k in common:
        cell_diffs = []
        for col in columns:
            ov = old[k].get(col)
            nv = new[k].get(col)
            if ov != nv:
                cell_diffs.append({"column": col, "old": ov, "new": nv})
        if cell_diffs:
            changed.append({
                "key": k,
                "title": new[k].get(title_column) if title_column else None,
                "changes": cell_diffs,
            })
        else:
            unchanged += 1
    changed.sort(key=lambda c: str(c["key"]))

    added = [{"key": k, **{c: new[k].get(c) for c in preview_columns}} for k in added_keys]
    removed = [
        {"key": k, "title": old[k].get(title_column) if title_column else None}
        for k in removed_keys
    ]

    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "unchanged_count": unchanged,
        "old_total": len(old),
        "new_total": len(new),
        "preview_columns": preview_columns,
        "title_column": title_column,
    }


def render_markdown(
    d: dict,
    old_date: str,
    new_date: str,
    *,
    sample_limit: int = 25,
    pdf_note: bool = False,
) -> str:
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
    if pdf_note:
        lines.append(
            "> Note: PDF table extraction occasionally interleaves characters when "
            "a cell holds multi-line text. Row counts and keys are reliable; "
            "cell-level before→after may include extraction noise. The archived "
            "PDF is authoritative."
        )
        lines.append("")

    preview_cols = d.get("preview_columns") or []
    title_col = d.get("title_column")

    if d["added"]:
        lines.append("## Added")
        lines.append("")
        header_row = ["Key"] + preview_cols
        lines.append("| " + " | ".join(header_row) + " |")
        lines.append("|" + "|".join(["---"] * len(header_row)) + "|")
        for a in d["added"][:sample_limit]:
            cells = [_md_escape(a["key"])] + [_md_escape(a.get(c)) for c in preview_cols]
            lines.append("| " + " | ".join(cells) + " |")
        if len(d["added"]) > sample_limit:
            lines.append("")
            lines.append(f"_…and {len(d['added']) - sample_limit} more added rows._")
        lines.append("")

    if d["removed"]:
        lines.append("## Removed")
        lines.append("")
        if title_col:
            lines.append(f"| Key | {title_col} |")
            lines.append("|---|---|")
            for r in d["removed"][:sample_limit]:
                lines.append(f"| {_md_escape(r['key'])} | {_md_escape(r.get('title'))} |")
        else:
            lines.append("| Key |")
            lines.append("|---|")
            for r in d["removed"][:sample_limit]:
                lines.append(f"| {_md_escape(r['key'])} |")
        if len(d["removed"]) > sample_limit:
            lines.append("")
            lines.append(f"_…and {len(d['removed']) - sample_limit} more removed rows._")
        lines.append("")

    if d["changed"]:
        lines.append("## Modified")
        lines.append("")
        for c in d["changed"][:sample_limit]:
            title_str = _md_escape(c.get("title")) if c.get("title") else ""
            heading = f"### {c['key']}"
            if title_str:
                heading += f" — {title_str}"
            lines.append(heading)
            for cell in c["changes"]:
                ov = _md_escape(cell["old"]) or "(empty)"
                nv = _md_escape(cell["new"]) or "(empty)"
                lines.append(f"- **{_md_escape(cell['column'])}:** `{ov}` → `{nv}`")
            lines.append("")
        if len(d["changed"]) > sample_limit:
            lines.append(f"_…and {len(d['changed']) - sample_limit} more modified rows._")
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
