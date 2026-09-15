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
    computed: dict[str, dict] | None = None,
    renames: dict[str, str] | None = None,
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
    csv_block = generate_csv_partition_m(filename, columns, source_ref=source_ref, computed=computed, renames=renames)
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
    statements, step = _csv_load_and_typefix_statements(filename, columns, source_ref, renames, computed)
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

    agg_names = [a["alias"] for a in aggregations]
    agg_list = ", ".join(f'"{a}"' for a in agg_names)
    statements.append(
        f'Merged = Table.NestedJoin(Base, {{{key_list}}}, Grouped, {{{key_list}}}, "JoinedAgg", JoinKind.LeftOuter)'
    )
    statements.append(f'Expanded = Table.ExpandTableColumn(Merged, "JoinedAgg", {{{agg_list}}}, {{{agg_list}}})')

    body = ",\n    ".join(statements)
    return f"let\n    {body}\nin\n    Expanded"


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
    just fed more than one file.
    """
    computed = computed or {}
    renames = renames or {}
    per_file_blocks = []
    for i, filename in enumerate(filenames):
        file_statements, file_step = _csv_load_and_typefix_statements(filename, columns, source_ref, renames, computed)
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
) -> tuple[list[str], str]:
    """The Csv.Document-through-type-fixing steps shared by
    generate_csv_partition_m (one file) and generate_combined_partition_m
    (several files, each going through exactly this same treatment before
    being combined). Returns (statements, final_step_name) — does NOT
    include the `computed` columns step, which the caller applies once,
    after every file has already been loaded (and, for the multi-file
    case, combined) rather than separately per file."""
    source_columns = [c for c in columns if c["name"] not in computed]
    extra_columns = _extra_helper_columns(columns, computed)

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
        f'Source = Csv.Document(File.Contents(Text.TrimEnd({source_ref}, {{"\\", "/"}}) & "\\{filename}"), '
        f'[Delimiter=",", Encoding=65001, QuoteStyle=QuoteStyle.Csv])',
        "Promoted = Table.PromoteHeaders(Source, [PromoteAllScalars=true])",
    ]
    # Tolerate the source file having EXTRA columns (drop them, same as the
    # Qlik script would) or being MISSING one of ours by this exact name
    # (comes through as a null column instead of failing the whole table's
    # refresh — surfacing one blank field is far better than blocking every
    # other field on the same table). Select by whichever name the file
    # actually uses (the rename's source name, when this column is renamed).
    select_names = [renames.get(c["name"], c["name"]) for c in source_columns] + extra_columns
    expected_names = ", ".join(f'"{n}"' for n in select_names)
    statements.append(f"Selected = Table.SelectColumns(Promoted, {{{expected_names}}}, MissingField.UseNull)")
    step = "Selected"
    if renames:
        rename_pairs = ", ".join(f'{{"{renames[c["name"]]}", "{c["name"]}"}}' for c in source_columns if c["name"] in renames)
        statements.append(f"Renamed = Table.RenameColumns({step}, {{{rename_pairs}}}, MissingField.Ignore)")
        step = "Renamed"
    text_cast = ", ".join(f'{{"{c["name"]}", type text}}' for c in source_columns + [{"name": n} for n in extra_columns])
    statements.append(f"AsText = Table.TransformColumnTypes({step}, {{{text_cast}}})")
    step = "AsText"

    date_cols = [c["name"] for c in source_columns if c.get("data_type") == "dateTime"]
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
    if numeric_cols:
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
        statements.append(f"NumbersFixed = Table.TransformColumns({step}, {{{', '.join(transforms)}}})")
        step = "NumbersFixed"

    # "variant" columns (see project.py: a field whose NAME is specifically
    # about a month, which can genuinely hold either shape depending on the
    # real source file — "6"/"06" or "Jun"/"June"). Keep whichever shape the
    # value actually is instead of forcing one: a value that parses as a
    # number becomes a real number, anything else passes through as its
    # original text. No target type is pinned on the TransformColumns call
    # (`type any`) so Power Query doesn't lock the column to one shape and
    # reject the other.
    variant_cols = [c for c in source_columns if c.get("data_type") == "variant"]
    if variant_cols:
        transforms = ", ".join(
            f'{{"{c["name"]}", each let _t = if _ = null then "" else Text.Trim(Text.From(_)) in '
            f'if _t = "" then null else try Number.FromText(_t, "en-US") otherwise _t, type any}}'
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

    return statements, step


def _ident(name: str) -> str:
    """A safe bare M step-name fragment from a column name (step names can't
    contain spaces/punctuation the way quoted identifiers can)."""
    return "".join(c if c.isalnum() else "_" for c in name)
