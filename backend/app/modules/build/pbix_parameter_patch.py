"""EXPERIMENTAL: makes a compiled .pbix keep `SourceDataPath` (and friends)
as a REAL, editable Power Query Parameter — the same as the .pbip project
already has — instead of a baked-in literal string.

## Why this exists

`pbip_compiler` (the library that compiles our TMDL+PBIR project into a
.pbix) has no concept of a Power Query Parameter at all — its own object
model (`pbip_compiler.models`) is only `Table`/`Column`/`Measure`/
`Relationship`. Its underlying engine, `pbix_mcp`, DOES have the real
internal schema for one (a `[Expression]` row in the ABF-backed metadata
SQLite — confirmed by reading `pbix_mcp.formats.metadata_schema`), but no
code anywhere in either library ever populates it — `MAttributes` (the
field that would carry the `IsParameterQuery=true` metadata) is written as
`NULL` in every single INSERT in `pbix_mcp.builder`, with no exception.

A previous attempt at this (see `project.py`'s compile block, git history)
patched an `[Expression]` row directly into the FULLY COMPILED .pbix bytes
— i.e. after ABF packaging, XPress9 compression, and ZIP packaging were
already done. Power BI Desktop's full load crashed with
`TMCacheManager::CreateEmptyCollectionsForAllParents`, a VertiPaq
consistency check the raw insert didn't satisfy. That attempt was reverted.

## What's different about this attempt

This patches ONE STEP EARLIER in the pipeline: right after
`pbix_mcp.builder._modify_metadata_and_encode` finishes populating the
metadata SQLite with tables/columns/partitions/measures/relationships (but
BEFORE that SQLite is handed to `build_abf_clean`/`compress_datamodel`/
`build_pbix_clean` for ABF packaging and compression), this inserts the
`[Expression]` row into that same flat, not-yet-packaged SQLite file, using
the exact same ID-allocation convention (`_get_max_id_across_tables` +
1000-id gap) the rest of the builder already uses successfully for every
other object type. The modified SQLite then goes through the REAL,
unmodified ABF/VertiPaq/ZIP packaging pipeline like everything else,
instead of being spliced into an already-finished file.

## What is genuinely unverified here (read before trusting this blind)

Two fields have NO reference implementation anywhere in `pbix_mcp` to
learn from (grep confirms `MAttributes` is `NULL` in every existing
INSERT in the library, and nothing anywhere reads/interprets an
`Expression.Kind` value):

- `Expression.Kind` — set to `2` here, inferred only by weak analogy to
  `Partition.Type` using `2` for a native/M-query partition (per a comment
  in `pbix_mcp.builder`) — NOT independently confirmed against Microsoft's
  own AMO/TOM `ExpressionKind` enum (no internet access from this
  environment to check it).
- `MAttributes` JSON shape — modeled on the real TMDL syntax for a
  parameter (`meta [IsParameterQuery=true, List={}, DefaultValue="...",
  Type="Text", ...]`) translated into what looks like the equivalent
  JSON-attribute-bag shape used elsewhere in this schema family, but this
  specific record has never been independently verified against a real
  Desktop-authored file's raw SQLite content in this environment.

**This is explicitly an experiment, not a verified fix.** Build a .pbix
with `enable_real_parameters()` active, open it in Power BI Desktop, and
report back whether it opens cleanly and whether the parameter is real and
editable via Transform Data > Manage Parameters. If Desktop rejects it,
the exact error message is the next clue — this module's docstring is the
place to record what was learned and tried.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import uuid

import pbix_mcp.builder as _pbm
import pbip_compiler.datamodel.builder as _pbc
import warnings

_ORIGINAL_MODIFY_AND_ENCODE = _pbm._modify_metadata_and_encode
_ORIGINAL_BUILD = _pbm.PBIXBuilder.build

_PENDING_EXPRESSIONS: list[dict] = []

# Parameters queued for the NEXT PbixMcpDataModelBuilder.build() call —
# project.py calls queue_parameter() right before compile_pbix(), since
# pbip_compiler's own SemanticModel/Table pydantic models (models.py) have
# no field to carry a parameter through its normal TMDL-reading path at all.
_QUEUED_PARAMETERS: list[dict] = []


def queue_parameter(name: str, expression_m: str, *, default_value: str | None = None, data_type: str = "Text") -> None:
    """Call once per parameter, right before `compile_pbix(...)`. Consumed
    (and cleared) by the next `PbixMcpDataModelBuilder.build()` call."""
    _QUEUED_PARAMETERS.append({
        "name": name, "expression_m": expression_m,
        "default_value": default_value, "data_type": data_type,
    })


def _patched_datamodel_builder_build(self: "_pbc.PbixMcpDataModelBuilder", model) -> bytes:
    """Re-implementation of pbip_compiler.datamodel.builder.
    PbixMcpDataModelBuilder.build — identical to the original (see that
    class's own source, read directly from the installed package when this
    was written) except for the `builder.add_expression(...)` calls added
    for each queued parameter, BEFORE `builder.build()` runs, so they go
    through the SAME patched pipeline as `_patched_modify_and_encode`
    above."""
    from pbix_mcp.builder import PBIXBuilder

    builder = PBIXBuilder()
    for table in model.tables:
        self._add_table(builder, table)
        for measure in table.measures:
            builder.add_measure(table.name, measure.name, measure.expression)
    for relationship in model.relationships:
        builder.add_relationship(
            relationship.from_table, relationship.from_column,
            relationship.to_table, relationship.to_column,
        )

    global _QUEUED_PARAMETERS
    for param in _QUEUED_PARAMETERS:
        builder.add_expression(
            param["name"], param["expression_m"],
            default_value=param["default_value"], data_type=param["data_type"],
        )
    _QUEUED_PARAMETERS = []

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        pbix_bytes = builder.build()

    from pbip_compiler.datamodel.mpatch import patch_partition_m
    m_by_table = {t.name: t.m_expression for t in model.tables if t.m_expression}
    for table in model.tables:
        if table.m_expression:
            print(f"    [data] {table.name}: M preserved -> refreshable "
                  f"(Refresh in Power BI loads the data)")
        else:
            print(f"    [warn] {table.name}: no partition M -> empty table")

    return patch_partition_m(pbix_bytes, m_by_table)

_EXPRESSION_KIND_M = 2  # UNVERIFIED — see module docstring


def add_expression(
    self: "_pbm.PBIXBuilder",
    name: str,
    expression_m: str,
    *,
    is_parameter: bool = True,
    default_value: str | None = None,
    data_type: str = "Text",
) -> "_pbm.PBIXBuilder":
    """Queue a Power Query Parameter/shared expression for the NEXT
    `.build()` call on this builder instance. `expression_m` is the M
    literal the parameter currently holds (e.g. `"D:\\Data\\ar_csv"` — a
    valid M text literal, already quoted, same convention as a table's own
    M partition source)."""
    if not hasattr(self, "_pbix_param_patch_expressions"):
        self._pbix_param_patch_expressions = []
    self._pbix_param_patch_expressions.append({
        "name": name,
        "expression_m": expression_m,
        "is_parameter": is_parameter,
        "default_value": default_value if default_value is not None else expression_m,
        "data_type": data_type,
    })
    return self


def _patched_build(self: "_pbm.PBIXBuilder") -> bytes:
    global _PENDING_EXPRESSIONS
    _PENDING_EXPRESSIONS = getattr(self, "_pbix_param_patch_expressions", [])
    try:
        return _ORIGINAL_BUILD(self)
    finally:
        _PENDING_EXPRESSIONS = []


def _patched_modify_and_encode(sqlite_bytes, tables, measures, relationships, **kwargs):
    new_sqlite_bytes, vertipaq_files = _ORIGINAL_MODIFY_AND_ENCODE(
        sqlite_bytes, tables, measures, relationships, **kwargs
    )
    if not _PENDING_EXPRESSIONS:
        return new_sqlite_bytes, vertipaq_files
    new_sqlite_bytes = _insert_expressions(new_sqlite_bytes, _PENDING_EXPRESSIONS)
    return new_sqlite_bytes, vertipaq_files


def _insert_expressions(sqlite_bytes: bytes, expressions: list[dict]) -> bytes:
    fd, tmp_path = tempfile.mkstemp(suffix=".sqlitedb")
    try:
        os.write(fd, sqlite_bytes)
        os.close(fd)
        conn = sqlite3.connect(tmp_path)
        try:
            # Same convention _modify_metadata_and_encode itself uses: scan
            # every table (this one already has our new tables/columns/
            # partitions/measures/relationships in it) and allocate fresh
            # IDs well past anything that exists, so nothing we add here
            # can collide with an object Desktop creates on its own open.
            alloc_start = _pbm._get_max_id_across_tables(conn) + 1000
            c = conn.cursor()
            for i, expr in enumerate(expressions):
                expr_id = alloc_start + i
                m_attributes = json.dumps({
                    "IsParameterQuery": expr["is_parameter"],
                    "List": [],
                    "IsParameterQueryRequired": False,
                    "DefaultValue": expr["default_value"],
                    "Type": expr["data_type"],
                }) if expr["is_parameter"] else None
                c.execute(
                    """INSERT INTO [Expression] (
                        ID, ModelID, Name, Description, Kind, Expression,
                        ModifiedTime, QueryGroupID, ParameterValuesColumnID,
                        MAttributes, LineageTag, SourceLineageTag,
                        RemoteParameterName, ExpressionSourceID
                    ) VALUES (?, 1, ?, NULL, ?, ?, ?, 0, 0, ?, ?, NULL, NULL, 0)""",
                    (
                        expr_id, expr["name"], _EXPRESSION_KIND_M, expr["expression_m"],
                        _pbm._FIXED_TIMESTAMP, m_attributes, str(uuid.uuid4()),
                    ),
                )
            conn.commit()
        finally:
            conn.close()
        with open(tmp_path, "rb") as f:
            return f.read()
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


_patched_enabled = False


def enable_real_parameters() -> None:
    """Activates the monkey-patch for every `PBIXBuilder` used in this
    process from now on. Idempotent — safe to call more than once."""
    global _patched_enabled
    if _patched_enabled:
        return
    _pbm.PBIXBuilder.add_expression = add_expression
    _pbm.PBIXBuilder.build = _patched_build
    _pbm._modify_metadata_and_encode = _patched_modify_and_encode
    _pbc.PbixMcpDataModelBuilder.build = _patched_datamodel_builder_build
    _patched_enabled = True
