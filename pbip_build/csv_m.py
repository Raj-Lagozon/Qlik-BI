"""Generate a Power Query (M) partition source that loads a table's real,
extracted data from CSV under a single, shared "SourceDataPath" location —
instead of trying to reconstruct the Qlik app's original (often unreachable)
source connection/file path, and instead of baking a literal absolute path
into every table individually.

Every table's M references the SAME bare identifier, `SourceDataPath`, which
is written once as a real Power Query Parameter (see
semantic_model.py's expressions.tmdl writer) — so repointing every table at
a different folder (a moved copy of the extracted CSVs, a network share, or
wherever SQL-exported CSVs with the SAME table/column names have been
dropped) is a single edit in Power BI Desktop's Manage Parameters dialog,
no re-extraction from the .qvf and no per-table edits required. Table names
and column data types are untouched by this — they still come from the
already-extracted data model; only the physical file location changes.

Numbers and dates are written to CSV as plain en-US-formatted values by
qlik_extract, so parsing here is pinned to "en-US" regardless of the
machine's regional settings, avoiding decimal-separator/date-format
ambiguity entirely.
"""

from __future__ import annotations

_TYPE_LITERAL = {"int64": "Int64.Type", "double": "type number"}


def generate_partition_m(
    table_name: str,
    filename: str,
    columns: list[dict],
    *,
    source_ref: str,
    sql: dict | None = None,
) -> str:
    """One table's partition M.

    Without `sql`: a plain CSV load resolved against `source_ref` (the
    `SourceDataPath` parameter).

    With `sql` ({"server_ref", "database_ref", "schema_ref"} — bare parameter
    identifiers): a dual-mode query. If the SqlServer parameter is non-empty
    the table is read straight from `[schema].[table_name]` on that SQL
    Server (column names already match the Qlik script names, so no
    renaming); otherwise it falls back to the CSV load. Power BI prompts for
    the SQL credentials itself on first refresh — they are never stored in
    the project file.
    """
    csv_block = generate_csv_partition_m(filename, columns, source_ref=source_ref)
    if not sql:
        return csv_block

    csv_indented = "\n        ".join(csv_block.splitlines())
    server = sql["server_ref"]
    database = sql["database_ref"]
    schema = sql["schema_ref"]
    item = table_name.replace('"', '""')
    return (
        "let\n"
        f"    LoadCsv = () =>\n        {csv_indented},\n"
        f'    LoadSql = () => Sql.Database({server}, {database})'
        f'{{[Schema={schema}, Item="{item}"]}}[Data],\n'
        f"    Source = if Text.Trim({server}) <> \"\" then LoadSql() else LoadCsv()\n"
        "in\n"
        "    Source"
    )


def generate_csv_partition_m(filename: str, columns: list[dict], *, source_ref: str) -> str:
    """`filename` is just the CSV's own name (e.g. "ARFact.csv"), resolved
    against `source_ref` at query time. `source_ref` is an M expression text
    that evaluates to the base folder path — normally the bare identifier
    `SourceDataPath` (the shared parameter), but pbix_compile substitutes a
    quoted literal for the compiled .pbix, since pbip-compiler can't embed a
    real Power Query Parameter object (see project.py's
    _tables_with_inlined_source)."""
    statements: list[str] = [
        f'Source = Csv.Document(File.Contents({source_ref} & "\\{filename}"), '
        f'[Delimiter=",", Columns={len(columns)}, Encoding=65001, QuoteStyle=QuoteStyle.Csv])',
        "Promoted = Table.PromoteHeaders(Source, [PromoteAllScalars=true])",
    ]
    text_cast = ", ".join(f'{{"{c["name"]}", type text}}' for c in columns)
    statements.append(f"AsText = Table.TransformColumnTypes(Promoted, {{{text_cast}}})")
    step = "AsText"

    date_cols = [c["name"] for c in columns if c.get("data_type") == "dateTime"]
    if date_cols:
        transforms = ", ".join(
            f'{{"{name}", each if _ = null or _ = "" then null else '
            f'#date(1899, 12, 30) + #duration(Number.FromText(_, "en-US"), 0, 0, 0), type date}}'
            for name in date_cols
        )
        statements.append(f"DatesFixed = Table.TransformColumns({step}, {{{transforms}}})")
        step = "DatesFixed"

    numeric_cols = [c for c in columns if c.get("data_type") in ("int64", "double") and c["name"] not in date_cols]
    if numeric_cols:
        transforms = []
        for c in numeric_cols:
            target_type = _TYPE_LITERAL[c["data_type"]]
            wrap = "Int64.From" if c["data_type"] == "int64" else ""
            expr = 'Number.FromText(_, "en-US")'
            if wrap:
                expr = f"{wrap}({expr})"
            transforms.append(
                f'{{"{c["name"]}", each if _ = null or _ = "" then null else {expr}, {target_type}}}'
            )
        statements.append(f"NumbersFixed = Table.TransformColumns({step}, {{{', '.join(transforms)}}})")
        step = "NumbersFixed"

    boolean_cols = [c["name"] for c in columns if c.get("data_type") == "boolean"]
    if boolean_cols:
        transforms = ", ".join(
            f'{{"{name}", each _ = "true" or _ = "1" or _ = "-1", type logical}}'
            for name in boolean_cols
        )
        statements.append(f"BoolsFixed = Table.TransformColumns({step}, {{{transforms}}})")
        step = "BoolsFixed"

    body = ",\n    ".join(statements)
    return f"let\n    {body}\nin\n    {step}"
