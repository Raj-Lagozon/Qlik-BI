"""Assemble converted artifacts into a *.pbip project (Report + SemanticModel)
under output/<app_name>/, then compile it to a real .pbix."""

from __future__ import annotations

import csv
import glob
import json
import os
import re
import shutil
import tempfile

from .semantic_model import write_semantic_model
from .report import write_report
from .pbix_compile import compile_pbix
from .csv_m import generate_csv_partition_m, generate_partition_m
from .infer_relationships import infer_relationships
from .what_if_params import detect_what_if_parameters, generate_range_m

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXTRACTED_ROOT = os.path.join(ROOT, "extracted")
CONVERTED_ROOT = os.path.join(ROOT, "converted")

# Every table's CSV partition M references this one shared Power Query
# Parameter for its base folder (see csv_m.py / semantic_model.py's
# expressions.tmdl writer) instead of a literal path baked into each table.
SOURCE_DATA_PARAM_NAME = "SourceDataPath"

# Optional SQL Server source. When the SqlServer parameter is left empty (the
# default) every table loads from its CSV under SourceDataPath exactly as
# before; fill SqlServer + SqlDatabase in Power BI Desktop's Manage
# Parameters dialog and every table instead reads [SqlSchema].[<TableName>]
# straight from that database (column names already match the Qlik script).
# Power BI prompts for the SQL credentials on first refresh and stores them
# in its own per-user credential store — they are never written to the
# .pbip / .pbix.
SQL_SERVER_PARAM_NAME = "SqlServer"
SQL_DATABASE_PARAM_NAME = "SqlDatabase"
SQL_SCHEMA_PARAM_NAME = "SqlSchema"
SQL_SCHEMA_DEFAULT = "dbo"

_SQL_PARTITION_REFS = {
    "server_ref": SQL_SERVER_PARAM_NAME,
    "database_ref": SQL_DATABASE_PARAM_NAME,
    "schema_ref": SQL_SCHEMA_PARAM_NAME,
}


def _default_source_data_path(extracted_dir: str) -> str:
    """Default value for the SourceDataPath parameter: the folder the .qvf
    extraction step already wrote each table's CSV into. This is only the
    STARTING value — SourceDataPath is a real Power Query Parameter in both
    the .pbip project and the compiled .pbix (Power BI Desktop > Transform
    data > Manage Parameters), so repointing every table at a different
    folder (a moved copy of the extracted CSVs, a network share, or a
    folder of SQL-exported CSVs using the SAME table/column names) is done
    there, in the file itself — no rebuild, no re-running the .qvf
    extraction, and no environment/config setting involved."""
    return os.path.abspath(os.path.join(extracted_dir, "data"))


OUTPUT_ROOT = os.path.join(ROOT, "output")


def build_project(app_name: str) -> str:
    """Returns the path to the compiled .pbix file."""
    extracted_dir = os.path.join(EXTRACTED_ROOT, app_name)
    converted_dir = os.path.join(CONVERTED_ROOT, app_name)
    project_dir = os.path.join(OUTPUT_ROOT, app_name)
    os.makedirs(project_dir, exist_ok=True)

    (tables, measures_by_table, calc_cols_by_table, hierarchies_by_table,
     original_measure_table) = _assemble_semantic_inputs(extracted_dir, converted_dir)

    llm_relationships = _load_json(converted_dir, "data_model.converted.json").get("relationships", [])
    inferred_relationships = infer_relationships(tables, extracted_dir)
    relationships = _merge_relationships(llm_relationships, inferred_relationships)
    roles = _load_json(converted_dir, "rls.converted.json").get("roles", [])
    parameters = _load_json(converted_dir, "variables.converted.json").get("variables", [])
    parameters = [p for p in parameters if p.get("target") == "power_query_parameter"]

    pages = _load_pages(converted_dir)

    # Sheet-local KPI expressions that never made it into the app's master
    # measures list (e.g. a single "Sum([ClosingStock])" typed straight into
    # one KPI object) get bound by the report_visuals conversion as a plain
    # field reference using the KPI's display label — which isn't a real
    # column, so Power BI shows "fields that need to be fixed". Recover the
    # real field + aggregation from the sheet's own hypercube definition
    # (ground truth) and add it as a proper DAX measure before the model is
    # written, so the visual can bind to it the same way any master measure
    # does.
    label_to_agg, label_to_field = _build_sheet_label_lookups(extracted_dir)
    kpi_object_titles = _build_kpi_object_titles(extracted_dir)
    _synthesize_adhoc_measures(pages, tables, measures_by_table, original_measure_table, label_to_agg)

    # A Qlik "variable input" slider (e.g. qlik-variable-input) lets the user
    # manually drive a variable's value within a numeric range — other
    # measures reference it by the slider's display label the same way they'd
    # reference a real measure. Give it a real Power BI equivalent: a small
    # parameter table holding the range plus a SELECTEDVALUE() measure named
    # after that same label, so those existing references resolve as-is.
    what_if_renamed = _apply_what_if_parameters(extracted_dir, tables, measures_by_table, original_measure_table)

    # A chart's dimension/measure can also be a COMPLEX calculated
    # expression (an If()/nested Sum() typed straight into the object, not a
    # bare "Sum(Field)") that the simple label_to_agg/label_to_field regex
    # matching above can't handle — those get their full DAX conversion from
    # llm_convert (adhoc_expressions.converted.json, reusing the same
    # dax_measures/dax_columns_hierarchies skills a real master item would
    # go through) and are merged in here the same way.
    _apply_adhoc_expressions(converted_dir, tables, measures_by_table, calc_cols_by_table, original_measure_table, label_to_field)

    # Some Qlik apps drive a whole set of KPI tiles from one config table (a
    # "KPI container" pattern) read at runtime by a generic extension rather
    # than exposing each KPI as its own chart object — the normal per-object
    # extraction never sees these. Turn each detected config table's rows
    # into real KPI cards on their own page, and hide the raw config table
    # itself (it's plumbing, not something an end user should browse).
    kpi_containers = _load_kpi_containers(converted_dir)
    kpi_pages = _apply_kpi_containers(kpi_containers, tables, measures_by_table, original_measure_table)
    pages.extend(kpi_pages)

    rename_map = _dedupe_measure_names(measures_by_table)
    # Applied late, after every measure (master, ad-hoc, KPI-container) has
    # been added — a bare '[vVarName]' reference to a what-if parameter can
    # sit inside ANY of those, not just ones that already existed when
    # _apply_what_if_parameters ran.
    _rewrite_renamed_measure_refs(measures_by_table, calc_cols_by_table, what_if_renamed)
    _rewrite_renamed_measure_refs(measures_by_table, calc_cols_by_table, rename_map)
    _fix_measure_self_references(measures_by_table, calc_cols_by_table, tables)
    _fix_phantom_table_refs(measures_by_table, calc_cols_by_table, tables)

    # Deterministic last line of defence against the single most common
    # cause of "opens with errors": a numeric aggregation (SUM/AVERAGE/...)
    # over a column the model treats as text — either because the LLM data
    # model typed it "string", or because its extracted CSV column is empty
    # so nothing could be sniffed. Power BI raises a hard calculation error
    # ("The function AVERAGE cannot work with values of type String") that
    # blanks every visual on the page, not just the one measure. Reconcile
    # the column's type (and its Power Query coercion) to how measures
    # actually use it, or neutralise the measure if the real data forbids it.
    _reconcile_column_types(tables, measures_by_table, calc_cols_by_table, extracted_dir)

    source_data_path = _default_source_data_path(extracted_dir)
    sm_dir = os.path.join(project_dir, f"{app_name}.SemanticModel")
    write_semantic_model(
        sm_dir,
        app_name=app_name,
        tables=tables,
        measures_by_table=measures_by_table,
        calculated_columns_by_table=calc_cols_by_table,
        hierarchies_by_table=hierarchies_by_table,
        relationships=relationships,
        roles=roles,
        parameters=parameters,
        source_data_parameter={
            "name": SOURCE_DATA_PARAM_NAME,
            "default_value": source_data_path,
            "sql": {
                "server_param": SQL_SERVER_PARAM_NAME,
                "database_param": SQL_DATABASE_PARAM_NAME,
                "schema_param": SQL_SCHEMA_PARAM_NAME,
                "schema_default": SQL_SCHEMA_DEFAULT,
            },
        },
    )

    _fix_field_and_measure_refs(
        pages, tables, calc_cols_by_table, original_measure_table, rename_map, label_to_field,
        kpi_object_titles=kpi_object_titles,
    )
    _sanitize_visual_shapes(pages)
    report_dir = os.path.join(project_dir, f"{app_name}.Report")
    write_report(report_dir, pages, app_name=app_name)

    _write_pbip_file(project_dir, app_name)

    # pbip-compiler has no concept of a Power Query Parameter/shared
    # expression object at all (its SemanticModel model only knows tables
    # and relationships), so the compiled .pbix would have every table's
    # 'SourceDataPath' reference dangling with nothing to resolve it
    # against, failing at Refresh with "SourceDataPath wasn't recognized".
    #
    # An earlier version of this closed that gap by inserting the missing
    # Expression row directly into the compiled .pbix's internal metadata
    # store after compiling — modeled on pbix-mcp's own RangeStart/RangeEnd
    # parameter writer, and it read back correctly through this same
    # library's own decompressor — but Power BI Desktop's full load (not
    # just a metadata read-back) crashed opening the result:
    # "TMCacheManager::CreateEmptyCollectionsForAllParents" — a VertiPaq
    # consistency check the raw INSERT didn't satisfy (almost certainly a
    # required bookkeeping row/field this reverse-engineered schema still
    # doesn't fully replicate). That's real, Desktop-confirmed corruption,
    # not a Python-side bug to chase further blind — reverted.
    #
    # Compile instead from a throwaway copy with the parameter reference
    # substituted for its literal current value. The real project on disk
    # (output/<app>/<app>.SemanticModel, opened directly in Power BI
    # Desktop) keeps the genuine, editable parameter — only the compiled
    # .pbix loses the "edit in one place" convenience, in exchange for
    # actually opening.
    pbix_path = os.path.join(project_dir, f"{app_name}.pbix")
    tables_for_compile = _tables_with_inlined_source(tables, source_data_path)
    with tempfile.TemporaryDirectory(prefix=f"{app_name}_compile_") as tmp_dir:
        tmp_sm_dir = os.path.join(tmp_dir, f"{app_name}.SemanticModel")
        write_semantic_model(
            tmp_sm_dir,
            app_name=app_name,
            tables=tables_for_compile,
            measures_by_table=measures_by_table,
            calculated_columns_by_table=calc_cols_by_table,
            hierarchies_by_table=hierarchies_by_table,
            relationships=relationships,
            roles=roles,
            parameters=parameters,
        )
        shutil.copytree(report_dir, os.path.join(tmp_dir, f"{app_name}.Report"))
        compile_pbix(tmp_dir, pbix_path)
    return pbix_path


def _tables_with_inlined_source(tables: dict[str, dict], source_data_path: str) -> dict[str, dict]:
    # M string literals only escape double quotes (as "") — backslash is a
    # literal character, not an escape introducer, so json.dumps() would be
    # wrong here (it would double every backslash in a Windows path).
    literal = '"' + source_data_path.replace('"', '""') + '"'
    # pbip-compiler has no parameter objects, so for the compiled .pbix every
    # parameter reference is swapped for a literal: SourceDataPath -> the
    # real CSV folder, and the SQL parameters -> empty/"dbo" so the dual-mode
    # query's `if SqlServer <> ""` test is false and it takes the CSV branch.
    # (SQL mode is only available in the .pbip project opened directly in
    # Power BI Desktop, where the parameters are real and editable.)
    sql_literals = {
        SQL_SERVER_PARAM_NAME: '""',
        SQL_DATABASE_PARAM_NAME: '""',
        SQL_SCHEMA_PARAM_NAME: f'"{SQL_SCHEMA_DEFAULT}"',
    }
    param_re = re.compile(
        r"\b(" + "|".join(re.escape(p) for p in
                           (SOURCE_DATA_PARAM_NAME, *sql_literals)) + r")\b"
    )
    subst = {SOURCE_DATA_PARAM_NAME: literal, **sql_literals}
    out: dict[str, dict] = {}
    for table_name, table in tables.items():
        m_expression = table.get("m_expression", "")
        if param_re.search(m_expression):
            m_expression = param_re.sub(lambda m: subst[m.group(1)], m_expression)
        out[table_name] = {**table, "m_expression": m_expression}
    return out


_DATA_TYPE_SYNONYMS = {
    "int64": "int64", "int": "int64", "integer": "int64", "whole number": "int64", "long": "int64",
    "double": "double", "number": "double", "numeric": "double", "decimal": "double",
    "float": "double", "currency": "double", "fixed decimal number": "double",
    "string": "string", "text": "string", "varchar": "string", "str": "string",
    "datetime": "dateTime", "date": "dateTime", "timestamp": "dateTime", "time": "dateTime",
    "boolean": "boolean", "bool": "boolean",
}


def _normalize_data_type(raw: str, table_name: str, field_name: str) -> str:
    """The data_model.skill.md conversion is told to only ever emit
    int64|string|double|dateTime|boolean, but an LLM won't always stick to
    that exact vocabulary — "date" instead of "dateTime", "integer" instead
    of "int64", "text"/"decimal"/"number"/"bool", etc. are all reasonable
    word choices a model might make. Both the TMDL writer and the CSV-load
    M generator match data_type by exact string, so an unrecognized value
    silently became "string" everywhere at once — with no error, since both
    layers independently fall back the same way — quietly losing a column's
    real type. Normalize once here, right where the type is first read, so
    every downstream consumer only ever sees the canonical 5 values."""
    key = (raw or "").strip().lower()
    normalized = _DATA_TYPE_SYNONYMS.get(key)
    if normalized is None:
        print(f"[build] WARNING: unrecognized data type '{raw}' for {table_name}.{field_name} "
              f"from the data model conversion — defaulting to string")
        return "string"
    return normalized


def _infer_column_type(field: dict) -> str:
    """Deterministic fallback for a field the LLM's data model conversion
    didn't classify — mirrors the tag-based rule documented in
    data_model.skill.md, computed directly from Qlik's own field tags so a
    skipped field never silently defaults to "string" (which breaks any
    SUM/AVERAGE measure over it)."""
    tags = field.get("qTags", field.get("tags", []))
    name = (field.get("qName") or field.get("name") or "").lower()
    if ("$date" in tags or "$timestamp" in tags) and "name" not in name:
        return "dateTime"
    if "$numeric" in tags:
        return "int64" if "$integer" in tags else "double"
    return "string"


_CALENDAR_FIELD_DAX = {
    "year": ("int64", "YEAR([Date])"),
    "quarter": ("int64", "QUARTER([Date])"),
    "quarternum": ("int64", "QUARTER([Date])"),
    "quarternumber": ("int64", "QUARTER([Date])"),
    "quartername": ("string", '"Q" & QUARTER([Date])'),
    "qtrname": ("string", '"Q" & QUARTER([Date])'),
    "yearquarter": ("string", 'FORMAT([Date], "YYYY") & "-Q" & QUARTER([Date])'),
    "quarteryear": ("string", '"Q" & QUARTER([Date]) & " " & YEAR([Date])'),
    "month": ("int64", "MONTH([Date])"),
    "monthnum": ("int64", "MONTH([Date])"),
    "monthnumber": ("int64", "MONTH([Date])"),
    "monthname": ("string", 'FORMAT([Date], "MMMM")'),
    "monthnameshort": ("string", 'FORMAT([Date], "MMM")'),
    "monthshort": ("string", 'FORMAT([Date], "MMM")'),
    "monabbr": ("string", 'FORMAT([Date], "MMM")'),
    "monthabbr": ("string", 'FORMAT([Date], "MMM")'),
    "monthyear": ("string", 'FORMAT([Date], "MMM YYYY")'),
    "yearmonth": ("string", 'FORMAT([Date], "MMM YYYY")'),
    "yearmonthnum": ("int64", "YEAR([Date])*100+MONTH([Date])"),
    "week": ("int64", "WEEKNUM([Date])"),
    "weeknum": ("int64", "WEEKNUM([Date])"),
    "weeknumber": ("int64", "WEEKNUM([Date])"),
    "weekday": ("int64", "WEEKDAY([Date])"),
    "dayofweek": ("int64", "WEEKDAY([Date])"),
    "weekdayname": ("string", 'FORMAT([Date], "dddd")'),
    "dayname": ("string", 'FORMAT([Date], "dddd")'),
    "day": ("int64", "DAY([Date])"),
    "dayofmonth": ("int64", "DAY([Date])"),
    "dayofyear": ("int64", "DATEDIFF(DATE(YEAR([Date]),1,1),[Date],DAY)+1"),
    "monthno": ("int64", "MONTH([Date])"),
    "quarterno": ("int64", "QUARTER([Date])"),
    "weekno": ("int64", "WEEKNUM([Date])"),
    "weekyear": ("string", '"W" & WEEKNUM([Date]) & " " & YEAR([Date])'),
    "monthstart": ("dateTime", "DATE(YEAR([Date]), MONTH([Date]), 1)"),
    "monthend": ("dateTime", "EOMONTH([Date], 0)"),
    "quarterstart": ("dateTime", "DATE(YEAR([Date]), (QUARTER([Date]) - 1) * 3 + 1, 1)"),
    "quarterend": ("dateTime", "EOMONTH(DATE(YEAR([Date]), QUARTER([Date]) * 3, 1), 0)"),
    "yearstart": ("dateTime", "DATE(YEAR([Date]), 1, 1)"),
    "yearend": ("dateTime", "DATE(YEAR([Date]), 12, 31)"),
    "todayflag": ("boolean", "[Date] = TODAY()"),
    "yearflag": ("boolean", "YEAR([Date]) = YEAR(TODAY())"),
    "currentmonthflag": ("boolean", "YEAR([Date]) = YEAR(TODAY()) && MONTH([Date]) = MONTH(TODAY())"),
}


def _normalize_calendar_field(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _detect_master_calendar_tables(raw_data_model: dict) -> set[str]:
    """A Qlik "master calendar" (almost always literally named that, or
    'Calendar'/'MasterCalendar'/a 'Dim...Calendar' variant — the standard
    idiom for an AutoCalendar()-built date table) is pure date-range
    generation logic, not real source data — it belongs in the model as a
    DAX calculated table (created "from the frontend": the same result a
    person gets from Power BI Desktop's Modeling > New Table > DAX formula),
    not loaded via Power Query from an exported CSV the way every other
    table is."""
    names = set()
    for t in raw_data_model.get("tables", []):
        name = t.get("qName") or t.get("name") or ""
        if "calendar" in name.lower():
            names.add(name)
    return names


def _build_calendar_table(table_name: str, raw_fields: list[dict]) -> dict | None:
    """Build a DAX calculated-table equivalent of a Qlik master calendar:
    CALENDARAUTO() gives the base date range (spanning every date/datetime
    column in the model, exactly like Qlik's own AutoCalendar()), extended
    via ADDCOLUMNS with one column per recognized calendar field pattern.
    Every derived column keeps the table's own ORIGINAL Qlik field name
    (never invented) — a field whose name doesn't match any recognized
    calendar pattern is left out entirely (warned about) rather than
    guessing a formula for it."""
    field_names = [f.get("qName") or f.get("name") for f in raw_fields]
    field_names = [f for f in field_names if f]

    date_field = next((f for f in field_names if _normalize_calendar_field(f) == "date"), None)
    if not date_field:
        print(f"[build] WARNING: '{table_name}' looks like a master calendar table but has no field "
              f"named 'Date' — building it as a normal CSV-loaded table instead")
        return None

    # CALENDARAUTO()'s own output column is always named "Date"; sourceColumn
    # maps that fixed name back to whatever the real Qlik field was actually
    # called (almost always "Date" too, but preserved either way).
    columns = [{"name": date_field, "data_type": "dateTime", "source_column": "Date"}]
    addcolumns_parts = []
    for fname in field_names:
        if fname == date_field:
            continue
        mapping = _CALENDAR_FIELD_DAX.get(_normalize_calendar_field(fname))
        if not mapping:
            print(f"[build] WARNING: calendar table '{table_name}' field '{fname}' doesn't match any "
                  f"recognized calendar field pattern — omitting it rather than guessing its formula "
                  f"(add it by hand in Power BI Desktop if you need it)")
            continue
        dtype, dax = mapping
        columns.append({"name": fname, "data_type": dtype, "source_column": fname})
        addcolumns_parts.append(f'"{fname}", {dax}')

    if addcolumns_parts:
        dax_expression = "ADDCOLUMNS(\n    CALENDARAUTO(),\n    " + ",\n    ".join(addcolumns_parts) + "\n)"
    else:
        dax_expression = "CALENDARAUTO()"

    print(f"[build] '{table_name}' detected as a master calendar table — building it as a DAX "
          f"calculated table (CALENDARAUTO()), created \"from the frontend\" the same way Modeling > "
          f"New Table would, instead of loading it from CSV. Real when the .pbip project is opened "
          f"directly in Power BI Desktop; pbip-compiler can't represent a calculated table at all, so "
          f"the compiled .pbix will only show a placeholder row for '{table_name}'.")

    return {"columns": columns, "m_expression": "", "is_calculated": True, "dax_expression": dax_expression}


def _assemble_semantic_inputs(extracted_dir: str, converted_dir: str):
    raw_data_model = _load_json(extracted_dir, "data_model.json")
    converted_data_model = _load_json(converted_dir, "data_model.converted.json")
    column_types = converted_data_model.get("column_types", {})
    calendar_table_names = _detect_master_calendar_tables(raw_data_model)

    tables: dict[str, dict] = {}
    for raw_table in raw_data_model.get("tables", []):
        table_name = raw_table.get("qName") or raw_table.get("name")
        if not table_name:
            continue
        raw_fields = raw_table.get("qFields", raw_table.get("fields", []))

        if table_name in calendar_table_names:
            calendar_table = _build_calendar_table(table_name, raw_fields)
            if calendar_table:
                tables[table_name] = calendar_table
                continue

        table_col_types = column_types.get(table_name, {})
        columns = []
        skipped_by_llm = 0
        for f in raw_fields:
            fname = f.get("qName") or f.get("name")
            if not fname:
                continue
            if fname in table_col_types:
                dtype = _normalize_data_type(table_col_types[fname], table_name, fname)
            else:
                # data_model.skill.md occasionally only classifies a subset
                # of a table's fields for a large data model (seen: 7 of 25
                # fields returned for one table) — defaulting an unclassified
                # field to "string" silently breaks any measure that SUMs it
                # (real value: "SUM cannot work with values of type String").
                # Fall back to the same tag-based rule the skill documents,
                # computed directly here, rather than guessing "string".
                dtype = _infer_column_type(f)
                skipped_by_llm += 1
            columns.append({"name": fname, "data_type": dtype, "source_column": fname})
        if skipped_by_llm:
            print(f"[build] {table_name}: {skipped_by_llm} column(s) not classified by the data model "
                  f"conversion — inferred type from Qlik field tags instead")
        csv_filename = f"{_safe(table_name)}.csv"
        csv_path = os.path.join(extracted_dir, "data", csv_filename)
        if os.path.exists(csv_path):
            # Real data extracted straight from the Qlik app takes priority
            # over the LLM's best-effort reconstruction of the original load
            # script, which usually points at a source (file path / DB) only
            # reachable from the machine that authored the .qvf. Every
            # table's M resolves its file against the shared SourceDataPath
            # parameter (see csv_m.py) rather than a literal path baked into
            # each table individually.
            m_expression = generate_partition_m(
                table_name, csv_filename, columns,
                source_ref=SOURCE_DATA_PARAM_NAME, sql=_SQL_PARTITION_REFS,
            )
        else:
            m_path = os.path.join(converted_dir, f"m_query__{_safe(table_name)}.m")
            m_expression = ""
            if os.path.exists(m_path):
                with open(m_path, encoding="utf-8") as f:
                    m_expression = f.read()
        tables[table_name] = {"columns": columns, "m_expression": m_expression}

    measures_by_table: dict[str, list[dict]] = {name: [] for name in tables}
    # Tabular measure names are matched case-insensitively, and the visual
    # JSON's Property casing isn't guaranteed to match the measure's stored
    # casing exactly, so key this by casefolded name; value is (table,
    # true_original_name) as recorded before dedup renamed anything.
    original_measure_table: dict[str, tuple[str, str]] = {}
    # A measure the DAX conversion couldn't assign to a real table (it
    # writes "Unknown", or names a table the extracted model doesn't have)
    # would be silently lost — write_semantic_model only iterates real
    # tables. A measure has no meaningful "home" table anyway (it's
    # referenced globally as [Name]), so redirect an orphan onto the first
    # real table rather than drop it: the visual then binds to a real
    # measure, and any error surfaces on the measure's own DAX (actionable)
    # instead of as "field not found" on every visual that used it.
    _fallback_measure_table = next(iter(tables), "")

    def _real_table(t: str) -> str:
        if t in tables:
            return t
        if _fallback_measure_table:
            print(f"[build] measure home table '{t or '(none)'}' isn't a real table — "
                  f"placing measure on '{_fallback_measure_table}' instead")
        return _fallback_measure_table

    for m in _load_json(converted_dir, "measures.converted.json").get("measures", []):
        table = _real_table(m.get("table", ""))
        m = {**m, "table": table}
        measures_by_table.setdefault(table, []).append(m)
        original_measure_table[m["name"].casefold()] = (table, m["name"])

    for v in _load_json(converted_dir, "variables.converted.json").get("variables", []):
        if v.get("target") == "dax_measure" and v.get("dax") and v["dax"].get("expression"):
            table = _real_table(v.get("table", ""))
            measures_by_table.setdefault(table, []).append({
                "name": v["name"], "expression": v["dax"]["expression"], "is_hidden": True,
            })
            original_measure_table[v["name"].casefold()] = (table, v["name"])
        elif v.get("target") == "dax_measure" and v.get("dax") and not v["dax"].get("expression"):
            # parameters_variables.skill.md deliberately emits expression:
            # null for a variable that's a set-analysis FRAGMENT meant to be
            # inlined into other measures, not a standalone measure — it has
            # no valid DAX of its own. Nothing to build; note it so a person
            # knows this variable's logic needs manual inlining wherever it
            # was referenced.
            print(f"[build] variable '{v['name']}' is a set-analysis fragment (no standalone expression) — "
                  f"not creating a measure; inline its logic manually wherever it's referenced. "
                  f"{v.get('notes', '')}")

    calc_cols_by_table: dict[str, list[dict]] = {name: [] for name in tables}
    hierarchies_by_table: dict[str, list[dict]] = {name: [] for name in tables}
    for item in _load_json(converted_dir, "dimensions.converted.json").get("items", []):
        table = item.get("table", "")
        if item.get("type") == "calculated_column":
            calc_cols_by_table.setdefault(table, []).append(item)
        elif item.get("type") == "hierarchy":
            hierarchies_by_table.setdefault(table, []).append(item)
        elif item.get("type") == "measure" and item.get("expression"):
            # dax_columns_hierarchies.skill.md emits this instead of a
            # calculated column whenever a Qlik dimension's expression is
            # actually an aggregation (Sum/Count/Avg/...) — baking an
            # aggregation into a calculated column would freeze it at
            # refresh-time instead of responding to filters like a real
            # measure does, so it belongs here, not in calc_cols_by_table.
            measures_by_table.setdefault(table, []).append({
                "name": item["name"], "expression": item["expression"],
                "format_string": item.get("format_string"), "is_hidden": item.get("is_hidden", False),
            })
            original_measure_table[item["name"].casefold()] = (table, item["name"])

    return tables, measures_by_table, calc_cols_by_table, hierarchies_by_table, original_measure_table


# Aggregators that HARD-ERROR in DAX on a text column (unlike MIN/MAX/COUNT,
# which tolerate text). If one of these wraps a bare Table[Column], that
# column must be numeric or the whole report breaks.
_NUMERIC_AGG_OVER_COLUMN_RE = re.compile(
    r"\b(SUM|AVERAGE|MEDIAN|PRODUCT|GEOMEAN|STDEV\.[PS]|VAR\.[PS])\s*\(\s*"
    r"(?:'([^']+)'|([A-Za-z_]\w*))\s*\[\s*([^\]]+?)\s*\]\s*\)",
    re.IGNORECASE,
)


def _reconcile_column_types(
    tables: dict[str, dict],
    measures_by_table: dict[str, list[dict]],
    calc_cols_by_table: dict[str, list[dict]],
    extracted_dir: str,
) -> None:
    """Make each base column's type consistent with how measures aggregate
    it. Runs after every measure source has been merged in, just before the
    semantic model is written.

    For every `SUM/AVERAGE/... ( Table[Column] )` found in any measure or
    calculated-column expression where Column is a real base column the model
    currently types as text:
      * if the extracted CSV proves the column is numeric (or is entirely
        empty, so nothing contradicts the measure's intent) -> promote the
        column to int64/double AND regenerate its Power Query partition so
        the CSV load coerces it the same way;
      * if the CSV has genuine non-numeric values -> leave the column as text
        and rewrite that expression to BLANK() with a TODO, so the report
        still opens instead of erroring on every visual of the page.
    """
    data_dir = os.path.join(extracted_dir, "data")

    col_index: dict[tuple[str, str], dict] = {}
    for tname, table in tables.items():
        for col in table.get("columns", []):
            col_index[(tname.casefold(), col["name"].casefold())] = col
    real_measure_names = {
        m["name"].casefold()
        for ms in measures_by_table.values() for m in ms
    }

    numeric_cache: dict[tuple[str, str], str | None] = {}

    def classify(tname: str, cname: str) -> str | None:
        """'int64' / 'double' if the CSV column is numeric or empty; None if
        it holds real non-numeric text; 'missing' if there's no CSV."""
        key = (tname, cname)
        if key in numeric_cache:
            return numeric_cache[key]
        csv_path = os.path.join(data_dir, f"{_safe(tname)}.csv")
        if not os.path.exists(csv_path):
            numeric_cache[key] = "missing"
            return "missing"
        real_name, values = None, []
        try:
            with open(csv_path, encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                for fn in reader.fieldnames or []:
                    if fn.casefold() == cname.casefold():
                        real_name = fn
                        break
                if real_name is None:
                    numeric_cache[key] = "missing"
                    return "missing"
                for r in reader:
                    v = (r.get(real_name) or "").strip()
                    if v:
                        values.append(v)
        except OSError:
            numeric_cache[key] = "missing"
            return "missing"

        result: str | None
        if not values:
            result = "double"  # empty column: trust the measure, don't block the report
        else:
            all_num = all_int = True
            for v in values:
                try:
                    f = float(v.replace(",", ""))
                except ValueError:
                    all_num = False
                    break
                if f != int(f):
                    all_int = False
            result = ("int64" if all_int else "double") if all_num else None
        numeric_cache[key] = result
        return result

    changed_tables: set[str] = set()

    def process(expr: str, owner: str) -> str:
        if not expr:
            return expr
        for m in list(_NUMERIC_AGG_OVER_COLUMN_RE.finditer(expr)):
            agg = m.group(1).upper()
            tref = (m.group(2) or m.group(3) or "")
            cref = m.group(4)
            col = col_index.get((tref.casefold(), cref.casefold()))
            if col is None:
                continue  # not a real base column (a measure ref, or phantom — other passes handle it)
            if cref.casefold() in real_measure_names:
                continue
            if col.get("data_type") not in ("string", None):
                continue  # already numeric/date — nothing to do

            verdict = classify(tref, cref)
            if verdict in ("int64", "double"):
                if col.get("data_type") != verdict:
                    print(f"[build] type reconcile: {tref}[{cref}] is aggregated by {agg}() in "
                          f"'{owner}' but was typed text -> promoting to {verdict} "
                          f"(and coercing its CSV load to match)")
                    col["data_type"] = verdict
                    changed_tables.add(tref)
            elif verdict == "missing":
                print(f"[build] type reconcile: {tref}[{cref}] is aggregated by {agg}() in '{owner}' "
                      f"but typed text and no CSV to verify — promoting to double so the report opens")
                col["data_type"] = "double"
                changed_tables.add(tref)
            else:
                print(f"[build] WARNING: {tref}[{cref}] holds non-numeric text but '{owner}' does "
                      f"{agg}() over it — rewriting that measure to BLANK() so the report still opens "
                      f"(fix the Qlik source or the conversion)")
                return f"BLANK() // TODO: {agg}({tref}[{cref}]) over a non-numeric column"
        return expr

    for ms in measures_by_table.values():
        for mrow in ms:
            mrow["expression"] = process(mrow.get("expression", ""), mrow.get("name", "?"))
    for cs in calc_cols_by_table.values():
        for crow in cs:
            crow["expression"] = process(crow.get("expression", ""), crow.get("name", "?"))

    for tname in changed_tables:
        table = tables.get(tname)
        if not table or table.get("is_calculated"):
            continue
        csv_path = os.path.join(data_dir, f"{_safe(tname)}.csv")
        if os.path.exists(csv_path) and SOURCE_DATA_PARAM_NAME in table.get("m_expression", ""):
            table["m_expression"] = generate_partition_m(
                tname, os.path.basename(csv_path), table["columns"],
                source_ref=SOURCE_DATA_PARAM_NAME, sql=_SQL_PARTITION_REFS,
            )


_TABLE_QUALIFIED_REF_RE = re.compile(r"(?:'([^']+)'|(\b[A-Za-z_]\w*\b))\[([^\[\]]+)\]")


def _fix_measure_self_references(
    measures_by_table: dict[str, list[dict]],
    calc_cols_by_table: dict[str, list[dict]],
    tables: dict[str, dict],
) -> None:
    """DAX has two different reference syntaxes and mixing them up is a
    serious, common mistake: a real column is 'Table[Column]', but a
    reference to another MEASURE is always bare '[Measure Name]' — never
    table-qualified. 'Table[Measure Name]' isn't valid alternate syntax for
    a measure reference, it's DAX for "the column named 'Measure Name' on
    Table" — which either errors outright, or worse, silently binds to an
    unrelated real column that happens to share that name. dax_measures.
    skill.md now instructs against this directly, but auto-correct any
    instance that still slips through, the same way other hallucination
    classes get corrected elsewhere in this file.

    Only rewrites a match when the bracketed name is a real measure AND
    NOT also a real column on that specific table — a genuine coincidental
    collision (rare) is left alone rather than risk breaking an intentional
    column reference.
    """
    column_owner: dict[str, set[str]] = {}
    for table_name, table in tables.items():
        for col in table.get("columns", []):
            column_owner.setdefault(col["name"].casefold(), set()).add(table_name)
    for table_name, cols in calc_cols_by_table.items():
        for col in cols:
            column_owner.setdefault(col["name"].casefold(), set()).add(table_name)

    measure_index: dict[str, str] = {}
    for measures in measures_by_table.values():
        for m in measures:
            measure_index.setdefault(m["name"].casefold(), m["name"])

    def fix_expression(table_name: str, name: str, expr: str) -> str:
        def replace(match: re.Match) -> str:
            ref_table = match.group(1) or match.group(2)
            field = match.group(3).strip()
            key = field.casefold()
            if key not in measure_index:
                return match.group(0)
            if table_name in column_owner.get(key, set()):
                return match.group(0)  # a real column by this exact name exists — ambiguous, leave alone
            fixed = f"[{measure_index[key]}]"
            if fixed != match.group(0):
                print(f"[build] fixed measure reference in '{name}': "
                      f"'{ref_table}[{field}]' -> '{fixed}' (measure, not a column)")
            return fixed

        return _TABLE_QUALIFIED_REF_RE.sub(replace, expr)

    for table_name, measures in measures_by_table.items():
        for m in measures:
            if m.get("expression"):
                m["expression"] = fix_expression(table_name, m["name"], m["expression"])
    for table_name, cols in calc_cols_by_table.items():
        for col in cols:
            if col.get("expression"):
                col["expression"] = fix_expression(table_name, col["name"], col["expression"])


def _fix_phantom_table_refs(
    measures_by_table: dict[str, list[dict]],
    calc_cols_by_table: dict[str, list[dict]],
    tables: dict[str, dict],
) -> None:
    """The DAX conversion sometimes fabricates a table-qualified reference
    `Foo[Foo]` for a Qlik field it couldn't place in the model (seen: a
    Qlik master measure `Sum({<set>}ExpediteCost)` where `ExpediteCost` is
    an orphaned field — not in the load script, not a table field, not a
    variable, not another measure — so it's genuinely unconvertible from
    the extracted model). `Foo` isn't a real table, so `Foo[Foo]` errors in
    Power BI ("can't find table Foo").

    If the bracketed column name turns out to be a real column on exactly
    one real table, repoint it there. If it's a real measure, make it a
    bare `[Name]`. Otherwise the whole expression can't be salvaged —
    replace it with BLANK() so the measure still EXISTS (visuals binding to
    it resolve and just show blank) rather than erroring, and warn loudly
    with the field name so a person can wire it up by hand."""
    real_tables = set(tables)
    col_owner: dict[str, set[str]] = {}
    for tn, t in tables.items():
        for c in t.get("columns", []):
            col_owner.setdefault(c["name"].casefold(), set()).add(tn)
    for tn, cols in calc_cols_by_table.items():
        for c in cols:
            col_owner.setdefault(c["name"].casefold(), set()).add(tn)
    measure_names: dict[str, str] = {}
    for ms in measures_by_table.values():
        for m in ms:
            measure_names.setdefault(m["name"].casefold(), m["name"])

    def fix(kind: str, name: str, expr: str) -> str:
        unresolved: list[str] = []

        def repl(match: re.Match) -> str:
            ref_table = match.group(1) or match.group(2)
            col = match.group(3).strip()
            if ref_table in real_tables:
                return match.group(0)
            owners = col_owner.get(col.casefold())
            if owners and len(owners) == 1:
                real = next(iter(owners))
                print(f"[build] {kind} '{name}': repointed '{ref_table}[{col}]' -> '{real}'[{col}] "
                      f"('{ref_table}' is not a real table)")
                return f"'{real}'[{col}]"
            if col.casefold() in measure_names:
                print(f"[build] {kind} '{name}': '{ref_table}[{col}]' -> [{measure_names[col.casefold()]}] "
                      f"(measure reference, '{ref_table}' is not a real table)")
                return f"[{measure_names[col.casefold()]}]"
            unresolved.append(f"{ref_table}[{col}]")
            return match.group(0)

        new_expr = _TABLE_QUALIFIED_REF_RE.sub(repl, expr)
        if unresolved:
            print(f"[build] WARNING: {kind} '{name}' references {', '.join(sorted(set(unresolved)))} "
                  f"— not a table/column/measure anywhere in the extracted model (an orphaned reference "
                  f"in the source Qlik app). Replaced its expression with BLANK(); wire it up by hand "
                  f"in Power BI Desktop if the underlying data is available there.")
            return "BLANK()"
        return new_expr

    for ms in measures_by_table.values():
        for m in ms:
            if m.get("expression"):
                m["expression"] = fix("measure", m["name"], m["expression"])
    for cols in calc_cols_by_table.values():
        for c in cols:
            if c.get("expression"):
                c["expression"] = fix("calculated column", c["name"], c["expression"])


def _dedupe_measure_names(measures_by_table: dict[str, list[dict]]) -> dict[tuple[str, str], str]:
    """DAX measure names must be unique across the whole model, unlike Qlik
    master measures which only need to be unique per-visual — and Tabular
    model object names are compared case-insensitively, so 'Foo' and 'FOO'
    collide too even though they're different Python strings. Rename any
    collision (keeping the first occurrence untouched) by suffixing the
    owning table name, so Power BI Desktop doesn't reject the .pbix. Returns
    a {(table, original_name): new_name} map for every renamed measure, so
    report visuals bound to the old name can be repointed."""
    seen: set[str] = set()
    renamed: dict[tuple[str, str], str] = {}
    for table_name, measures in measures_by_table.items():
        for m in measures:
            original = m["name"]
            name = original
            n = 1
            while name.casefold() in seen:
                n += 1
                name = f"{original} ({table_name})" if n == 2 else f"{original} ({table_name} {n})"
            if name != original:
                print(f"[build] renamed duplicate measure '{original}' -> '{name}' on table '{table_name}'")
                m["name"] = name
                renamed[(table_name, original)] = name
            seen.add(name.casefold())
    return renamed


_BARE_MEASURE_REF_RE = re.compile(r"(?<![\w'])\[([^\[\]]+)\]")


def _rewrite_renamed_measure_refs(
    measures_by_table: dict[str, list[dict]],
    calc_cols_by_table: dict[str, list[dict]],
    renamed: dict[tuple[str, str], str],
) -> None:
    """_dedupe_measure_names renames a colliding measure but never touches
    any OTHER measure's DAX that references it — a bare '[Old Name]' measure
    reference elsewhere in the model still names the pre-rename measure.
    Left alone, that reference doesn't just go stale: DAX/Tabular resolves
    measure names case-insensitively, so if the rename only changed casing
    (e.g. Qlik had both 'Dispute Count (90d)' and 'Dispute Count (90D)' as
    distinct titles — legal in Qlik, not in DAX), the untouched bracket
    reference can end up matching the WRONG measure — even the one that
    contains it — producing a circular-dependency error in Power BI Desktop
    instead of a missing-field one. Rewrite every bare bracket reference to
    a renamed measure, in every measure and calculated column's expression,
    right after dedup renames it.

    The lookup below matches EXACT case, not casefold. That matters
    specifically for a case-only collision like the 'Dispute Count (90d)'
    vs 'Dispute Count (90D)' example above: once dedup renames the second
    one, a casefold-keyed lookup can no longer tell '[Dispute Count (90d)]'
    written inside the RENAMED measure's own formula (referencing the
    OTHER, un-renamed measure — correct, should stay untouched) apart from
    a reference to the renamed measure's own pre-rename name (which should
    redirect) — both casefold to the same key, so a casefold lookup
    redirects the first case too, making the renamed measure reference
    itself. Every DAX expression this pipeline generates is required to
    preserve a Qlik name's exact original casing (see each skill.md's
    'Critical: preserve exact names'), so an exact-case lookup is both
    sufficient and unambiguous here — no case-insensitive fallback needed."""
    if not renamed:
        return
    rename_lookup: dict[str, str] = {}
    for (_table, original), new_name in renamed.items():
        rename_lookup.setdefault(original, new_name)

    def _sub(expr: str) -> str:
        def repl(match: re.Match) -> str:
            new_name = rename_lookup.get(match.group(1))
            return f"[{new_name}]" if new_name else match.group(0)
        return _BARE_MEASURE_REF_RE.sub(repl, expr)

    for measures in measures_by_table.values():
        for m in measures:
            if m.get("expression"):
                m["expression"] = _sub(m["expression"])
    for cols in calc_cols_by_table.values():
        for c in cols:
            if c.get("expression"):
                c["expression"] = _sub(c["expression"])


_AGG_RE = re.compile(r"^\s*(Sum|Count|Avg|Min|Max)\s*\(\s*\[?([\w .]+?)\]?\s*\)\s*$", re.IGNORECASE)
_DAX_AGG_FUNC = {"SUM": "SUM", "COUNT": "COUNT", "AVG": "AVERAGE", "MIN": "MIN", "MAX": "MAX"}


def _build_sheet_label_lookups(extracted_dir: str) -> tuple[dict[str, tuple[str, str]], dict[str, str]]:
    """Scan the raw extracted sheets.json for every chart object's own
    hypercube definition (ground truth: real field names + expressions),
    building two label -> real-thing lookups used to recover a sheet-local
    KPI/dimension the report_visuals conversion only captured by display
    label: label_to_agg maps a measure's label to (FUNCTION, real_field);
    label_to_field maps a dimension's label to its real field name."""
    sheets_path = os.path.join(extracted_dir, "sheets.json")
    if not os.path.exists(sheets_path):
        return {}, {}
    with open(sheets_path, encoding="utf-8") as f:
        sheets = json.load(f)

    label_to_agg: dict[str, tuple[str, str]] = {}
    label_to_field: dict[str, str] = {}

    def walk(node):
        if isinstance(node, dict):
            hc = node.get("qHyperCubeDef")
            if isinstance(hc, dict):
                for m in hc.get("qMeasures") or []:
                    qdef = (m or {}).get("qDef") or {}
                    expr, label = qdef.get("qDef"), qdef.get("qLabel")
                    if expr and label:
                        match = _AGG_RE.match(expr)
                        if match:
                            label_to_agg.setdefault(label.strip().casefold(), (match.group(1).upper(), match.group(2).strip()))
                for d in hc.get("qDimensions") or []:
                    qdef = (d or {}).get("qDef") or {}
                    field_defs = qdef.get("qFieldDefs") or []
                    labels = qdef.get("qFieldLabels") or []
                    if field_defs:
                        field = str(field_defs[0]).lstrip("=").strip("[]").strip()
                        # A calculated dimension (e.g. an Aggr()/If() expression
                        # rather than a plain field) isn't a real column we can
                        # point Power BI at — skip it rather than passing the
                        # raw expression text through as a fake "field name".
                        if "(" in field or " " in field:
                            continue
                        label = (labels[0] if labels else field).strip()
                        if field:
                            label_to_field.setdefault(label.casefold(), field)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(sheets)

    # A chart can reference a master dimension by its library id instead of
    # repeating qFieldDefs inline — dimensions.json (from GetDimensionList)
    # always has the real field regardless of how any one chart refers to
    # it, so fold those in too rather than relying only on inline defs.
    dimensions_path = os.path.join(extracted_dir, "dimensions.json")
    if os.path.exists(dimensions_path):
        with open(dimensions_path, encoding="utf-8") as f:
            dimensions = json.load(f)
        for dim in dimensions:
            if dim.get("grouping") != "N":
                continue
            field_defs = dim.get("field_defs") or []
            if not field_defs:
                continue
            field = str(field_defs[0]).lstrip("=").strip("[]").strip()
            if "(" in field or " " in field:
                continue
            for label in [dim.get("title")] + (dim.get("field_labels") or []):
                if label:
                    label_to_field.setdefault(label.strip().casefold(), field)

    return label_to_agg, label_to_field


def _apply_what_if_parameters(
    extracted_dir: str,
    tables: dict[str, dict],
    measures_by_table: dict[str, list[dict]],
    original_measure_table: dict[str, tuple[str, str]],
) -> dict[tuple[str, str], str]:
    """Returns a rename map, same shape as _dedupe_measure_names's, so the
    caller can feed it into _rewrite_renamed_measure_refs: the what-if
    measure's real name is the slider's display label ("Sales Achievement
    %"), never the raw Qlik variable name ("vSalesAchievement%") — but a
    master/ad-hoc measure's own DAX can reference this variable by EITHER
    text (dax_measures.skill.md is told to preserve $(vVarName) substitution
    verbatim, i.e. as the raw name). original_measure_table below makes
    either bracket text resolve correctly for report-VISUAL field binding,
    but that lookup is never consulted for a bracket reference sitting
    inside ANOTHER measure's own DAX expression — so a bare '[vSalesAchievement%]'
    left as-is inside some other measure's formula stays literally
    unresolvable DAX ("the value ... cannot be determined") even though the
    real measure exists under its real name. The caller must rewrite it."""
    renamed: dict[tuple[str, str], str] = {}
    for param in detect_what_if_parameters(extracted_dir):
        table_name = param["label"]
        if table_name in tables:
            print(f"[build] WARNING: what-if parameter '{table_name}' collides with an existing table name — skipping")
            continue

        tables[table_name] = {
            # The column can't share its name with the measure below (a
            # Tabular table can't have a column and a measure with the same
            # name) — "Value" for the column, the slider's own label for the
            # measure that other DAX expressions actually reference.
            "columns": [{"name": "Value", "data_type": "double", "source_column": "Value"}],
            "m_expression": generate_range_m(param["min"], param["max"], param["step"]),
        }
        expr = f"SELECTEDVALUE('{table_name}'[Value], {param['default']})"
        measures_by_table.setdefault(table_name, []).append({"name": table_name, "expression": expr})
        # Register under BOTH the slider's display label ("Sales Achievement
        # %") and the raw Qlik variable name ("vSalesAchievement%") — a
        # measure/adhoc-expression conversion may reference this variable
        # either way ($(vSalesAchievement%) substitution should preserve the
        # raw name per dax_measures.skill.md, but the label is also common),
        # and either bracket reference needs to resolve to this same measure.
        original_measure_table[table_name.casefold()] = (table_name, table_name)
        original_measure_table[param["variable"].casefold()] = (table_name, table_name)
        if param["variable"] != table_name:
            renamed[(table_name, param["variable"])] = table_name
        print(f"[build] added what-if parameter '{table_name}' (range {param['min']}-{param['max']} "
              f"step {param['step']}, default {param['default']}) from Qlik variable '{param['variable']}'")
    return renamed


def _apply_adhoc_expressions(
    converted_dir: str,
    tables: dict[str, dict],
    measures_by_table: dict[str, list[dict]],
    calc_cols_by_table: dict[str, list[dict]],
    original_measure_table: dict[str, tuple[str, str]],
    label_to_field: dict[str, str],
) -> None:
    data = _load_json(converted_dir, "adhoc_expressions.converted.json")
    if not data:
        return

    def _register_source_alias(src: str | None, target: tuple[str, str]) -> None:
        # report_visuals often binds a visual to the RAW Qlik expression
        # text (e.g. "Sum(OutstandingAmount)", "Count({<...>} DISTINCT
        # InvoiceID)", "='W'&PredictedCollectionWeek") rather than to the
        # name this ad-hoc pass gave the converted item — register the raw
        # source as an alias so _fix_field_and_measure_refs still resolves
        # it. converters._attach_qlik_source puts it on `qlik_source`.
        if src and src.strip():
            original_measure_table.setdefault(src.strip().casefold(), target)

    for m in data.get("measures", []):
        table, name, expr = m.get("table"), m.get("name"), m.get("expression")
        if not (table and name and expr and table in tables):
            continue
        measures_by_table.setdefault(table, []).append({
            "name": name, "expression": expr,
            "format_string": m.get("format_string"), "is_hidden": m.get("is_hidden", False),
        })
        original_measure_table[name.casefold()] = (table, name)
        _register_source_alias(m.get("qlik_source"), (table, name))
        print(f"[build] converted ad-hoc chart expression -> measure '{name}' on '{table}'")

    for item in data.get("items", []):
        table, name = item.get("table"), item.get("name")
        if not (table and name and table in tables):
            continue
        if item.get("type") == "calculated_column" and item.get("expression"):
            calc_cols_by_table.setdefault(table, []).append({"name": name, "expression": item["expression"]})
            label_to_field.setdefault(name.casefold(), name)
            src = item.get("qlik_source")
            if src and src.strip():
                label_to_field.setdefault(src.strip().casefold(), name)
            print(f"[build] converted ad-hoc chart expression -> calculated column '{name}' on '{table}'")
        elif item.get("type") == "measure" and item.get("expression"):
            # Same rule as the master-dimension path in _assemble_semantic_inputs:
            # an ad-hoc chart dimension expression that's actually an
            # aggregation (Sum/Count/Avg/...) must become a real measure, not
            # a calculated column with the aggregation frozen in at refresh
            # time. dax_columns_hierarchies.skill.md emits "measure" for it.
            measures_by_table.setdefault(table, []).append({
                "name": name, "expression": item["expression"],
                "format_string": item.get("format_string"), "is_hidden": item.get("is_hidden", False),
            })
            original_measure_table[name.casefold()] = (table, name)
            _register_source_alias(item.get("qlik_source"), (table, name))
            print(f"[build] converted ad-hoc chart expression -> measure '{name}' on '{table}' (aggregation)")


def _synthesize_adhoc_measures(
    pages: list[dict],
    tables: dict[str, dict],
    measures_by_table: dict[str, list[dict]],
    original_measure_table: dict[str, tuple[str, str]],
    label_to_agg: dict[str, tuple[str, str]],
) -> None:
    """Add a real DAX measure for every visual field reference that names a
    sheet-local KPI label (not a real column, not an existing master
    measure) recognized in label_to_agg, so it can bind the same way any
    master measure does instead of pointing at a nonexistent field."""
    column_owner: dict[str, str] = {}
    for table_name, table in tables.items():
        for col in table["columns"]:
            column_owner.setdefault(col["name"].casefold(), table_name)

    added: set[str] = set()

    def _maybe_synthesize(prop: str) -> None:
        key = (prop or "").casefold()
        if not key or key in column_owner or key in original_measure_table or key not in label_to_agg:
            return
        func, field = label_to_agg[key]
        owner = column_owner.get(field.casefold())
        if owner and key not in added:
            expr = f"{_DAX_AGG_FUNC.get(func, 'SUM')}('{owner}'[{field}])"
            measures_by_table.setdefault(owner, []).append({"name": prop, "expression": expr})
            original_measure_table[key] = (owner, prop)
            added.add(key)
            print(f"[build] synthesized measure '{prop}' = {expr} (sheet-local KPI not in master measures)")

    def _walk(node):
        if isinstance(node, dict):
            # The report_visuals conversion sometimes binds a sheet-local KPI
            # expression as a "Column" (since it isn't a real field) and
            # sometimes correctly guesses "Measure" (since it isn't a real
            # measure either, this fails resolve_measure downstream) — check
            # both shapes the same way.
            column = node.get("Column")
            if isinstance(column, dict):
                _maybe_synthesize(column.get("Property"))
            measure = node.get("Measure")
            if isinstance(measure, dict):
                _maybe_synthesize(measure.get("Property"))
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for v in node:
                _walk(v)

    for page in pages:
        _walk(page)


def _normalize_label(s: str) -> str:
    """Strip everything but letters/digits and lowercase, so minor
    transcription drift between two independent LLM calls over the same
    source label (a dropped period, doubled space, etc.) still matches."""
    return re.sub(r"[^a-z0-9]", "", s.casefold())


_AGG_FILLER_WORDS = ("countdistinct", "distinctcount", "count", "distinct",
                     "sumof", "sum", "totalof", "total", "avgof", "avg",
                     "averageof", "average", "of", "the", "value", "amount")


def _normalize_measure_label_aggressive(s: str) -> str:
    """Beyond _normalize_label: also drop leading/trailing aggregation
    filler words, so two independently-invented names for the same
    computed value collapse together ("CountDistinctInvoiceID" and "Count
    of InvoiceID" both -> "invoiceid"). Deliberately lossy — only used as a
    last-resort measure-name match."""
    t = _normalize_label(s)
    changed = True
    while changed and t:
        changed = False
        for w in _AGG_FILLER_WORDS:
            if t.startswith(w) and len(t) > len(w):
                t = t[len(w):]
                changed = True
            if t.endswith(w) and len(t) > len(w):
                t = t[: -len(w)]
                changed = True
    return t


def _build_kpi_object_titles(extracted_dir: str) -> dict[str, str]:
    """Map a Qlik 'kpi'-type object's own id to its real 'title' property.

    The modern Qlik "kpi" extension often has NO qLabel/qFallbackTitle at
    all on its own measure (see _collect_adhoc_expressions in
    converters.py) — the object's own 'title' is the only real display name
    that exists for it. report_visuals sometimes overlooks that title
    entirely and fabricates a plausible-sounding Property/queryRef text
    from the expression's CONTENT instead (observed: a discount-calculating
    KPI titled 'DISCOUNT COST' bound to the invented name
    'Total Discount Amount', which is not a real field, column, or measure
    anywhere — a hallucination distinct from the 'used a display label
    instead of the real field name' class the rest of this file already
    corrects, since there's no real label to have used at all here).
    _fix_field_and_measure_refs falls back to this map, by the visual's own
    object id, as a last resort when nothing else resolves — the id is
    ground truth regardless of what text report_visuals invented for it."""
    sheets_path = os.path.join(extracted_dir, "sheets.json")
    if not os.path.exists(sheets_path):
        return {}
    with open(sheets_path, encoding="utf-8") as f:
        sheets = json.load(f)

    titles: dict[str, str] = {}

    def walk(node):
        if isinstance(node, dict):
            obj_id = node.get("id")
            layout = node.get("layout", {})
            props = layout.get("properties", {}) if isinstance(layout, dict) else {}
            if obj_id and props.get("visualization") == "kpi":
                title = props.get("title")
                if isinstance(title, str) and title.strip():
                    titles[obj_id] = title.strip()
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(sheets)
    return titles


def _fix_field_and_measure_refs(
    pages: list[dict],
    tables: dict[str, dict],
    calc_cols_by_table: dict[str, list[dict]],
    original_measure_table: dict[str, tuple[str, str]],
    rename_map: dict[tuple[str, str], str],
    label_to_field: dict[str, str],
    kpi_object_titles: dict[str, str] | None = None,
) -> None:
    """The report_visuals LLM call converts each sheet independently and can
    hallucinate a plausible-looking but nonexistent table name for a field's
    SourceRef.Entity (seen in practice: "Measures", "Month"). Power BI then
    refuses to open the visual ("Fields that need to be fixed"). Since the
    real owning table for every measure/column is already known from the
    other converters, force every SourceRef.Entity to the true table instead
    of trusting the LLM's guess, and repoint measure Property names that
    _dedupe_measure_names renamed for uniqueness.

    column_owner maps a field/calculated-column name to the (single) table
    that owns it; when a name exists on more than one table the LLM's own
    guess is kept if it names one of the real candidates, else the first
    match wins and a warning is printed (query results may need a manual fix
    in that rare ambiguous case).
    """
    kpi_object_titles = kpi_object_titles or {}
    # report_visuals is a per-sheet LLM call and its exact JSON shape for a
    # field binding drifts between runs: sometimes Property sits as a sibling
    # of Expression (canonical PBIR), sometimes it's nested INSIDE Expression
    # next to SourceRef. Every resolver/reader below (and pbip-compiler) only
    # looks at the sibling position, so the nested variant makes EVERY field
    # on the page read as Property=None -> "measure 'None' not found" -> the
    # projection gets pruned -> the visual renders empty ("Select or drag
    # fields to populate this visual"). Normalise to the canonical shape once,
    # up front, so the drift can't silently gut a page again.
    _normalize_field_node_shapes(pages)
    column_owner: dict[str, list[str]] = {}
    column_real_name: dict[str, str] = {}
    for table_name, table in tables.items():
        for col in table["columns"]:
            key = col["name"].casefold()
            column_owner.setdefault(key, []).append(table_name)
            column_real_name.setdefault(key, col["name"])
    for table_name, cols in calc_cols_by_table.items():
        for col in cols:
            key = col["name"].casefold()
            column_owner.setdefault(key, []).append(table_name)
            column_real_name.setdefault(key, col["name"])
    normalized_column_index: dict[str, str] = {}
    for key in column_owner:
        normalized_column_index.setdefault(_normalize_label(key), key)

    def resolve_column(prop: str, guessed: str) -> tuple[str, str] | None:
        """Returns (entity, real_column_name) or None. Resolves via exact
        casefold match first, then a punctuation/whitespace-stripped
        fallback — this bridges report_visuals binding a chart's display
        LABEL (e.g. 'Predicted Collection Week') instead of the real
        script-defined field name ('PredictedCollectionWeek'). The skill is
        now told not to do this going forward, but the fix has to live here
        too: any Property that only resolves through the normalized
        fallback must be rewritten to the real column name, not just have
        its Entity corrected — leaving Property as the display label still
        produces 'fields that need to be fixed' even once Entity is right,
        since Property has to be a real, queryable field name."""
        if not prop:
            return None
        key = prop.casefold()
        owners = column_owner.get(key)
        real_name = column_real_name.get(key)
        if not owners:
            fallback_key = normalized_column_index.get(_normalize_label(key))
            if fallback_key is not None:
                owners = column_owner.get(fallback_key)
                real_name = column_real_name.get(fallback_key)
        if not owners:
            return None
        entity = guessed if guessed in owners else owners[0]
        if len(owners) > 1 and guessed not in owners:
            print(f"[build] ambiguous field '{prop}' exists on {owners}; using '{entity}'")
        return entity, real_name

    # Independent LLM calls transcribing the same raw Qlik label can drift
    # slightly (e.g. report_visuals renders "Achv %" while the ad-hoc
    # measure conversion — a separate call over the same source text —
    # renders "Achv. %"). An exact casefold match won't bridge that; a
    # punctuation/whitespace-stripped index will, as a fallback only (exact
    # match always wins first, so this never masks a genuinely different
    # name that just happens to normalize the same).
    normalized_measure_index: dict[str, str] = {}
    aggressive_measure_index: dict[str, str] = {}
    for key in original_measure_table:
        normalized_measure_index.setdefault(_normalize_label(key), key)
        agg = _normalize_measure_label_aggressive(key)
        if agg:
            aggressive_measure_index.setdefault(agg, key)

    def resolve_measure(prop: str) -> tuple[str, str] | None:
        if not prop:
            return None
        key = prop.casefold()
        found = original_measure_table.get(key)
        if found is None:
            fallback_key = normalized_measure_index.get(_normalize_label(key))
            if fallback_key is not None:
                found = original_measure_table.get(fallback_key)
        if found is None:
            # Last resort: report_visuals and the ad-hoc conversion are
            # separate LLM calls and sometimes invent DIFFERENT names for
            # the same computed value ("CountDistinctInvoiceID" vs "Count
            # of InvoiceID"). Strip aggregation filler words too and try
            # again — only when nothing else matched, so a genuine miss
            # still surfaces as "field not found" rather than silently
            # binding to the wrong measure.
            agg = _normalize_measure_label_aggressive(key)
            if agg:
                fallback_key = aggressive_measure_index.get(agg)
                if fallback_key is not None:
                    found = original_measure_table.get(fallback_key)
        if found is None:
            return None
        table, true_original = found
        final_name = rename_map.get((table, true_original), true_original)
        return table, final_name

    def _walk(node, visual_id: str | None = None):
        if isinstance(node, dict):
            measure = node.get("Measure")
            if isinstance(measure, dict):
                resolved = resolve_measure(measure.get("Property"))
                if not resolved and visual_id in kpi_object_titles:
                    resolved = resolve_measure(kpi_object_titles[visual_id])
                if resolved:
                    entity, final_name = resolved
                    measure.setdefault("Expression", {}).setdefault("SourceRef", {})["Entity"] = entity
                    measure["Property"] = final_name
                else:
                    print(f"[build] WARNING: measure '{measure.get('Property')}' not found — visual may show 'fields that need to be fixed'")

            column = node.get("Column")
            if isinstance(column, dict):
                prop = column.get("Property")
                guessed = column.get("Expression", {}).get("SourceRef", {}).get("Entity")
                # Check for a real MEASURE by this name first, not a column.
                # report_visuals sometimes emits a "Column" node for
                # something that's actually a measure (a KPI/aggregate
                # value bound as if it were a raw field) — if that name also
                # happens to exist as (or coincidentally match) a real
                # column, resolving the column first would silently keep it
                # bound as a column and mask the real measure reference,
                # which is the dominant failure mode seen in practice
                # ("measure used in an expression ends up bound as a
                # column"). A genuine column reference is unaffected by this
                # order, since resolve_measure only returns non-None when a
                # measure by that exact (or normalized) name truly exists.
                measure_resolved = resolve_measure(prop)
                if not measure_resolved and visual_id in kpi_object_titles:
                    # Last resort, tried before falling back to a plain
                    # column: report_visuals fabricated a Property/queryRef
                    # text that matches neither a real field nor any known
                    # measure label — the visual's own Qlik object id is
                    # ground truth regardless of what text got invented for
                    # it, so retry measure resolution using that object's
                    # real title instead of giving up.
                    measure_resolved = resolve_measure(kpi_object_titles[visual_id])
                if measure_resolved:
                    del node["Column"]
                    node["Measure"] = {
                        "Expression": {"SourceRef": {"Entity": measure_resolved[0]}},
                        "Property": measure_resolved[1],
                    }
                else:
                    resolved_col = resolve_column(prop, guessed)
                    if resolved_col:
                        entity, real_name = resolved_col
                        column["Property"] = real_name
                        column.setdefault("Expression", {}).setdefault("SourceRef", {})["Entity"] = entity
                    elif prop and prop.casefold() in label_to_field:
                        real_field = label_to_field[prop.casefold()]
                        resolved_col2 = resolve_column(real_field, guessed)
                        if resolved_col2:
                            entity2, real_name2 = resolved_col2
                            column["Property"] = real_name2
                            column.setdefault("Expression", {}).setdefault("SourceRef", {})["Entity"] = entity2
                        else:
                            print(f"[build] WARNING: dimension label '{prop}' maps to field '{real_field}' but no table owns it")
                    else:
                        print(f"[build] WARNING: field '{prop}' does not match any real column or measure — visual may show 'fields that need to be fixed'")

            agg = node.get("Aggregation")
            if isinstance(agg, dict):
                inner = agg.get("Expression", {}).get("Column")
                if isinstance(inner, dict):
                    guessed = inner.get("Expression", {}).get("SourceRef", {}).get("Entity")
                    resolved_agg = resolve_column(inner.get("Property"), guessed)
                    if resolved_agg:
                        entity, real_name = resolved_agg
                        inner["Property"] = real_name
                        inner.setdefault("Expression", {}).setdefault("SourceRef", {})["Entity"] = entity

            for v in node.values():
                _walk(v, visual_id)
        elif isinstance(node, list):
            for v in node:
                _walk(v, visual_id)

    for page in pages:
        for visual in page.get("visuals", []):
            _walk(visual, visual.get("name"))

    _fix_query_refs(pages)
    _prune_unresolved_projections(pages)


def _normalize_field_node_shapes(pages: list[dict]) -> None:
    """Hoist a `Property` that the LLM nested inside `Expression` (next to
    `SourceRef`) up to its canonical position as a sibling of `Expression`,
    on every Measure/Column node anywhere in the page tree. Idempotent: a
    node that already has a sibling Property is left alone (and any stray
    nested copy removed so the two can't disagree later)."""
    def walk(node):
        if isinstance(node, dict):
            for key in ("Measure", "Column"):
                inner = node.get(key)
                if isinstance(inner, dict):
                    expr = inner.get("Expression")
                    if isinstance(expr, dict) and "Property" in expr:
                        inner.setdefault("Property", expr["Property"])
                        del expr["Property"]
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(pages)


def _entity_and_property_of_field(field: dict) -> tuple[str | None, str | None]:
    """Read back whatever Entity/Property ended up on a projection's field
    node after _walk's resolution pass, across the 3 shapes a projection can
    take (Measure / Column / Aggregation-wrapped Column)."""
    if not isinstance(field, dict):
        return None, None
    for key in ("Measure", "Column"):
        inner = field.get(key)
        if isinstance(inner, dict):
            return inner.get("Expression", {}).get("SourceRef", {}).get("Entity"), inner.get("Property")
    agg = field.get("Aggregation")
    if isinstance(agg, dict):
        inner = agg.get("Expression", {}).get("Column")
        if isinstance(inner, dict):
            return inner.get("Expression", {}).get("SourceRef", {}).get("Entity"), inner.get("Property")
    return None, None


def _entity_of_field(field: dict) -> str | None:
    return _entity_and_property_of_field(field)[0]


def _fix_query_refs(pages: list[dict]) -> None:
    """A projection carries a `queryRef` string ("Entity.Property") ALONGSIDE
    its structured `field` binding — Power BI Desktop validates both, not
    just the field object, so a queryRef left over from a fabricated or
    stale name (e.g. "Invoices.Total Discount Amount", from before _walk
    corrected the field itself to ARFact[DISCOUNT COST]) still triggers
    "Fields that need to be fixed" — the exact bad name Desktop reports
    ("(Invoices) Total Discount Amount") comes straight from this string,
    not from the field object _walk already fixed. Recompute every
    queryRef from its now-corrected field so the two can never disagree."""
    for page in pages:
        for visual in page.get("visuals", []):
            query_state = visual.get("visual", {}).get("query", {}).get("queryState", {})
            if not isinstance(query_state, dict):
                continue
            for value in query_state.values():
                if not isinstance(value, dict):
                    continue
                for proj in value.get("projections", []) or []:
                    if not isinstance(proj, dict):
                        continue
                    entity, prop = _entity_and_property_of_field(proj.get("field", {}))
                    if entity and prop:
                        proj["queryRef"] = f"{entity}.{prop}"


def _prune_unresolved_projections(pages: list[dict]) -> None:
    """A projection whose measure/column name never resolved to a real table
    (already WARNING-logged above) is left with whatever — often empty or
    entirely MISSING — Entity/Property the LLM (or a programmatic visual
    builder like _apply_kpi_containers) originally produced, since the
    resolve_* calls only ever set these on a successful match. Feeding an
    empty Entity into pbip-compiler crashes it outright
    (`entity[0].lower()` on an empty string); feeding a field with no
    Property key at all crashes it differently but just as hard
    (`meas["Property"]` -> KeyError) — either way it takes down the entire
    build over one bad field in one visual. Drop those specific unresolved
    projections instead — the rest of that visual (and every other visual/
    page) still builds; only that one binding is missing, which is a far
    better failure mode than losing the whole .pbix."""
    for page in pages:
        for visual in page.get("visuals", []):
            query_state = visual.get("visual", {}).get("query", {}).get("queryState", {})
            if not isinstance(query_state, dict):
                continue
            for role, value in query_state.items():
                if not isinstance(value, dict):
                    continue
                projections = value.get("projections")
                if not isinstance(projections, list):
                    continue
                kept = []
                for proj in projections:
                    entity, prop = _entity_and_property_of_field(proj.get("field", {})) if isinstance(proj, dict) else (None, None)
                    if entity and prop:
                        kept.append(proj)
                    else:
                        print(f"[build] WARNING: dropping unresolved '{role}' projection "
                              f"(queryRef='{proj.get('queryRef') if isinstance(proj, dict) else proj}') "
                              f"from visual '{visual.get('name')}' on page '{page.get('page', {}).get('name')}' "
                              f"— would otherwise crash pbip-compiler with an empty table reference")
                value["projections"] = kept


def _load_pages(converted_dir: str) -> list[dict]:
    pages = []
    for path in sorted(glob.glob(os.path.join(converted_dir, "page__*.json"))):
        with open(path, encoding="utf-8") as f:
            pages.append(json.load(f))
    _normalize_query_states(pages)
    return pages


_CARD_TYPES = {"card", "multiRowCard"}


def _normalize_query_states(pages: list[dict]) -> None:
    """The report_visuals conversion is supposed to shape every
    query.queryState.<Role> as {"projections": [...]}, but occasionally
    emits the bare projections list instead — pbip-compiler crashes with an
    unhelpful AttributeError on that shape ('list' object has no attribute
    'get'), so normalize it here rather than trust every LLM response to
    get this exactly right."""
    for page in pages:
        for visual in page.get("visuals", []):
            visual_obj = visual.get("visual", {})
            if not isinstance(visual_obj, dict):
                continue
            visual_type = visual_obj.get("visualType")

            query_state = visual_obj.get("query", {}).get("queryState", {})
            if isinstance(query_state, dict):
                for role, value in list(query_state.items()):
                    if isinstance(value, list):
                        query_state[role] = {"projections": value}

                # A card/multiRowCard's single data role is named "Values"
                # in Power BI's built-in Card visual — a projection filed
                # under "Y" instead (the role name for axis-based charts)
                # renders blank with no visible error, a common cause of
                # "the KPI isn't showing." Rename rather than trust every
                # conversion to use the exact role name for this specific
                # visual type.
                if visual_type in _CARD_TYPES and "Y" in query_state and "Values" not in query_state:
                    query_state["Values"] = query_state.pop("Y")


def _sanitize_visual_shapes(pages: list[dict]) -> None:
    """Run AFTER _fix_field_and_measure_refs / _prune_unresolved_projections
    (a visual's queryState can be emptied there), just before write_report.
    Fixes two PBIR schema-validation failures Power BI Desktop reports when
    opening the .pbip directly — both from report_visuals output this
    pipeline otherwise passes through as-is."""
    for page in pages:
        for visual in page.get("visuals", []):
            visual_obj = visual.get("visual", {})
            if not isinstance(visual_obj, dict):
                continue

            # PBIR: if `visual.query` is present at all it MUST contain a
            # `queryState`. report_visuals emits a bare `query: {}` (or one
            # whose queryState got emptied by projection pruning) for a
            # data-less visual — a textbox, an action-button placeholder, a
            # shape. A real Desktop-authored data-less visual simply has NO
            # `query` key, so drop it rather than ship an incomplete one
            # ("Required property 'queryState' was not included in the
            # /visual/query property").
            q = visual_obj.get("query")
            if isinstance(q, dict):
                qs = q.get("queryState")
                if not isinstance(qs, dict) or not any(
                    isinstance(v, dict) and v.get("projections") for v in qs.values()
                ):
                    visual_obj.pop("query", None)

            # PBIR: every `visual.objects.<name>` value must be a LIST of
            # {"properties": {...}} entries (real visual.json: "objects":
            # {"title": [{"properties": {...}}]}). report_visuals sometimes
            # emits a bare dict for one (seen: `general`, `text`) —
            # "Property /visual/objects/<name> was not provided as the
            # correct type". Coerce to the list shape.
            objects = visual_obj.get("objects")
            if isinstance(objects, dict):
                for key, val in list(objects.items()):
                    if isinstance(val, list):
                        continue
                    if isinstance(val, dict):
                        objects[key] = [val] if "properties" in val else [{"properties": val}]
                    else:
                        objects.pop(key, None)


def _load_kpi_containers(converted_dir: str) -> list[tuple[str, list[dict]]]:
    result = []
    for path in sorted(glob.glob(os.path.join(converted_dir, "kpi_container__*.json"))):
        table_name = os.path.basename(path)[len("kpi_container__"):-len(".json")]
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        result.append((table_name, data.get("kpis", [])))
    return result


def _apply_kpi_containers(
    kpi_containers: list[tuple[str, list[dict]]],
    tables: dict[str, dict],
    measures_by_table: dict[str, list[dict]],
    original_measure_table: dict[str, tuple[str, str]],
) -> list[dict]:
    """Hide each detected KPI-container config table, add any measure the
    conversion had to synthesize for a row's expression, and lay out one
    real card visual per row on a dedicated page — since we can't reliably
    tell which original Qlik sheet a container's tiles belonged to, group
    all of one config table's KPIs onto their own page rather than risk
    guessing wrong and overlapping hand-placed visuals on an existing page.
    """
    kpi_pages: list[dict] = []

    for table_name, kpis in kpi_containers:
        if table_name in tables:
            tables[table_name]["is_hidden"] = True

        def sort_key(k: dict):
            return (str(k.get("sheet") or ""), str(k.get("position", k.get("row_index", 0))))

        visuals = []
        for i, kpi in enumerate(sorted(kpis, key=sort_key)):
            main_ref = None
            if kpi.get("measure_name"):
                main_ref = ("", kpi["measure_name"])
            elif kpi.get("synthesized_measure"):
                sm = kpi["synthesized_measure"]
                measures_by_table.setdefault(sm["table"], []).append({
                    "name": sm["name"], "expression": sm["expression"],
                    "format_string": sm.get("format_string"),
                })
                original_measure_table[sm["name"].casefold()] = (sm["table"], sm["name"])
                main_ref = (sm["table"], sm["name"])
            if not main_ref:
                continue

            projections = [{
                "field": {"Measure": {"Expression": {"SourceRef": {"Entity": main_ref[0]}}, "Property": main_ref[1]}},
                "queryRef": f"KPI.{main_ref[1]}",
            }]

            sub = kpi.get("subtitle_measure")
            if sub:
                measures_by_table.setdefault(sub["table"], []).append({"name": sub["name"], "expression": sub["expression"]})
                original_measure_table[sub["name"].casefold()] = (sub["table"], sub["name"])
                projections.append({
                    "field": {"Measure": {"Expression": {"SourceRef": {"Entity": sub["table"]}}, "Property": sub["name"]}},
                    "queryRef": f"KPI.{sub['name']}",
                })

            col, row = i % 5, i // 5
            visuals.append({
                "name": f"kpi_{_safe(table_name)}_{i}",
                "position": {"x": col * 260, "y": row * 160, "z": i, "width": 240, "height": 140, "tabOrder": i},
                "visual": {
                    "visualType": "multiRowCard" if sub else "card",
                    "query": {"queryState": {"Y": {"projections": projections}}},
                    "objects": {},
                },
            })

        if visuals:
            kpi_pages.append({
                "page": {
                    "name": f"KPIContainer_{_safe(table_name)}",
                    "displayName": f"KPIs ({table_name})",
                    "width": 1280, "height": 720, "ordinal": 1000,
                },
                "visuals": visuals,
            })

    return kpi_pages


def _write_pbip_file(project_dir: str, app_name: str) -> None:
    pbip = {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/pbip/pbipProperties/1.0.0/schema.json",
        "version": "1.0",
        "artifacts": [{"report": {"path": f"{app_name}.Report"}}],
        "settings": {"enableAutoRecovery": True},
    }
    with open(os.path.join(project_dir, f"{app_name}.pbip"), "w", encoding="utf-8") as f:
        json.dump(pbip, f, indent=2)


def _merge_relationships(llm_relationships: list[dict], inferred_relationships: list[dict]) -> list[dict]:
    """Prefer the data-driven inference (grounded in real row counts) over
    the LLM's read of Qlik's own association metadata, since that metadata
    depends on Engine API details that aren't fully verifiable offline;
    still keep any LLM relationship covering a table pair the data-driven
    pass didn't find (e.g. an intentionally inactive/secondary path).

    Tabular models reject (or, worse, silently make DAX ambiguous at query
    time — "Unable to execute DAX ... ambiguous paths between X and Y") any
    ACTIVE relationship that reconnects two tables already reachable through
    some other chain of relationships. Checking for duplicate exact table
    PAIRS isn't enough to prevent this: e.g. inference already links
    ARFact-CustomerDim and CreditDim-CustomerDim, and the LLM separately
    proposes ARFact-CreditDim — a different pair each time, but activating
    the third relationship creates a cycle (ARFact reaches CustomerDim two
    ways).

    Build the accepted-active set as a spanning FOREST with union-find: a
    relationship whose two tables aren't already connected becomes active
    and joins the forest. A relationship that WOULD create a cycle is kept
    in the output too, but marked `is_active: false` rather than dropped —
    Qlik's associative model genuinely had that path, and Power BI supports
    exactly this multi-path shape natively (inactive relationship +
    `USERELATIONSHIP()` in any DAX measure that needs it). Silently deleting
    it instead of deactivating it is why a real association from the source
    app could end up missing entirely.
    """
    # Two proposals for the literal same join (same two tables AND same two
    # columns, direction-independent — the LLM re-describing what inference
    # already found) are the SAME relationship, not a second path: keep only
    # the first (inference wins, since it's grounded in real row counts) —
    # Tabular models reject two relationships on the identical column pair
    # regardless of active/inactive, so this must happen before cycle
    # detection, not be treated as a cycle to deactivate.
    def exact_key(rel: dict) -> frozenset:
        return frozenset({(rel["from_table"], rel["from_column"]), (rel["to_table"], rel["to_column"])})

    deduped: list[dict] = []
    seen_exact: set[frozenset] = set()
    for rel in inferred_relationships + llm_relationships:
        key = exact_key(rel)
        if key in seen_exact:
            continue
        seen_exact.add(key)
        deduped.append(rel)

    parent: dict[str, str] = {}

    def find(t: str) -> str:
        parent.setdefault(t, t)
        while parent[t] != t:
            parent[t] = parent[parent[t]]
            t = parent[t]
        return t

    def union(a: str, b: str) -> bool:
        ra, rb = find(a), find(b)
        if ra == rb:
            return False  # already connected — this edge would create a cycle if made active
        parent[ra] = rb
        return True

    merged: list[dict] = []
    for rel in deduped:
        if union(rel["from_table"], rel["to_table"]):
            merged.append(rel)
        else:
            rel = dict(rel, is_active=False)
            merged.append(rel)
            print(f"[build] relationship {rel['from_table']} -> {rel['to_table']} kept but marked inactive "
                  f"(another path already connects these tables — use USERELATIONSHIP() in DAX to activate "
                  f"this one where needed)")
    return merged


def _load_json(directory: str, filename: str) -> dict:
    path = os.path.join(directory, filename)
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _safe(name: str) -> str:
    import re
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)
