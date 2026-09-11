"""Write a *.SemanticModel/ folder (TMDL) from the converted
data model / measures / dimensions / variables / RLS artifacts."""

from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from typing import Any

from .report import write_platform_file


def _guid() -> str:
    return str(uuid.uuid4())


def _indent_block(text: str, tabs: int = 3) -> str:
    prefix = "\t" * tabs
    return "\n".join(prefix + line if line.strip() else line for line in text.splitlines())


def _render_multiline_stmt(first_line_prefix: str, expression: str, cont_tabs: int) -> list[str]:
    """Render `<prefix><first line>` followed by continuation lines indented
    with `cont_tabs` leading tabs each.

    TMDL delimits a multi-line measure/column expression purely by
    indentation: a continuation line only counts as part of the expression
    if it's indented MORE than that object's own PROPERTY lines (dataType:,
    lineageTag:, formatString:, etc. — not just more than the `measure`/
    `column` declaration line itself). Those properties sit one level
    deeper than the declaration, so the continuation has to go one level
    deeper still: an LLM-generated DAX expression is naturally formatted
    with spaces, not tabs, and writing it through at the SAME depth as the
    object's own properties either truncates the expression at its first
    line the moment it's parsed back ("Name = DIVIDE(" with nothing after),
    or — if a line happens to look like "word = value" (a DAX `VAR x = ...`
    is exactly that shape) — gets misread as an attempted property
    assignment outright ("VAR is not a supported property in the current
    context").

    For a genuinely multi-line expression, nothing follows the "=" on the
    declaration line at all — every line of the expression, including what
    would otherwise be "the first line", is indented on its own line
    instead. This is the same convention this file already uses (and
    which, unlike measures/calc-columns, was never reported broken) for a
    table's own `partition 'X' = m` / `= calculated`: "source =" ends the
    line bare, and _indent_block puts the entire body on indented lines
    below it. Gluing the first line of a multi-line DAX expression onto the
    declaration line (the previous behavior here) is exactly what produced
    "Unexpected line type: Other!" on some later continuation line — a
    single-line expression is unaffected either way and keeps reading
    naturally as "measure 'Name' = <expr>" on one line.
    """
    expr_lines = expression.splitlines() or [""]
    cont_prefix = "\t" * cont_tabs
    if len(expr_lines) == 1:
        return [f"{first_line_prefix}{expr_lines[0]}"]
    out = [first_line_prefix.rstrip()]
    for line in expr_lines:
        stripped = line.strip()
        out.append(f"{cont_prefix}{stripped}" if stripped else "")
    return out


def _strip_redundant_name_prefix(name: str, expr: str | None) -> str | None:
    """A generated measure/calculated-column expression occasionally
    restates its own assignment as the first line (observed: a calculated
    column named 'Scheme' whose "expression" field literally started with
    "Scheme =\\nVAR ..."). The TMDL writer already emits "column 'Name' = "
    (or "measure 'Name' = ") itself, so a redundant leading "Name =" line
    inside the expression text doubles up into invalid DAX ("... = Scheme
    =\\nVAR ...") instead of the intended plain value expression. Strip it
    if present; leave everything else untouched."""
    if not expr:
        return expr
    lines = expr.splitlines()
    if not lines:
        return expr
    first = lines[0].strip().casefold()
    if first in (f"{name} =".casefold(), f"'{name}' =".casefold(), f'"{name}" ='.casefold()):
        return "\n".join(lines[1:]).lstrip("\n") or None
    return expr


DATA_TYPE_MAP = {
    "int64": "int64", "double": "double", "string": "string",
    "dateTime": "dateTime", "boolean": "boolean", "decimal": "double",
}


def write_semantic_model(
    sm_dir: str,
    *,
    app_name: str,
    tables: dict[str, dict],       # table_name -> {"columns": [...], "m_expression": str}
    measures_by_table: dict[str, list[dict]],
    calculated_columns_by_table: dict[str, list[dict]],
    hierarchies_by_table: dict[str, list[dict]],
    relationships: list[dict],
    roles: list[dict],
    parameters: list[dict],
    source_data_parameter: dict | None = None,  # {"name": "SourceDataPath", "default_value": "..."}
) -> None:
    defn_dir = os.path.join(sm_dir, "definition")
    tables_dir = os.path.join(defn_dir, "tables")
    # Regenerate tables/ from a CLEAN slate. If Power BI Desktop has opened
    # this .pbip before, it will have injected its own hidden helper tables
    # (Auto date/time: DateTableTemplate_<guid>, one LocalDateTable_<guid>
    # per date column) straight into this folder — PBIP saves live. Our
    # rebuild rewrites model.tmdl (where the matching `variation` wiring
    # would live) but left those helper .tmdl files behind, so on the next
    # open Desktop sees a LocalDateTable that nothing varies onto and
    # refuses the project ("...must be a target of a variation..."). Wiping
    # the folder each build keeps it exactly equal to what we produced.
    if os.path.isdir(tables_dir):
        shutil.rmtree(tables_dir)
    os.makedirs(tables_dir, exist_ok=True)

    # Same as *.Report/.platform + definition.pbir — pbip-compiler doesn't
    # need these, but Power BI Desktop requires them to open the .pbip
    # project directly.
    write_platform_file(sm_dir, item_type="SemanticModel", display_name=app_name)
    # $schema confirmed against a real Desktop-authored definition.pbism
    # (found bundled as a pbix-mcp test fixture) — our earlier version
    # omitted it entirely, which is very likely part of why Power BI
    # Desktop has been refusing to open the raw .pbip project directly.
    with open(os.path.join(sm_dir, "definition.pbism"), "w", encoding="utf-8") as f:
        json.dump({
            "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/semanticModel/definitionProperties/1.0.0/schema.json",
            "version": "4.0",
            "settings": {},
        }, f, indent=2)

    for table_name, table in tables.items():
        content = _render_table_tmdl(
            table_name,
            table.get("columns", []),
            measures_by_table.get(table_name, []),
            calculated_columns_by_table.get(table_name, []),
            hierarchies_by_table.get(table_name, []),
            table.get("m_expression", ""),
            table_is_hidden=table.get("is_hidden", False),
            is_calculated=table.get("is_calculated", False),
            dax_expression=table.get("dax_expression", ""),
        )
        with open(os.path.join(tables_dir, f"{_safe(table_name)}.tmdl"), "w", encoding="utf-8") as f:
            f.write(content)

    if parameters:
        _write_parameters_table(tables_dir, parameters)

    if source_data_parameter:
        _write_expressions_tmdl(defn_dir, source_data_parameter)
    else:
        _remove_if_exists(os.path.join(defn_dir, "expressions.tmdl"))

    with open(os.path.join(defn_dir, "relationships.tmdl"), "w", encoding="utf-8") as f:
        f.write(_render_relationships_tmdl(relationships))

    with open(os.path.join(defn_dir, "model.tmdl"), "w", encoding="utf-8") as f:
        f.write(_render_model_tmdl())

    if roles:
        with open(os.path.join(defn_dir, "roles.tmdl"), "w", encoding="utf-8") as f:
            f.write(_render_roles_tmdl(roles))
    else:
        _remove_if_exists(os.path.join(defn_dir, "roles.tmdl"))


def _remove_if_exists(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def _q(name: str) -> str:
    """A single-quoted TMDL object name with any embedded single quote
    doubled ('' — the TMDL escape). A Qlik title can legitimately contain a
    quote (seen: a measure literally titled 'FILTERS'); writing it raw as
    `measure ''FILTERS''` is a parse error ("single-quote character in name
    must be escaped")."""
    return "'" + str(name).replace("'", "''") + "'"


def _render_table_tmdl(
    name: str, columns: list[dict], measures: list[dict],
    calc_columns: list[dict], hierarchies: list[dict], m_expression: str,
    table_is_hidden: bool = False, is_calculated: bool = False, dax_expression: str = "",
) -> str:
    lines = [f"table {_q(name)}", f"\tlineageTag: {_guid()}"]
    if table_is_hidden:
        lines.append("\tisHidden")
    lines.append("")

    for col in columns:
        dtype = DATA_TYPE_MAP.get(col.get("data_type", "string"), "string")
        lines.append(f"\tcolumn {_q(col['name'])}")
        lines.append(f"\t\tdataType: {dtype}")
        lines.append(f"\t\tlineageTag: {_guid()}")
        lines.append("\t\tsummarizeBy: none")
        lines.append(f"\t\tsourceColumn: {col.get('source_column', col['name'])}")
        if col.get("is_hidden"):
            lines.append("\t\tisHidden")
        lines.append("")
        lines.append("\t\tannotation SummarizationSetBy = Automatic")
        lines.append("")

    for col in calc_columns:
        expr = _strip_redundant_name_prefix(col["name"], col.get("expression"))
        if not expr:
            print(f"[build] WARNING: calculated column '{col['name']}' on table '{name}' has no expression — skipping it")
            continue
        # cont_tabs=3, not 2: this column's own properties (dataType:,
        # lineageTag:, etc.) sit at 2 tabs — a multi-line expression's
        # continuation lines have to be indented DEEPER than that or
        # Desktop's TMDL parser can't tell a continuation apart from a
        # sibling property line, and misreads content like "VAR x = ..." as
        # an attempted (and invalid) property assignment ("VAR is not a
        # supported property in the current context").
        lines.extend(_render_multiline_stmt(f"\tcolumn {_q(col['name'])} = ", expr, cont_tabs=3))
        lines.append("\t\tdataType: string")
        lines.append(f"\t\tlineageTag: {_guid()}")
        lines.append("\t\tsummarizeBy: none")
        lines.append("")

    for m in measures:
        expr = _strip_redundant_name_prefix(m["name"], m.get("expression"))
        if not expr:
            print(f"[build] WARNING: measure '{m['name']}' on table '{name}' has no expression — skipping it "
                  f"(a conversion step produced an incomplete measure; check converted/*.json for '{m['name']}')")
            continue
        # Same reasoning as calc_columns above: this measure's own
        # properties (formatString:, lineageTag:, etc.) sit at 2 tabs, so
        # continuation lines need to be deeper than that (3), not just
        # deeper than the "measure 'X' =" declaration line itself (1).
        lines.extend(_render_multiline_stmt(f"\tmeasure {_q(m['name'])} = ", expr, cont_tabs=3))
        if m.get("format_string"):
            lines.append(f"\t\tformatString: {m['format_string']}")
        if m.get("is_hidden"):
            lines.append("\t\tisHidden")
        lines.append(f"\t\tlineageTag: {_guid()}")
        lines.append("")

    for h in hierarchies:
        lines.append(f"\thierarchy {_q(h['name'])}")
        lines.append(f"\t\tlineageTag: {_guid()}")
        lines.append("")
        for level in h.get("levels", []):
            lines.append(f"\t\tlevel {_q(level['name'])}")
            lines.append(f"\t\t\tcolumn: {level['column']}")
            lines.append("")

    if is_calculated and dax_expression:
        # A DAX calculated table (created "from the Power BI frontend", the
        # same as a person clicking Modeling > New Table and typing a DAX
        # formula in Desktop) — the table's own partition IS the DAX
        # expression, not a Power Query source. pbip-compiler's TmdlParser
        # recognizes 'calculated' as a distinct partition type but only
        # ever pulls the source out of an 'm'-type one (see
        # pbip_build/pbix_compile.py's notes on this package's other
        # documented gaps) — a calculated table safely no-ops in the
        # compiled .pbix (placeholder row only, same as any table with no
        # M) rather than crashing, and is fully real when the .pbip project
        # is opened directly in Power BI Desktop.
        lines.append(f"\tpartition {_q(name)} = calculated")
        lines.append("\t\tmode: import")
        lines.append("\t\tsource =")
        lines.append(_indent_block(dax_expression, tabs=3))
        lines.append("")
    elif m_expression:
        lines.append(f"\tpartition {_q(name)} = m")
        lines.append("\t\tmode: import")
        lines.append("\t\tsource =")
        lines.append(_indent_block(m_expression, tabs=3))
        lines.append("")

    return "\n".join(lines)


def _write_parameters_table(tables_dir: str, parameters: list[dict]) -> None:
    """Power Query parameters aren't real TMDL tables; emit them as a
    documented queries group the M step above can reference by name. Written
    as a comment-only .pq reference file since pbip-compiler's TMDL parser
    doesn't model parameters — Power BI Desktop's own Power Query editor is
    where these get turned into first-class parameters after opening the
    .pbip, so we keep the definitions alongside for that manual step."""
    lines = ["// Power Query parameters (add via Home > Manage Parameters in Power BI Desktop)", ""]
    for p in parameters:
        pq = p.get("power_query")
        if not pq:
            continue
        lines.append(f"// {p['name']}: type={pq.get('type')} default={pq.get('current_value')}")
    with open(os.path.join(tables_dir, "..", "parameters.pq.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def _write_expressions_tmdl(defn_dir: str, source_data_parameter: dict) -> None:
    """A real, first-class Power Query Parameter (shown in Power BI
    Desktop's Manage Parameters dialog), written as a TMDL shared M
    expression. Every table's M references it by this same bare name
    (see csv_m.py) — moving the source data (a different folder, a network
    share, a folder of SQL-exported CSVs using the same table/column names)
    is then a single edit here instead of touching every table's query.

    NOTE: pbip-compiler has no concept of a Power Query Parameter/shared
    expression object at all (its SemanticModel model only knows tables and
    relationships — see pbip_build/pbix_compile.py's other worked-around
    gaps in that same package). It can't carry this into the compiled
    .pbix, so project.py compiles from a separate copy with the parameter
    reference substituted for its literal value instead. This file — and
    the real parameter reference in each table's M — is only exercised when
    the .pbip project itself is opened directly in Power BI Desktop, where
    it works as a normal, editable parameter."""
    def _text_param(pname: str, pvalue: str, required: bool) -> list[str]:
        req = "true" if required else "false"
        return [
            f'expression {pname} = "{pvalue.replace(chr(34), chr(34) * 2)}" '
            f'meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired={req}]',
            f"\tlineageTag: {_guid()}",
            "",
            "\tannotation PBI_ResultType = Text",
            "",
        ]

    lines = _text_param(source_data_parameter["name"], source_data_parameter["default_value"], True)

    # Optional SQL Server source. Empty SqlServer (the default) => every table
    # stays on its CSV; set SqlServer + SqlDatabase in Manage Parameters and
    # every table reads [SqlSchema].[<TableName>] from that database instead.
    # Credentials are entered in Power BI on first refresh, not stored here.
    sql = source_data_parameter.get("sql")
    if sql:
        lines += _text_param(sql["server_param"], "", False)
        lines += _text_param(sql["database_param"], "", False)
        lines += _text_param(sql["schema_param"], sql.get("schema_default", "dbo"), False)

    with open(os.path.join(defn_dir, "expressions.tmdl"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def _tmdl_qualify(table: str, column: str) -> str:
    """TMDL's relationship fromColumn/toColumn value is a single
    'Table.Column' token, not separate fromTable/toTable + fromColumn/
    toColumn properties — confirmed against a real Desktop-authored
    relationships.tmdl (the same pbix-mcp test fixture used for the
    version.json/$schema/model.tmdl fixes). A table name that isn't a bare
    identifier (spaces, '%', etc. — e.g. a what-if parameter table like
    'Sales Achievement %') needs single-quoting, the same convention DAX
    itself uses for 'Table'[Column]."""
    if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", table):
        return f"{table}.{column}"
    return f"'{table}'.{column}"


def _render_relationships_tmdl(relationships: list[dict]) -> str:
    out = []
    for rel in relationships:
        out.append(f"relationship {_guid()}")
        out.append(f"\tfromColumn: {_tmdl_qualify(rel['from_table'], rel['from_column'])}")
        out.append(f"\ttoColumn: {_tmdl_qualify(rel['to_table'], rel['to_column'])}")
        # A relationship omitting cardinality defaults to many-to-one
        # (fromCardinality: many, toCardinality: one). When infer_relationships
        # found the "from" side's key ALSO fully unique it's really 1:1 —
        # mark it so, because RELATED() then traverses in BOTH directions: a
        # measure that iterates the "to" table with SUMX and pulls a "from"
        # table column via RELATED() (which would be an invalid one->many
        # hop under the default cardinality — "column ... doesn't have a
        # relationship to any table available in the current context")
        # works once the relationship is genuinely one-to-one.
        if rel.get("cardinality") == "one-to-one":
            out.append("\tfromCardinality: one")
            out.append("\ttoCardinality: one")
            # Power BI requires a one-to-one relationship to filter both
            # ways ("CrossFilterDirection for One-to-One relationships
            # should always be set to BothDirections") — it rejects the
            # project outright otherwise.
            out.append("\tcrossFilteringBehavior: bothDirections")
        if rel.get("is_active") is False:
            out.append("\tisActive: false")
        out.append("")
    return "\n".join(out)


def _render_model_tmdl() -> str:
    # A model-level `annotation` line is NOT a nested property of `model
    # Model` — confirmed against a real Desktop-authored model.tmdl (the
    # same pbix-mcp test fixture used to fix version.json/$schema earlier):
    # its annotations sit at column 0, separated from the indented property
    # block by a blank line. Indenting it under the model block like the
    # other properties (culture:, etc.) is exactly what Desktop's TMDL
    # parser rejected with "Invalid indentation was detected!".
    # __PBI_TimeIntelligenceEnabled = 0 turns OFF "Auto date/time". Left on
    # (the default), Power BI Desktop silently generates a hidden
    # LocalDateTable_<guid> calculated table for every dateTime column plus a
    # DateTableTemplate, and writes them into definition/tables/ the first
    # time it opens the project. Those helper tables + their `variation`
    # wiring are fragile to regenerate and were causing "LocalDateTable ...
    # must be a target of a variation" load failures. We don't need them
    # (real date logic belongs in the MasterCalendar table), so disable it.
    return (
        "model Model\n"
        "\tculture: en-US\n"
        "\tdefaultPowerBIDataSourceVersion: powerBI_V3\n"
        "\tsourceQueryCulture: en-US\n"
        "\n"
        "annotation PBIDesktopVersion = 2.130\n"
        "\n"
        "annotation __PBI_TimeIntelligenceEnabled = 0\n"
    )


def _render_roles_tmdl(roles: list[dict]) -> str:
    out = []
    for role in roles:
        out.append(f"role {_q(role['name'])}")
        out.append(f"\tmodelPermission: read")
        for perm in role.get("table_permissions", []):
            out.append("")
            out.extend(_render_multiline_stmt(f"\ttablePermission {_q(perm['table'])} = ", perm["filter"], cont_tabs=2))
        out.append("")
    return "\n".join(out)


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "_-. " else "_" for c in name)
