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

import re

_TYPE_LITERAL = {"int64": "Int64.Type", "double": "type number"}

_EXCEL_EXTENSIONS = (".xlsx", ".xlsm", ".xls")


def _source_load_statement(filename: str, source_ref: str, sheet_name: str | None) -> str:
    """The M `Source = ...` step that opens `filename` (resolved against
    `source_ref`, the shared SourceDataPath parameter) — CSV via
    Csv.Document (unchanged, the original/only behavior), or, when the
    file's own extension says it's a workbook (.xlsx/.xlsm/.xls), via
    Excel.Workbook selecting the ONE sheet the Qlik script actually reads
    (`sheet_name`, from the script's own `(ooxml, embedded labels, table is
    X)` qualifier — see project.py's `_detect_source_sheets`). A single
    .xlsx commonly backs EVERY table in the Qlik script (one sheet per
    table), unlike a CSV where each table has its own file — Csv.Document
    on an .xlsx would just fail outright (wrong file format), and even if
    it didn't, there would be no way to pick which of the several tables'
    worth of data inside it belongs to THIS Qlik table without the sheet
    qualifier.

    `Text.TrimEnd(source_ref, {"\\", "/"})` strips a trailing slash from
    SourceDataPath before joining the filename — same reasoning as the CSV
    path below: whether the user's parameter value ends with one or not,
    the joined path always has exactly one separator."""
    is_excel = filename.casefold().endswith(_EXCEL_EXTENSIONS)
    if not is_excel:
        return (
            f'Source = Csv.Document(File.Contents(Text.TrimEnd({source_ref}, {{"\\", "/"}}) & "\\{filename}"), '
            f'[Delimiter=",", Encoding=65001, QuoteStyle=QuoteStyle.Csv])'
        )
    # Fall back to the file's own base name (minus extension) as a best
    # guess at the sheet name when the script's qualifier wasn't detected —
    # still better than crashing outright, though the caller should always
    # have a real `sheet_name` for a properly-parsed `(ooxml, ...)` source.
    sheet = sheet_name or re.sub(r"\.[^.]+$", "", filename)
    sheet_escaped = sheet.replace('"', '""')
    return (
        f'Source = Excel.Workbook(File.Contents(Text.TrimEnd({source_ref}, {{"\\", "/"}}) & "\\{filename}"), '
        f'null, true){{[Item="{sheet_escaped}",Kind="Sheet"]}}[Data]'
    )


def _qlik_date_format_to_m_expr(value_expr: str, fmt: str) -> str:
    """Compile ONE Qlik date#() format string (e.g. 'MM/DD/YYYY',
    'YYYYMMDD', 'DD-MMM-YYYY') into an M expression that parses
    `value_expr` (already a plain text value) into a `date`.

    Qlik's LOAD script explicitly names each source file's own date shape
    per Date#()/date() call — the Qlik author already knew "this specific
    file's dates look like YYYYMMDD" — so parsing must follow the SAME
    per-file format, not a single generic guess (which is what silently
    nulled every date this was built to fix: 'YYYYMMDD' and ambiguous
    'DD-MM-YYYY' text don't survive a locale-generic Date.FromText call).

    General approach: split the raw text on whichever separator the format
    itself uses (the same delimiter appears at the same position in every
    real value using that format), then read each part according to the
    format token in that position (year/month/month-name/day) — this
    handles a variable-width part (e.g. a single-digit day in 'D/M/YYYY',
    "5/3/2024") correctly, since Number.FromText doesn't care how many
    digits a part has. A format with NO delimiter at all ('YYYYMMDD') is
    the one case that needs fixed-width slicing instead, handled as its
    own branch."""
    fmt_clean = fmt.strip()
    delimiter = next((d for d in ("/", "-", ".") if d in fmt_clean), None)

    if delimiter is None:
        # Fixed-width, no separator — only YYYYMMDD (or similar all-numeric
        # concatenated forms) realistically appears this way in practice.
        # Slice by each token's own known width (Y=4, M=2, D=2) in the
        # order the format string itself lists them.
        widths = {"Y": 4, "M": 2, "D": 2}
        pos = 0
        parts_m = []
        i = 0
        while i < len(fmt_clean):
            token_char = fmt_clean[i].upper()
            run = 1
            while i + run < len(fmt_clean) and fmt_clean[i + run].upper() == token_char:
                run += 1
            width = widths.get(token_char, run)
            parts_m.append((token_char, f"Text.Range(_v, {pos}, {width})"))
            pos += width
            i += run
        year = next((e for t, e in parts_m if t == "Y"), None)
        month = next((e for t, e in parts_m if t == "M"), None)
        day = next((e for t, e in parts_m if t == "D"), None)
        return (
            f"let _v = {value_expr} in #date(Number.FromText({year}), Number.FromText({month}), "
            f"Number.FromText({day}))"
        )

    tokens = fmt_clean.split(delimiter)
    return_parts = {"year": None, "month": None, "day": None}
    for idx, tok in enumerate(tokens):
        t = tok.strip().upper()
        part_ref = f"_p{{{idx}}}"
        if t.startswith("Y"):
            return_parts["year"] = f"Number.FromText({part_ref})"
        elif t.startswith("MMM"):
            return_parts["month"] = (
                f"(List.PositionOf({{\"JAN\",\"FEB\",\"MAR\",\"APR\",\"MAY\",\"JUN\",\"JUL\",\"AUG\","
                f"\"SEP\",\"OCT\",\"NOV\",\"DEC\"}}, Text.Upper(Text.Start({part_ref}, 3))) + 1)"
            )
        elif t.startswith("M"):
            return_parts["month"] = f"Number.FromText({part_ref})"
        elif t.startswith("D"):
            return_parts["day"] = f"Number.FromText({part_ref})"

    return (
        f'let _p = Text.Split({value_expr}, "{delimiter}") in '
        f'#date({return_parts["year"]}, {return_parts["month"]}, {return_parts["day"]})'
    )


def generate_partition_m(
    table_name: str,
    filename: str,
    columns: list[dict],
    *,
    source_ref: str,
    sql: dict | None = None,
    computed: dict[str, dict] | None = None,
    renames: dict[str, str] | None = None,
    expr_columns: dict[str, dict] | None = None,
    date_formats: dict[str, dict] | None = None,
    sheet_name: str | None = None,
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
    the project file. `computed`/`renames` (CSV branch only — a SQL source
    is assumed to already carry the model's own column names) are passed
    straight through to generate_csv_partition_m.
    """
    csv_block = generate_csv_partition_m(
        filename, columns, source_ref=source_ref, computed=computed, renames=renames,
        expr_columns=expr_columns, date_formats=date_formats, sheet_name=sheet_name,
    )
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


# M equivalents of the Qlik LOAD-script functions recognized as "computed
# from another field in the same table" (see project.py's
# _detect_month_derived_columns / _detect_chr_concat_columns, which parse
# the actual script text for these — not a name-based guess). Qlik computes
# these AT LOAD TIME regardless of what the source file contains, so they
# never come from the CSV either way; `source` is the M step name they read
# the already-typed source column from.
def _computed_column_expr(spec: dict) -> str:
    func = spec["func"]
    if func == "ConcatKey":
        # Qlik's dual(A & 'sep' & B, autonumber(A & 'sep' & B)) composite-key
        # idiom (see project.py's _detect_dual_autonumber_keys) — reproduced
        # as the plain text concatenation alone; DAX/Power Query have no
        # dual-value equivalent, and a join key just needs both sides to
        # agree on ONE value, not specifically a number.
        a, b, sep = spec["field_a"], spec["field_b"], spec["sep"].replace('"', '""')
        return f'each if [{a}] = null or [{b}] = null then null else [{a}] & "{sep}" & [{b}]'
    if func == "ApplyMapSubfield":
        # Qlik's SubField(ApplyMap('map', Field, 'default'), 'sep', N) —
        # look Field up in the mapping table (MapDict_<map>, built by
        # _mapping_dict_statement and injected before this step), fall back
        # to the default when unmatched (ApplyMap's own semantics), then
        # take the Nth ('-'-separated etc.) piece of whatever came back.
        # Qlik's SubField index is 1-based; spec["part_index"] is already
        # converted to the 0-based M list index.
        source_col = spec["source"]
        map_ident = _ident(spec["map_name"])
        default = spec["default"].replace('"', '""')
        sep = spec["sep"].replace('"', '""')
        idx = spec["part_index"]
        return (
            f'each let mapped = Record.FieldOrDefault(MapDict_{map_ident}, '
            f'Text.From([{source_col}]), "{default}"), '
            f'parts = Text.Split(mapped, "{sep}") in '
            f'if List.Count(parts) > {idx} then parts{{{idx}}} else null'
        )
    source_col = spec["source"]
    if func == "Month":
        return f'each if [{source_col}] = null then null else Date.Month([{source_col}])'
    if func == "MonthName":
        # Qlik's MonthName() is a dual value: numerically a date, displayed
        # as e.g. "Aug 2026" — approximated here as that same "MMM yyyy"
        # text (the exact wording can differ from Qlik's own month-format
        # regional setting; adjust the format string if it needs to match
        # exactly).
        return f'each if [{source_col}] = null then null else Date.ToText([{source_col}], "MMM yyyy")'
    # ChrConcat: Qlik's Chr(N) is the literal Unicode/ASCII character N
    # (Chr(10) = line feed) — Power Query's exact equivalent is
    # Character.FromNumber(N). `&` propagates null if either side is null,
    # so guard the source field explicitly rather than silently losing the
    # whole value.
    char_expr = f"Character.FromNumber({spec['code']})"
    concat = f"{char_expr} & [{source_col}]" if spec["prefix"] else f"[{source_col}] & {char_expr}"
    return f'each if [{source_col}] = null then null else {concat}'


def generate_csv_partition_m(
    filename: str, columns: list[dict], *, source_ref: str,
    computed: dict[str, dict] | None = None, renames: dict[str, str] | None = None,
    expr_columns: dict[str, dict] | None = None, date_formats: dict[str, dict] | None = None,
    sheet_name: str | None = None,
) -> str:
    """`filename` is just the CSV's own name (e.g. "ARFact.csv"), resolved
    against `source_ref` at query time. `source_ref` is an M expression text
    that evaluates to the base folder path — normally the bare identifier
    `SourceDataPath` (the shared parameter), but pbix_compile substitutes a
    quoted literal for the compiled .pbix, since pbip-compiler can't embed a
    real Power Query Parameter object (see project.py's
    _tables_with_inlined_source).

    `computed` ({column_name: {"func": "Month"|"MonthName", "source": <other
    column name>}}) marks columns the Qlik script computes from another
    field in this same LOAD rather than reading from the source file — they
    are excluded from the CSV select/type-fix pipeline below (the source
    file was never expected to have them) and appended as their own M step
    instead, reproducing the same computation.

    `renames` ({model_name: source_file_name}) marks columns the Qlik script
    renames on the way in (`RiskBand AS PredictedRiskBand`) — the source file
    only ever has the OLD name, so it's selected by that name and renamed to
    the model's name right after, instead of being selected by the model's
    name (which the file was never going to have) and loading as null.
    """
    computed = computed or {}
    renames = renames or {}
    statements, step = _csv_load_and_typefix_statements(
        filename, columns, source_ref, renames, computed, expr_columns, date_formats, sheet_name
    )
    statements, step = _apply_computed_columns(statements, step, columns, computed, source_ref=source_ref)

    body = ",\n    ".join(statements)
    return f"let\n    {body}\nin\n    {step}"


def _m_string_literal(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


# Qlik function -> M equivalent for a GROUP BY key / aggregation, mirrored
# from project.py's _QLIK_TO_M_DATE_FUNC / _QLIK_TO_M_AGG_FUNC (kept here
# too since this module has no import on project.py — the two dicts must
# stay in sync; project.py's detector is the single source of truth for
# WHICH functions are considered safe to reproduce, this module just knows
# how to spell each one in M).
_QLIK_TO_M_DATE_FUNC = {
    "monthstart": "Date.StartOfMonth", "monthend": "Date.EndOfMonth",
    "year": "Date.Year", "month": "Date.Month",
    "weekstart": "Date.StartOfWeek", "weekend": "Date.EndOfWeek",
}
_QLIK_TO_M_AGG_FUNC = {"sum": "List.Sum", "count": "List.Count", "avg": "List.Average", "min": "List.Min", "max": "List.Max"}


def wrap_with_left_join_aggregation(base_m: str, spec: dict) -> str:
    """Reproduces Qlik's `left join(Target) LOAD <keys>, <agg> RESIDENT
    Source ... GROUP BY <keys>;` — `base_m` is Target's own already-built
    partition M (its plain CSV/SQL/etc. load, MINUS the aggregation
    column(s), which project.py excludes from that build so there's no
    name clash with the ones added here). `spec["source_table"]` is
    referenced by its bare table name — a normal Power Query cross-query
    reference, which Power BI resolves into the correct refresh order on
    its own, the same way Qlik's own script evaluates RESIDENT against an
    already-loaded table.
    """
    source_table = spec["source_table"]
    group_by = spec["group_by"]
    aggregations = spec["aggregations"]

    statements: list[str] = [f"Base =\n        (\n        {_reindent(base_m, 8)}\n        )"]
    step = source_table
    for gk in group_by:
        if gk["func"] == "identity":
            if gk["alias"] != gk["field"]:
                statements.append(f'Keyed_{_ident(gk["alias"])} = Table.RenameColumns({step}, {{{{"{gk["field"]}", "{gk["alias"]}"}}}})')
                step = f'Keyed_{_ident(gk["alias"])}'
            continue
        m_func = _QLIK_TO_M_DATE_FUNC[gk["func"]]
        new_step = f'Keyed_{_ident(gk["alias"])}'
        statements.append(f'{new_step} = Table.AddColumn({step}, "{gk["alias"]}", each {m_func}([{gk["field"]}]))')
        step = new_step

    key_names = [gk["alias"] for gk in group_by]
    key_list = ", ".join(f'"{k}"' for k in key_names)
    agg_specs = ", ".join(
        f'{{"{a["alias"]}", each {_QLIK_TO_M_AGG_FUNC[a["func"]]}([{a["field"]}]), type nullable number}}'
        for a in aggregations
    )
    statements.append(f"Grouped = Table.Group({step}, {{{key_list}}}, {{{agg_specs}}})")

    # The two sides of this join come from DIFFERENT source files (Base is
    # its own CSV; Grouped is aggregated from another table entirely) — an
    # "identity" key (a plain text field, e.g. ZoneID) commonly differs in
    # case or stray leading/trailing whitespace between two independently-
    # authored files even though it's meant to be the same value ("Z01" vs
    # "z01 "). Table.NestedJoin matches EXACT values only, so this makes
    # every row silently fail to match — the joined aggregate columns come
    # back entirely null (LeftOuter keeps every Base row, just with nothing
    # attached), and every measure built on them evaluates to BLANK with no
    # error anywhere. A date-derived key (built by the SAME Qlik
    # monthstart()-equivalent function on both sides, see the Keyed_* steps
    # above) doesn't have this problem — it's excluded from normalization.
    # Match on a trimmed/uppercased COPY of each identity key instead of the
    # raw value, without altering the real column's own value (other
    # relationships in the model may depend on its exact original casing).
    base_step = "Base"
    grouped_step = "Grouped"
    base_join_keys, grouped_join_keys = [], []
    for gk in group_by:
        alias = gk["alias"]
        if gk["func"] != "identity":
            base_join_keys.append(alias)
            grouped_join_keys.append(alias)
            continue
        norm_key = f"_JoinKey_{_ident(alias)}"
        statements.append(
            f'BaseKeyed_{_ident(alias)} = Table.AddColumn({base_step}, "{norm_key}", '
            f'each Text.Trim(Text.Upper(Text.From([{alias}]))))'
        )
        base_step = f"BaseKeyed_{_ident(alias)}"
        statements.append(
            f'GroupedKeyed_{_ident(alias)} = Table.AddColumn({grouped_step}, "{norm_key}", '
            f'each Text.Trim(Text.Upper(Text.From([{alias}]))))'
        )
        grouped_step = f"GroupedKeyed_{_ident(alias)}"
        base_join_keys.append(norm_key)
        grouped_join_keys.append(norm_key)

    base_key_list = ", ".join(f'"{k}"' for k in base_join_keys)
    grouped_key_list = ", ".join(f'"{k}"' for k in grouped_join_keys)
    agg_names = [a["alias"] for a in aggregations]
    agg_list = ", ".join(f'"{a}"' for a in agg_names)
    statements.append(
        f'Merged = Table.NestedJoin({base_step}, {{{base_key_list}}}, {grouped_step}, {{{grouped_key_list}}}, "JoinedAgg", JoinKind.LeftOuter)'
    )
    statements.append(f'Expanded = Table.ExpandTableColumn(Merged, "JoinedAgg", {{{agg_list}}}, {{{agg_list}}})')
    final_step = "Expanded"
    norm_key_names = [k for k in base_join_keys if k.startswith("_JoinKey_")]
    if norm_key_names:
        drop_list = ", ".join(f'"{k}"' for k in norm_key_names)
        statements.append(f"Cleaned = Table.RemoveColumns(Expanded, {{{drop_list}}})")
        final_step = "Cleaned"

    body = ",\n    ".join(statements)
    return f"let\n    {body}\nin\n    {final_step}"


def _reindent(text: str, spaces: int) -> str:
    pad = " " * spaces
    return f"\n{pad}".join(text.splitlines())


def generate_inline_partition_m(columns: list[dict], rows: list[dict[str, str]]) -> str:
    """A table Qlik builds from `LOAD * INLINE [...]` — literal rows typed
    directly into the script, not read from any file. No SourceDataPath/SQL
    dependency at all: the data is embedded in the M itself via `#table`,
    the same way it's embedded in the Qlik script. Row values arrive as
    plain text (INLINE blocks carry no type info of their own beyond what
    the value looks like); the same date/number/boolean cast cascade CSV
    loading uses is applied afterward so the result matches the model's own
    column types.
    """
    col_names = ", ".join(f'"{c["name"]}"' for c in columns)
    row_literals = []
    for row in rows:
        cells = ", ".join(_m_string_literal(row.get(c["name"], "") or "") for c in columns)
        row_literals.append(f"{{{cells}}}")
    rows_block = ",\n        ".join(row_literals)
    statements = [f"Source = #table({{{col_names}}}, {{\n        {rows_block}\n    }})"]
    step = "Source"

    date_cols = [c["name"] for c in columns if c.get("data_type") == "dateTime"]
    if date_cols:
        transforms = ", ".join(
            f'{{"{name}", each if _ = null or _ = "" then null else '
            f'try Date.From(Date.FromText(_, "en-US")) otherwise null, type date}}'
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
                f'{{"{c["name"]}", each if _ = null or _ = "" then null else try {expr} otherwise null, {target_type}}}'
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


def generate_combined_partition_m(
    table_name: str,
    filenames: list[str],
    columns: list[dict],
    *,
    source_ref: str,
    sql: dict | None = None,
    computed: dict[str, dict] | None = None,
    renames: dict[str, str] | None = None,
    expr_columns_per_file: list[dict[str, dict]] | None = None,
    date_formats_per_file: list[dict[str, dict]] | None = None,
    sheet_names: list[str | None] | None = None,
) -> str:
    """A table the Qlik script builds by concatenating MULTIPLE source
    files — several unlabeled `LOAD ... FROM [...]` blocks in a row
    (Qlik auto-concatenates a LOAD with no table name of its own onto
    whichever table is currently in scope), or an explicit
    `Concatenate(Table) LOAD ... FROM [...]` (see project.py's
    _detect_multi_source_tables). Reproduces that as a `Table.Combine()`
    over each file loaded/typed the exact same way a single-source table
    is, IN THE SAME ORDER the script itself loads them — same signature
    and same `computed`/`renames`/`sql` handling as generate_partition_m,
    just fed more than one file. `expr_columns_per_file`/
    `date_formats_per_file` are lists positionally aligned with
    `filenames` — each source file can compute/parse things differently
    (e.g. 'Sale' vs 'Return' as RecordType, or each file's own explicit
    date#() format string)."""
    computed = computed or {}
    renames = renames or {}
    per_file_blocks = []
    for i, filename in enumerate(filenames):
        file_expr_columns = expr_columns_per_file[i] if expr_columns_per_file and i < len(expr_columns_per_file) else None
        file_date_formats = date_formats_per_file[i] if date_formats_per_file and i < len(date_formats_per_file) else None
        file_sheet_name = sheet_names[i] if sheet_names and i < len(sheet_names) else None
        file_statements, file_step = _csv_load_and_typefix_statements(
            filename, columns, source_ref, renames, computed, file_expr_columns, file_date_formats, file_sheet_name
        )
        inner_body = ",\n        ".join(file_statements)
        per_file_blocks.append(f"Source_{i} =\n        let\n        {inner_body}\n        in\n            {file_step}")
    combine_list = ", ".join(f"Source_{i}" for i in range(len(filenames)))
    statements = list(per_file_blocks)
    statements.append(f"Combined = Table.Combine({{{combine_list}}})")
    step = "Combined"
    statements, step = _apply_computed_columns(statements, step, columns, computed, source_ref=source_ref)
    csv_block = "let\n    " + ",\n    ".join(statements) + f"\nin\n    {step}"

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


def _mapping_dict_statement(spec: dict, source_ref: str) -> str:
    """One `MapDict_<map>` step: loads the Qlik `mapping LOAD key, value
    FROM [...]` table's own CSV (same SourceDataPath convention as every
    other table) and turns it into an M Record (key -> value), the
    equivalent of Qlik's in-memory mapping table that ApplyMap() reads."""
    map_ident = _ident(spec["map_name"])
    filename = spec["map_filename"]
    key_field = spec["map_key_field"]
    value_field = spec["map_value_field"]
    inner = (
        "let\n"
        f'            MSource = Csv.Document(File.Contents(Text.TrimEnd({source_ref}, {{"\\", "/"}}) & "\\{filename}"), '
        '[Delimiter=",", Encoding=65001, QuoteStyle=QuoteStyle.Csv]),\n'
        "            MPromoted = Table.PromoteHeaders(MSource, [PromoteAllScalars=true]),\n"
        f'            MTyped = Table.TransformColumnTypes(MPromoted, {{{{"{key_field}", type text}}, {{"{value_field}", type text}}}})\n'
        "        in\n"
        f"            Record.FromList(MTyped[{value_field}], MTyped[{key_field}])"
    )
    return f"MapDict_{map_ident} =\n        {inner}"


def _apply_computed_columns(
    statements: list[str], step: str, columns: list[dict], computed: dict[str, dict],
    *, source_ref: str | None = None,
) -> tuple[list[str], str]:
    """Appends each `computed` column's Table.AddColumn step, then — if any
    of them needed extra helper source columns not in the table's own
    declared column list (see _extra_helper_columns) — a final
    Table.RemoveColumns dropping those, so the query's actual output shape
    matches the model's declared columns exactly (a Qlik table that
    consumes PlantID/ProductID only to build ProductKey doesn't expose
    PlantID/ProductID as fields of its own either).

    An "ApplyMapSubfield" spec additionally needs its own MapDict_<map>
    step injected BEFORE the Computed_ step that reads it — added once per
    distinct map even if several columns (or, via the multi-file combine
    path, several per-file blocks) reference the same one."""
    injected_maps: set[str] = set()
    for name, spec in computed.items():
        if spec.get("func") == "ApplyMapSubfield":
            map_ident = spec["map_name"]
            if map_ident not in injected_maps:
                injected_maps.add(map_ident)
                statements.append(_mapping_dict_statement(spec, source_ref or "SourceDataPath"))
        expr = _computed_column_expr(spec)
        statements.append(f'Computed_{_ident(name)} = Table.AddColumn({step}, "{name}", {expr})')
        step = f"Computed_{_ident(name)}"
    extra_columns = _extra_helper_columns(columns, computed)
    if extra_columns:
        drop_list = ", ".join(f'"{n}"' for n in extra_columns)
        statements.append(f"Cleaned = Table.RemoveColumns({step}, {{{drop_list}}})")
        step = "Cleaned"
    return statements, step


def _arith_expr_to_m(expr: str, fields: list[str]) -> str:
    """Qlik arithmetic syntax — identifiers, `+ - * /`, parens, numeric
    literals, e.g. `(Qty*UnitPrice)-Discount` — is already valid M syntax
    for those same operators. The only translation needed is wrapping each
    REAL field reference in M's `[Field]` column-reference syntax; every
    other character (operators, parens, digits) passes through unchanged.
    Longest names are substituted first so a shorter field name that's a
    substring of a longer one (e.g. "Qty" inside "ReturnQty") is never
    partially matched and mangled."""
    out = expr
    for name in sorted(fields, key=len, reverse=True):
        out = re.sub(rf"\b{re.escape(name)}\b", f"[{name}]", out)
    return out


def _extra_helper_columns(columns: list[dict], computed: dict[str, dict]) -> list[str]:
    """Fields a `computed` spec reads (e.g. ConcatKey's field_a/field_b)
    that aren't themselves one of the table's own declared columns — Qlik's
    LOAD can consume a field purely to build a computed one (`dual(PlantID
    & '|' & ProductID, ...) AS ProductKey`) without keeping PlantID/
    ProductID as output fields of the table at all. Those still need to be
    selected from the source file as scratch inputs, just not kept in the
    final model-facing column list."""
    declared = {c["name"] for c in columns}
    extra: list[str] = []
    for spec in computed.values():
        for key in ("field_a", "field_b", "source"):
            name = spec.get(key)
            if name and name not in declared and name not in extra:
                extra.append(name)
    return extra


def _csv_load_and_typefix_statements(
    filename: str, columns: list[dict], source_ref: str, renames: dict[str, str], computed: dict[str, dict],
    expr_columns: dict[str, dict] | None = None,
    date_formats: dict[str, dict] | None = None,
    sheet_name: str | None = None,
) -> tuple[list[str], str]:
    """The Csv.Document-through-type-fixing steps shared by
    generate_csv_partition_m (one file) and generate_combined_partition_m
    (several files, each going through exactly this same treatment before
    being combined). Returns (statements, final_step_name) — does NOT
    include the `computed` columns step, which the caller applies once,
    after every file has already been loaded (and, for the multi-file
    case, combined) rather than separately per file.

    `expr_columns` ({column_name: spec}) marks a column the Qlik script
    computes for THIS specific source block rather than reading it
    directly from the file — never a real column in any source file, so
    selecting it via Table.SelectColumns would silently null it
    (MissingField.UseNull), and it can't be reproduced as a single
    post-combine `computed` expression either when a multi-file table's
    rows need something DIFFERENT depending on which file they came from
    (e.g. `'Sale' as RecordType` in one LOAD block, `'Return' as
    RecordType` in another feeding the same concatenated table). Three
    spec `"kind"`s, covering the common Qlik LOAD idioms that aren't
    already handled by the (global, post-combine) `computed` mechanism:
    - `{"kind": "literal", "value": "Sale"}` — `'Sale' as RecordType`.
    - `{"kind": "textfunc", "func": "upper", "source": "ZoneName"}` —
      `upper(ZoneName) as ZM_ZONENAME`. `func` is one of
      upper/lower/trim/capitalize.
    - `{"kind": "arith", "expr": "(Qty*UnitPrice)-Discount", "fields":
      ["Qty","UnitPrice","Discount"]}` — a script expression combining
      real fields with +-*/ and parens, e.g. `(Qty*UnitPrice)-Discount as
      NetAmount`. `expr` is ALREADY valid M syntax once each field name in
      it is wrapped as `[Field]` (Qlik and M share the same arithmetic
      operators) — see _arith_expr_to_m.

    `date_formats` ({column_name: {"source": field, "formats": [fmt,
    ...]}}) marks a column whose real source field (possibly under a
    DIFFERENT name per file, e.g. "OrderDate" vs "ReturnDate") needs
    parsing with an EXPLICIT Qlik date#() format string rather than the
    generic locale-guessing fallback below — necessary because different
    source files in the same concatenated table can each use a genuinely
    different date text shape ('MM/DD/YYYY' in one, 'YYYYMMDD' in
    another), which the generic Date.FromText/serial-number fallback
    cannot reliably tell apart (e.g. 'YYYYMMDD' text doesn't parse as
    either). `formats` has more than one entry only for a Qlik `alt(...)`
    call — tried in order, first one that parses wins."""
    expr_columns = expr_columns or {}
    date_formats = date_formats or {}
    source_columns = [c for c in columns if c["name"] not in computed and c["name"] not in expr_columns]
    extra_columns = _extra_helper_columns(columns, computed)

    declared_names = {c["name"] for c in columns}
    expr_extra_fields: list[str] = []
    for spec in expr_columns.values():
        if spec.get("kind") == "textfunc":
            src = spec.get("source")
            if src and src not in declared_names and src not in expr_extra_fields:
                expr_extra_fields.append(src)
        elif spec.get("kind") == "arith":
            for f in spec.get("fields", []):
                if f not in declared_names and f not in expr_extra_fields:
                    expr_extra_fields.append(f)

    # Columns is deliberately NOT pinned to len(source_columns) here. That
    # count is the Qlik LOAD script's OUTPUT shape (after it drops/renames/
    # computes fields), but SourceDataPath can point at the script's INPUT
    # files instead (its raw `FROM [lib://...]` source) — which commonly has
    # a DIFFERENT column count (e.g. an ID column the script deliberately
    # doesn't load). Csv.Document given an explicit wrong Columns count
    # misparses the whole file (rows shift, PromoteHeaders can't match real
    # header text), which is what turns even a column that genuinely exists
    # in the file into "The column '...' of the table wasn't found." Let
    # Csv.Document infer the column count from the file's own header row.
    # Text.TrimEnd(..., {"\", "/"}) strips a trailing slash from
    # SourceDataPath before joining the filename — whether the parameter
    # value the user typed in Manage Parameters ends with one or not (both
    # are natural things to type/paste), the joined path always has exactly
    # one separator. Without this, a value that already ends in "\" joins
    # into a DOUBLED separator ("...FoodMart\\Dim_Customers.csv"), which
    # File.Contents rejects outright ("Illegal characters in path") even
    # though the folder and filename are both completely valid on their own.
    statements: list[str] = [
        _source_load_statement(filename, source_ref, sheet_name),
        "Promoted = Table.PromoteHeaders(Source, [PromoteAllScalars=true])",
    ]
    # Tolerate the source file having EXTRA columns (drop them, same as the
    # Qlik script would) or being MISSING one of ours by this exact name
    # (comes through as a null column instead of failing the whole table's
    # refresh — surfacing one blank field is far better than blocking every
    # other field on the same table). Select by whichever name the file
    # actually uses — the rename's source name for an ordinary renamed
    # field, or the date_formats source name for a column that ALSO needs
    # per-file custom date parsing (its own source field name can differ
    # per file too, e.g. "OrderDate" vs "ReturnDate").
    def _source_name_for(c_name: str) -> str:
        if c_name in date_formats:
            return date_formats[c_name]["source"]
        return renames.get(c_name, c_name)

    select_names = [_source_name_for(c["name"]) for c in source_columns] + extra_columns + expr_extra_fields
    expected_names = ", ".join(f'"{n}"' for n in select_names)
    # A real-world CSV export very commonly spells a header slightly
    # differently from the Qlik script's own field name — different casing
    # ("customerid" vs "CustomerID"), stray spaces, or "_"/"-" instead of
    # nothing ("Net_Amount" vs "NetAmount") — while still being obviously
    # the same column. Without this step, Table.SelectColumns's exact-name
    # match would silently null that ENTIRE column via MissingField.UseNull
    # (no error, just a blank field everywhere it's used — the classic
    # "chart/KPI renders with nothing in it" symptom), even though the real
    # data is sitting right there under a near-identical name. Rename any
    # actual column whose case/spacing/punctuation-insensitive form matches
    # one of ours to our exact expected name FIRST, so only a column that's
    # truly absent (not just differently formatted) falls through to
    # MissingField.UseNull below.
    normalize_fn = (
        '(n) => Text.Lower(Text.Replace(Text.Replace(Text.Replace(Text.Trim(Text.From(n)), "_", ""), '
        '"-", ""), " ", ""))'
    )
    expected_list = "{" + expected_names + "}"
    statements.append(
        f"NormalizeName = {normalize_fn}"
    )
    statements.append(
        "RenamePairs = List.RemoveNulls(List.Transform(Table.ColumnNames(Promoted), each "
        f"let _actual = _, _match = List.Select({expected_list}, (e) => NormalizeName(e) = NormalizeName(_actual) and e <> _actual) "
        "in if List.Count(_match) > 0 then {_actual, _match{0}} else null))"
    )
    statements.append("Normalized = Table.RenameColumns(Promoted, RenamePairs, MissingField.Ignore)")
    statements.append(f"Selected = Table.SelectColumns(Normalized, {{{expected_names}}}, MissingField.UseNull)")
    step = "Selected"
    all_renames = {**renames, **{c_name: spec["source"] for c_name, spec in date_formats.items()}}
    if all_renames:
        rename_pairs = ", ".join(
            f'{{"{all_renames[c["name"]]}", "{c["name"]}"}}' for c in source_columns if c["name"] in all_renames
        )
        statements.append(f"Renamed = Table.RenameColumns({step}, {{{rename_pairs}}}, MissingField.Ignore)")
        step = "Renamed"
    text_cast = ", ".join(
        f'{{"{c["name"]}", type text}}'
        for c in source_columns + [{"name": n} for n in extra_columns + expr_extra_fields]
    )
    statements.append(f"AsText = Table.TransformColumnTypes({step}, {{{text_cast}}})")
    step = "AsText"

    # A date_formats column is parsed with its OWN explicit per-file
    # format below (custom_date_cols) instead of the generic locale-
    # guessing fallback — excluded from date_cols so it isn't ALSO run
    # through the generic path.
    custom_date_cols = [c["name"] for c in source_columns if c["name"] in date_formats]
    date_cols = [
        c["name"] for c in source_columns
        if c.get("data_type") == "dateTime" and c["name"] not in date_formats
    ]
    if custom_date_cols:
        transforms = []
        for name in custom_date_cols:
            fmts = date_formats[name]["formats"]
            # Each candidate format (only >1 for a Qlik alt(...) call) is
            # tried in order; a format that doesn't match this particular
            # value throws inside the parse expression (e.g. Number.FromText
            # on a non-numeric part), which `try/otherwise` catches and
            # moves on to the next candidate instead of failing the row.
            chain = "null"
            for fmt in reversed(fmts):
                parser = _qlik_date_format_to_m_expr("_", fmt)
                chain = f"try Date.From({parser}) otherwise {chain}"
            transforms.append(
                f'{{"{name}", each if _ = null or _ = "" then null else {chain}, type date}}'
            )
        statements.append(f"CustomDatesFixed = Table.TransformColumns({step}, {{{', '.join(transforms)}}})")
        step = "CustomDatesFixed"

    if date_cols:
        # Two genuinely different source shapes can land here: an ordinary,
        # human-readable date string (SourceDataPath pointed at a real CSV
        # export — "2026-05-20") or a Qlik serial-day number as text (only
        # true when the CSV came from THIS pipeline's own earlier extractor,
        # which wrote raw #(qNum) values). Try the real-text parse first, and
        # only fall back to the serial-number interpretation if that fails —
        # `try ... otherwise` never lets a row that matches neither shape
        # hard-error the whole column, it just becomes null.
        transforms = ", ".join(
            f'{{"{name}", each if _ = null or _ = "" then null else '
            f'try Date.From(Date.FromText(_, "en-US")) otherwise '
            f'try Date.From(#date(1899, 12, 30) + #duration(Number.FromText(_, "en-US"), 0, 0, 0)) '
            f'otherwise null, type date}}'
            for name in date_cols
        )
        statements.append(f"DatesFixed = Table.TransformColumns({step}, {{{transforms}}})")
        step = "DatesFixed"

    numeric_cols = [c for c in source_columns if c.get("data_type") in ("int64", "double") and c["name"] not in date_cols]
    # An "arith" spec's helper fields (e.g. Qty/UnitPrice/Discount feeding
    # `(Qty*UnitPrice)-Discount`) aren't part of the table's own declared
    # columns, so they have no known data_type to drive this cast from —
    # arithmetic on them only makes sense as numbers, so default them to
    # double, the same safe assumption `_reconcile_column_types` falls back
    # to elsewhere in this pipeline when a type genuinely can't be known.
    arith_extra_names = {
        f for spec in expr_columns.values() if spec.get("kind") == "arith" for f in spec.get("fields", [])
        if f in expr_extra_fields
    }
    if numeric_cols or arith_extra_names:
        transforms = []
        for c in numeric_cols:
            target_type = _TYPE_LITERAL[c["data_type"]]
            wrap = "Int64.From" if c["data_type"] == "int64" else ""
            expr = 'Number.FromText(_, "en-US")'
            if wrap:
                expr = f"{wrap}({expr})"
            # try/otherwise: a value that doesn't actually parse as a number
            # (a source/metadata mismatch) becomes null instead of
            # hard-erroring the whole column.
            transforms.append(
                f'{{"{c["name"]}", each if _ = null or _ = "" then null else try {expr} otherwise null, {target_type}}}'
            )
        for name in arith_extra_names:
            transforms.append(
                f'{{"{name}", each if _ = null or _ = "" then null else '
                f'try Number.FromText(_, "en-US") otherwise null, type number}}'
            )
        statements.append(f"NumbersFixed = Table.TransformColumns({step}, {{{', '.join(transforms)}}})")
        step = "NumbersFixed"

    # "variant" columns (see project.py: a field whose NAME is specifically
    # about a month, which can genuinely hold either shape depending on the
    # real source file — "6"/"06" or "Jun"/"June"). There's no real TMDL
    # column type for "could be either" (see semantic_model.py's
    # DATA_TYPE_MAP — this is declared as a plain `string` column), so
    # normalize to text here too rather than the mixed `type any` this used
    # to produce: a numeric value ("6") is kept as its own text ("6", not
    # reformatted), and any other text passes through unchanged. This loses
    # nothing a real "variant" column would have kept anyway — Power BI
    # would have displayed a mixed column as text-or-number per row either
    # way, and every DAX function that needs a number here already coerces
    # numeric-looking text automatically (e.g. SUM, MONTH()).
    variant_cols = [c for c in source_columns if c.get("data_type") == "variant"]
    if variant_cols:
        transforms = ", ".join(
            f'{{"{c["name"]}", each let _t = if _ = null then "" else Text.Trim(Text.From(_)) in '
            f'if _t = "" then null else _t, type text}}'
            for c in variant_cols
        )
        statements.append(f"VariantFixed = Table.TransformColumns({step}, {{{transforms}}})")
        step = "VariantFixed"

    boolean_cols = [c["name"] for c in source_columns if c.get("data_type") == "boolean"]
    if boolean_cols:
        transforms = ", ".join(
            f'{{"{name}", each _ = "true" or _ = "1" or _ = "-1", type logical}}'
            for name in boolean_cols
        )
        statements.append(f"BoolsFixed = Table.TransformColumns({step}, {{{transforms}}})")
        step = "BoolsFixed"

    if expr_columns:
        for name, spec in expr_columns.items():
            kind = spec.get("kind")
            if kind == "literal":
                escaped = spec["value"].replace('"', '""')
                expr = f'each "{escaped}"'
                target_type = "type text"
            elif kind == "textfunc":
                m_func = {"upper": "Text.Upper", "lower": "Text.Lower", "trim": "Text.Trim",
                          "capitalize": "Text.Proper"}[spec["func"]]
                expr = f'each {m_func}(Text.From([{spec["source"]}]))'
                target_type = "type text"
            elif kind == "arith":
                expr = f"each {_arith_expr_to_m(spec['expr'], spec['fields'])}"
                target_type = "type number"
            else:
                continue
            statements.append(f'Expr_{_ident(name)} = Table.AddColumn({step}, "{name}", {expr}, {target_type})')
            step = f"Expr_{_ident(name)}"

        if expr_extra_fields:
            drop_list = ", ".join(f'"{n}"' for n in expr_extra_fields)
            statements.append(f"ExprHelpersDropped = Table.RemoveColumns({step}, {{{drop_list}}})")
            step = "ExprHelpersDropped"

    return statements, step


def _ident(name: str) -> str:
    """A safe bare M step-name fragment from a column name (step names can't
    contain spaces/punctuation the way quoted identifiers can)."""
    return "".join(c if c.isalnum() else "_" for c in name)
