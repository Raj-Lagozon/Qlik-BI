"""Compile a .pbip project folder into a real .pbix using pbip-compiler."""

from __future__ import annotations

import re

from pbip_compiler import PbipCompiler
from pbip_compiler.models import Relationship
from pbip_compiler.semantic_model import tmdl as _tmdl_module


_QUALIFIED_COL_RE = re.compile(r"^\s*(?:'([^']+)'|([^.]+))\.(.+?)\s*$")


def _split_table_column(value: str) -> tuple[str, str] | None:
    """Parse a TMDL qualified column reference — 'Table.Column', or
    ''Table Name'.Column' when the table isn't a bare identifier (spaces,
    '%', etc.) — the single format real TMDL uses for a relationship's
    fromColumn/toColumn (confirmed against a real Desktop-authored
    relationships.tmdl). semantic_model.py's _tmdl_qualify is what writes
    this format; this is its inverse."""
    m = _QUALIFIED_COL_RE.match(value)
    if not m:
        return None
    table = m.group(1) if m.group(1) is not None else m.group(2)
    return table.strip(), m.group(3).strip()


def _patched_parse_relationships(self, text: str) -> list[Relationship]:
    """Replaces pbip_compiler.semantic_model.tmdl.TmdlParser._parse_relationships.

    The shipped implementation matches a relationship block with
    `relationship\\s+\\S+\\s*\\n((?:\\s+\\S.*\\n?)+)` — since `\\s` matches
    newlines too, that body group doesn't stop at the blank line separating
    two relationship declarations: it keeps consuming through the blank line
    and right into the next "relationship <guid>" header line, merging every
    relationship in the file into one giant match. The four `re.search`
    calls that follow only ever return the FIRST fromTable/fromColumn/
    toTable/toColumn in that merged blob, so every relationship after the
    first one in a file is silently dropped. Splitting on the header line
    itself keeps each block isolated regardless of blank-line spacing.

    Also parses the real TMDL relationship format — a single
    'fromColumn: Table.Column' / 'toColumn: Table.Column' pair, not
    separate fromTable/fromColumn/toTable/toColumn properties (the old
    separate-property form this function used to look for isn't valid TMDL
    at all — Desktop rejected it outright: "fromTable is not a supported
    property in the current context").
    """
    out: list[Relationship] = []
    blocks = re.split(r"(?m)^relationship\s+\S+\s*$", text)
    for body in blocks[1:]:
        from_m = re.search(r"(?m)^\s*fromColumn:\s*(.+)$", body)
        to_m = re.search(r"(?m)^\s*toColumn:\s*(.+)$", body)
        if not (from_m and to_m):
            continue
        from_split = _split_table_column(from_m.group(1))
        to_split = _split_table_column(to_m.group(1))
        if not (from_split and to_split):
            continue
        from_table, from_column = from_split
        to_table, to_column = to_split
        # pbip_compiler.models.Relationship has no is_active field at all —
        # its DataModel builder activates every relationship it's given,
        # unconditionally. An `isActive: false` line here is real and
        # correct in the TMDL (Power BI Desktop's own loader, opening the
        # .pbip project directly, DOES respect it) but silently ignored by
        # this compiled-.pbix path — baking in a second ACTIVE path between
        # tables already connected another way, which Power BI then rejects
        # outright the next time anything tries to save/refresh the model:
        # "There are ambiguous paths between 'X' and 'Y'". Since this
        # compiler can't represent "inactive", the only correct choice here
        # is to leave the relationship out of the compiled model entirely —
        # it stays fully present (and properly inactive) in the .pbip's own
        # relationships.tmdl for anyone opening that project directly.
        if re.search(r"(?m)^\s*isActive:\s*false\s*$", body, re.IGNORECASE):
            continue
        out.append(Relationship(
            from_table=from_table,
            from_column=from_column,
            to_table=to_table,
            to_column=to_column,
        ))
    return out


if hasattr(_tmdl_module.TmdlParser, "_parse_relationships"):
    _tmdl_module.TmdlParser._parse_relationships = _patched_parse_relationships
else:
    print(
        "[build] WARNING: pbip_compiler.semantic_model.tmdl.TmdlParser has no "
        "_parse_relationships to patch (package version changed?) — "
        "relationships beyond the first per table pair may be dropped."
    )


def compile_pbix(project_dir: str, output_pbix_path: str) -> str:
    compiler = PbipCompiler(project_dir)
    result = compiler.compile(output_pbix_path)
    print(f"[build] compiled -> {result}")
    print(
        "[build] NOTE: tables load with a placeholder row baked in — open the "
        ".pbix in Power BI Desktop and click Refresh once to pull real data "
        "through the generated Power Query (M) steps."
    )
    return str(result)
