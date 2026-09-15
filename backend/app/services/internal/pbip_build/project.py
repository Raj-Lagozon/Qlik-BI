"""Assemble converted artifacts into a *.pbip project (Report + SemanticModel)
under output/<app_name>/, then compile it to a real .pbix."""

from __future__ import annotations

import csv
import datetime
import glob
import json
import os
import re
import shutil
import tempfile

from app.config.settings import settings

from .semantic_model import write_semantic_model
from .report import write_report
from .pbix_compile import compile_pbix
from .csv_m import (
    generate_partition_m, generate_combined_partition_m, generate_inline_partition_m,
    wrap_with_left_join_aggregation,
)
from .infer_relationships import infer_relationships
from .what_if_params import detect_what_if_parameters, generate_range_m

EXTRACTED_ROOT = str(settings.project_root / "extracted")
CONVERTED_ROOT = str(settings.project_root / "converted")

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


OUTPUT_ROOT = str(settings.project_root / "output")


def build_project(app_name: str) -> str:
    """Returns the path to the compiled .pbix file."""
    extracted_dir = os.path.join(EXTRACTED_ROOT, app_name)
    converted_dir = os.path.join(CONVERTED_ROOT, app_name)
    project_dir = os.path.join(OUTPUT_ROOT, app_name)
    os.makedirs(project_dir, exist_ok=True)

    (tables, measures_by_table, calc_cols_by_table, hierarchies_by_table,
     original_measure_table, required_relationship_orientations) = _assemble_semantic_inputs(extracted_dir, converted_dir)

    llm_relationships = _load_json(converted_dir, "data_model.converted.json").get("relationships", [])
    inferred_relationships = infer_relationships(tables, extracted_dir)
    relationships = _merge_relationships(llm_relationships, inferred_relationships)
    relationships = _orient_relationships_for_related(relationships, required_relationship_orientations)
    relationships = _drop_relationships_into_related_calc_tables(relationships, tables)
    roles = _load_json(converted_dir, "rls.converted.json").get("roles", [])
    parameters = _load_json(converted_dir, "variables.converted.json").get("variables", [])
    parameters = [p for p in parameters if p.get("target") == "power_query_parameter"]

    pages = _load_pages(converted_dir)

    # A Qlik sheet built from its own "Data Model Viewer" (a table listing
    # $Table/$Rows/$Field/... — Qlik's built-in introspection system
    # fields, not real data) has no Power BI equivalent: there's no "Model"
    # table, and never will be, so leaving the binding as-is always shows
    # "fields that need to be fixed". Swap any such visual for a plain
    # textbox noting what it was, the same fallback already used for any
    # other genuinely unrepresentable Qlik object.
    _replace_system_field_visuals(pages)

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

    # A Qlik KPI can be a hard-coded constant wrapped in Sum() (e.g.
    # "=Sum(5)") — a common idiom for a static target/placeholder tile, not
    # a real aggregation over any field. report_visuals recognizes this (it
    # says so in the visual's own "notes": "Placeholder measure 'Sum5'
    # (value=5)") but still binds the visual to a FABRICATED table
    # ("MeasureTable") and measure name that exist nowhere in the model —
    # Power BI reports that as "Fields that need to be fixed". Give the
    # fabricated measure name a real, constant-valued DAX measure so the
    # existing binding resolves naturally instead of pointing at nothing.
    _synthesize_placeholder_constant_measures(pages, tables, measures_by_table, original_measure_table)

    # A Qlik "Last reload"/ReloadTime() KPI has no field in the .qvf script
    # to bind to (it's a Qlik system value, not stored data) — deliberately
    # NOT reproduced with an invented supporting table. The goal is to match
    # what's in the Qlik app's own script/data model, not add infrastructure
    # Qlik never had; that placeholder is left to _fix_field_and_measure_refs'
    # normal unresolved-projection handling (dropped/warned, same as any
    # other field that can't be resolved) rather than synthesized.

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
    _wrap_bare_measure_refs(measures_by_table, calc_cols_by_table, original_measure_table)
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


# A Qlik "rolling aggregate" table: computed with RESIDENT + GROUP BY over
# another already-loaded table, grouped by a key pulled from a separate
# `Mapping LOAD` table via ApplyMap() — e.g. a rolling-N-day distinct count
# per customer, where the customer id isn't a stored field on the source
# table itself. It was never a source FILE either (RESIDENT reads another
# in-app table, not `FROM [lib://...]`), so it has no CSV to point
# SourceDataPath at — it needs to become a DAX calculated table instead,
# same idea as the master-calendar handling below.
_MAPPING_TABLE_RE = re.compile(
    r"\b(?P<name>\w+):\s*Mapping\s+LOAD\s+(?P<key>\w+)\s*,\s*(?P<value>\w+)\s*RESIDENT\s+(?P<source>\w+)",
    re.IGNORECASE,
)
_GROUPBY_APPLYMAP_COUNT_RE = re.compile(
    r"\b(?P<table>\w+):\s*LOAD\s+"
    r"ApplyMap\(\s*'(?P<map>\w+)'\s*,\s*(?P<key>\w+)\s*\)\s+AS\s+(?P<alias>\w+)\s*,\s*"
    r"Count\(\s*DISTINCT\s+(?P<countfield>\w+)\s*\)\s+AS\s+(?P<measure>\w+)\s*"
    r"RESIDENT\s+(?P<source>\w+)\s*"
    r"(?:WHERE\s+(?P<wfield>\w+)\s*(?P<op>>=|<=|>|<|=)\s*'(?P<wval>[^']+)'\s*)?"
    r"GROUP\s+BY\s+ApplyMap\(\s*'(?P=map)'\s*,\s*(?P=key)\s*\)",
    re.IGNORECASE,
)


def _detect_groupby_count_tables(script_text: str) -> dict[str, dict]:
    """{table_name: {source_table, key_field, count_field, measure_name,
    group_alias, value_field, value_source_table, where_field, where_op,
    where_val}} for every RESIDENT+GROUP BY-via-ApplyMap table found, with
    its ApplyMap() resolved back to the real table/column it actually reads
    (through the matching `Mapping LOAD`)."""
    mappings = {}
    for m in _MAPPING_TABLE_RE.finditer(script_text):
        mappings[m.group("name").casefold()] = {
            "key_field": m.group("key"), "value_field": m.group("value"), "source_table": m.group("source"),
        }
    out: dict[str, dict] = {}
    for m in _GROUPBY_APPLYMAP_COUNT_RE.finditer(script_text):
        mapping = mappings.get(m.group("map").casefold())
        if not mapping:
            continue
        out[m.group("table")] = {
            "source_table": m.group("source"),
            "key_field": m.group("key"),
            "count_field": m.group("countfield"),
            "measure_name": m.group("measure"),
            "group_alias": m.group("alias"),
            "value_field": mapping["value_field"],
            "value_source_table": mapping["source_table"],
            "where_field": m.group("wfield"),
            "where_op": m.group("op"),
            "where_val": m.group("wval"),
        }
    return out


def _qlik_date_literal_to_dax(literal: str) -> str:
    try:
        d = datetime.datetime.strptime(literal, "%Y-%m-%d")
        return f"DATE({d.year}, {d.month}, {d.day})"
    except ValueError:
        # Not the ISO format this script happened to use — fall back to
        # DATEVALUE, which still compares correctly against a real `date`
        # column regardless of the literal's original text format.
        return f'DATEVALUE("{literal}")'


def _build_groupby_count_table(table_name: str, spec: dict, fields_by_table: dict[str, set[str]]) -> dict | None:
    source_table = spec["source_table"]
    value_table = spec["value_source_table"]
    key_field = spec["key_field"]
    if source_table not in fields_by_table or value_table not in fields_by_table:
        return None
    if key_field not in fields_by_table[source_table] or key_field not in fields_by_table[value_table]:
        return None
    if spec["value_field"] not in fields_by_table[value_table]:
        return None
    if spec["count_field"] not in fields_by_table[source_table]:
        return None

    base = source_table
    if spec["where_field"] and spec["where_field"] in fields_by_table[source_table] and spec["where_val"]:
        date_expr = _qlik_date_literal_to_dax(spec["where_val"])
        base = f"FILTER({source_table}, {source_table}[{spec['where_field']}] {spec['where_op']} {date_expr})"

    dax_expression = (
        "SUMMARIZE(\n"
        f'    ADDCOLUMNS(\n        {base},\n'
        f'        "{spec["group_alias"]}", RELATED({value_table}[{spec["value_field"]}])\n    ),\n'
        f'    [{spec["group_alias"]}],\n'
        f'    "{spec["measure_name"]}", DISTINCTCOUNT({source_table}[{spec["count_field"]}])\n'
        ")"
    )
    columns = [
        {"name": spec["group_alias"], "data_type": "string", "source_column": spec["group_alias"]},
        {"name": spec["measure_name"], "data_type": "int64", "source_column": spec["measure_name"]},
    ]
    print(f"[build] '{table_name}' detected as a Qlik RESIDENT/GROUP BY aggregate over '{source_table}' "
          f"(the group key resolves via mapping to '{value_table}[{spec['value_field']}]') — building it "
          f"as a DAX calculated table (RELATED + SUMMARIZE) instead of loading from CSV: it was never a "
          f"source file in Qlik either, it's computed at load time from '{source_table}'.")
    return {"columns": columns, "m_expression": "", "is_calculated": True, "dax_expression": dax_expression}


_MONTH_DERIVED_RE = re.compile(
    r"\b(Month|MonthName)\s*\(\s*(?:Date#\(\s*)?([A-Za-z_]\w*)(?:\s*,[^)]*\))?\s*\)\s+[Aa][Ss]\s+([A-Za-z_]\w*)"
)


def _detect_month_derived_columns(script_text: str) -> dict[str, dict]:
    """Find every `Month(...)`/`MonthName(...)  AS <Field>` in the Qlik LOAD
    script and return {target_field.casefold(): {"func", "source"}} — these
    fields are computed by Qlik at load time from another field ALREADY in
    the same LOAD, never read from the source file, regardless of what the
    source file actually contains. Used to reproduce the same computation in
    the generated Power Query M instead of expecting the source CSV to have
    them (see csv_m.py)."""
    out: dict[str, dict] = {}
    for m in _MONTH_DERIVED_RE.finditer(script_text):
        func, source_field, target_field = m.group(1), m.group(2), m.group(3)
        out[target_field.casefold()] = {"func": func, "source": source_field}
    return out


_LOAD_BLOCK_RE = re.compile(
    # Field list ends at whichever comes first: a real FROM/RESIDENT clause,
    # or a statement-ending ";" — a LOAD with neither (LOAD ... INLINE [...]
    # has no FROM/RESIDENT at all) would otherwise make the non-greedy `.*?`
    # keep expanding straight through the INLINE block and into whatever
    # table's LOAD comes NEXT in the script, misattributing its entire field
    # list (and anything a caller detects in it) to THIS table instead.
    r"\b(\w+):\s*LOAD\s+(.*?)\s*(?:\bFROM\b|\bRESIDENT\b|;)", re.IGNORECASE | re.DOTALL,
)


def _split_top_level_commas(text: str) -> list[str]:
    """Split a Qlik LOAD field list on commas that are NOT inside a
    function call's parentheses or a quoted string — a plain str.split(",")
    would wrongly cut `Count(DISTINCT A, B)` (no such case here, but general
    LOAD lists can nest commas) apart into fragments."""
    items, depth, quote, start = [], 0, None, 0
    for i, ch in enumerate(text):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            items.append(text[start:i])
            start = i + 1
    items.append(text[start:])
    return items


_SIMPLE_RENAME_RE = re.compile(r"^\s*([A-Za-z_]\w*)\s+[Aa][Ss]\s+([A-Za-z_]\w*)\s*$")


_TABLE_LABEL_LOAD_RE = re.compile(r"\b(\w+):\s*\r?\n\s*(?:mapping\s+)?(?:LOAD|SELECT)\b", re.IGNORECASE)
_CONCAT_JOIN_PREFIX_RE = re.compile(
    r"\b(?:concatenate|join|left\s+join|right\s+join|inner\s+join|outer\s+join)\s*\(\s*(\w+)\s*\)",
    re.IGNORECASE,
)
_FROM_FILE_RE = re.compile(r"\bFROM\s*\[([^\]]+)\]", re.IGNORECASE)


def _detect_source_files(script_text: str) -> dict[str, list[str]]:
    """The real CSV file(s) each table actually loads from, per the Qlik
    script itself — NEVER assume a table's source file is named after the
    table (`{TableName}.csv`): the two commonly disagree (seen: table
    `Dim_Product` loads `FROM [.../Product_Master.csv]`, `Dim_Zones` loads
    `Zone_Master.csv`, etc. — every single-source table in one real app had
    a different file name than its table name). Also handles a table built
    from SEVERAL source files loaded one after another — either several
    unlabeled `LOAD ... FROM [...]` blocks in a row (Qlik auto-concatenates
    a LOAD with no table name of its own onto whichever table is currently
    "in scope"), or an explicit `Concatenate(TableName) LOAD ... FROM
    [...]`. Only the FIRST block carries the `TableName:` label; a
    script-parser that only recognizes labeled blocks (see _LOAD_BLOCK_RE
    and friends) only ever sees that first file and silently ignores the
    rest.

    Walks the script sequentially tracking which table is "current" (set by
    a `TableName:` label or a Concatenate/Join(...) prefix) and records
    every `FROM [...]` filename against it, in the order the script loads
    them — a `Join`/`Left Join`/etc. (merges COLUMNS from a RESIDENT
    aggregation, not more rows from a file) naturally contributes no FROM
    file and so never shows up here.

    Returns {table_name: [filename, ...]} for every table with at least one
    detected source file (most tables: exactly one entry — still use it
    instead of guessing `{table}.csv`; a table with none found at all, e.g.
    built from `LOAD ... INLINE [...]` or purely from other RESIDENT
    tables, isn't in this dict — callers fall back to their own default for
    those)."""
    events: list[tuple[int, str, str]] = []
    for m in _TABLE_LABEL_LOAD_RE.finditer(script_text):
        events.append((m.start(), "current_table", m.group(1)))
    for m in _CONCAT_JOIN_PREFIX_RE.finditer(script_text):
        events.append((m.start(), "current_table", m.group(1)))
    for m in _FROM_FILE_RE.finditer(script_text):
        # Just the filename — the lib:// connection path is only valid on
        # the machine that authored the .qvf; every table's M already
        # resolves against the shared SourceDataPath parameter instead.
        filename = re.split(r"[/\\]", m.group(1))[-1]
        events.append((m.start(), "from", filename))
    events.sort(key=lambda e: e[0])

    sources_by_table: dict[str, list[str]] = {}
    current_table: str | None = None
    for _, kind, value in events:
        if kind == "current_table":
            current_table = value
        elif current_table:
            files = sources_by_table.setdefault(current_table, [])
            if value not in files:
                files.append(value)
    return sources_by_table


_INLINE_TABLE_RE = re.compile(
    r"\b(\w+):\s*\r?\n\s*LOAD\s+(?:\*|[^\[\];]*?)\s*INLINE\s*\[(.*?)\]", re.IGNORECASE | re.DOTALL,
)


def _split_inline_csv_line(line: str) -> list[str]:
    return [cell.strip().strip("'\"") for cell in _split_top_level_commas(line)]


def _detect_inline_tables(script_text: str) -> dict[str, list[dict[str, str]]]:
    """A Qlik `LOAD * INLINE [header\\nrow1\\nrow2...]` table — literal rows
    typed directly into the script, not sourced from any file at all (seen:
    a small lookup/config table like a manager/zone assignment list). A
    build that assumes every table has a `{TableName}.csv` (or even any
    file) to load from fails outright for one of these — "Could not find
    file" — since no such file was ever meant to exist. Parsed as a simple
    comma-separated block (Qlik's own INLINE syntax): first line is the
    header, everything after is data rows.

    Returns {table_name: [{"ColName": "value", ...}, ...]}."""
    out: dict[str, list[dict[str, str]]] = {}
    for m in _INLINE_TABLE_RE.finditer(script_text):
        table_name, block = m.group(1), m.group(2)
        lines = [ln for ln in (raw.strip() for raw in block.splitlines()) if ln]
        if not lines:
            continue
        header = _split_inline_csv_line(lines[0])
        rows = []
        for line in lines[1:]:
            cells = _split_inline_csv_line(line)
            rows.append({h: (cells[i] if i < len(cells) else "") for i, h in enumerate(header)})
        out[table_name] = rows
    return out


def _detect_simple_renamed_columns(script_text: str) -> dict[str, dict[str, str]]:
    """Find every LOAD field that's a bare rename — `RiskBand AS
    PredictedRiskBand`, nothing else on either side — per table. Anchored to
    the WHOLE field expression (not just "immediately before AS") so a
    concatenation like `chr(10) & InsightText AS Insights` is correctly NOT
    treated as a rename of InsightText (it's a computed expression, out of
    scope here — the field expression as a whole isn't just one identifier).

    A source CSV that mirrors the Qlik table's ORIGINAL columns (rather than
    its post-script output) still has the field under its OLD name — e.g.
    the file has "RiskBand" but the model/measures expect "PredictedRiskBand"
    — so `Table.SelectColumns` by the new name alone finds nothing and the
    column loads as null even though the file genuinely has the data under
    its other name. Reproduce the rename in Power Query instead (see
    csv_m.py) rather than expecting the file to already use the new name.

    Returns {table_name: {target_field.casefold(): source_field}}."""
    out: dict[str, dict[str, str]] = {}
    for block in _LOAD_BLOCK_RE.finditer(script_text):
        table_name, field_list = block.group(1), block.group(2)
        renames: dict[str, str] = {}
        for item in _split_top_level_commas(field_list):
            m = _SIMPLE_RENAME_RE.match(item)
            if not m:
                continue
            source_field, target_field = m.group(1), m.group(2)
            if source_field.casefold() != target_field.casefold():
                renames[target_field.casefold()] = source_field
        if renames:
            out[table_name] = renames
    return out


# Chr(N) is Qlik's function for a literal character by its Unicode/ASCII
# code point (Chr(10) = line feed) — Power Query's exact equivalent is
# Character.FromNumber(N) (confirmed against Qlik's own Chr() docs and the
# Power Query M reference). Covers both orderings: the literal character
# concatenated before OR after the real field.
_CHR_PREFIX_RE = re.compile(r"^\s*chr\(\s*(\d+)\s*\)\s*&\s*([A-Za-z_]\w*)\s+[Aa][Ss]\s+([A-Za-z_]\w*)\s*$", re.IGNORECASE)
_CHR_SUFFIX_RE = re.compile(r"^\s*([A-Za-z_]\w*)\s*&\s*chr\(\s*(\d+)\s*\)\s+[Aa][Ss]\s+([A-Za-z_]\w*)\s*$", re.IGNORECASE)


def _detect_chr_concat_columns(script_text: str) -> dict[str, dict[str, dict]]:
    """Find every LOAD field that's `Chr(<code>) & <Field> AS <Target>` (or
    the same with the order flipped) — e.g. `chr(10) & InsightText AS
    Insights`, a Qlik idiom for prefixing/suffixing a real field with a
    literal character (very often a line break before a text block). Like
    _detect_simple_renamed_columns, this is a genuinely COMPUTED field, not
    something any source file was ever going to contain under the target
    name — reproduced as its own Power Query step (see csv_m.py) instead of
    silently loading as null.

    Returns {table_name: {target_field: {"func": "ChrConcat", "source":
    field, "code": int, "prefix": bool}}} — merged into the same `computed`
    dict _detect_month_derived_columns feeds (same downstream plumbing)."""
    out: dict[str, dict[str, dict]] = {}
    for block in _LOAD_BLOCK_RE.finditer(script_text):
        table_name, field_list = block.group(1), block.group(2)
        found: dict[str, dict] = {}
        for item in _split_top_level_commas(field_list):
            m = _CHR_PREFIX_RE.match(item)
            if m:
                code, source_field, target_field = m.group(1), m.group(2), m.group(3)
                found[target_field.casefold()] = {"func": "ChrConcat", "source": source_field, "code": int(code), "prefix": True}
                continue
            m = _CHR_SUFFIX_RE.match(item)
            if m:
                source_field, code, target_field = m.group(1), m.group(2), m.group(3)
                found[target_field.casefold()] = {"func": "ChrConcat", "source": source_field, "code": int(code), "prefix": False}
        if found:
            out[table_name] = found
    return out


# Qlik's idiom for a composite surrogate key from two text fields, avoiding
# a synthetic key when two tables need to join on a combination of columns
# neither has alone: `dual(A & 'sep' & B, autonumber(A & 'sep' & B)) as
# Target` — a "dual" value that DISPLAYS as the concatenated text but
# SORTS/COMPARES as the autonumber()-assigned integer. DAX/Power Query have
# no equivalent dual-value type, and there's no way to reproduce Qlik's own
# autonumber() sequence outside Qlik anyway — but a relationship only needs
# the two sides to agree on a single, unique key value, not specifically a
# number, so the plain text concatenation alone (dropping the numeric
# "display as" half) is a completely faithful substitute: same value,
# same join behavior, just stored as text instead of a dual.
_DUAL_AUTONUMBER_KEY_RE = re.compile(
    r"^\s*dual\(\s*([A-Za-z_]\w*)\s*&\s*'([^']*)'\s*&\s*([A-Za-z_]\w*)\s*,\s*"
    r"autonumber\(\s*\1\s*&\s*'[^']*'\s*&\s*\3\s*\)\s*\)\s+[Aa][Ss]\s+([A-Za-z_]\w*)\s*$",
    re.IGNORECASE,
)


def _detect_dual_autonumber_keys(script_text: str) -> dict[str, dict[str, dict]]:
    """Find every `dual(A & 'sep' & B, autonumber(A & 'sep' & B)) AS Target`
    field. Left unhandled, `Target` matches no column in any source file
    (it's 100% computed, never a stored field) and loads as blank for every
    row — which for a key column doesn't just leave one field empty, it
    breaks the one-to-one/primary-key constraint on whatever relationship
    uses it and can cancel refresh of the ENTIRE model, not just this table
    (seen: "Column 'ProductKey' ... contains blank values ..." plus every
    other table in the same refresh reporting "Load was cancelled by an
    error in loading a previous table").

    Returns {table_name: {target_field.casefold(): {"func": "ConcatKey",
    "field_a", "field_b", "sep"}}} — merged into the same `computed` dict
    the other script-derived-column detectors feed."""
    out: dict[str, dict[str, dict]] = {}
    for block in _LOAD_BLOCK_RE.finditer(script_text):
        table_name, field_list = block.group(1), block.group(2)
        found: dict[str, dict] = {}
        for item in _split_top_level_commas(field_list):
            m = _DUAL_AUTONUMBER_KEY_RE.match(item)
            if not m:
                continue
            field_a, sep, field_b, target_field = m.group(1), m.group(2), m.group(3), m.group(4)
            found[target_field.casefold()] = {"func": "ConcatKey", "field_a": field_a, "field_b": field_b, "sep": sep}
        if found:
            out[table_name] = found
    return out


# A Qlik `mapping LOAD key, value FROM [...]` table — a small lookup table
# (not a real data table; never gets its own TMDL table in Power BI, only
# ever read via ApplyMap()) whose two columns are its key and value. Only
# the plain "load straight from a file" shape is handled here — a mapping
# table itself built from RESIDENT/INLINE is out of scope for now.
_MAPPING_FROM_FILE_RE = re.compile(
    r"\b(?P<name>\w+):\s*mapping\s+LOAD\s+(?P<key>\w+)\s*,\s*(?P<value>\w+)\s*FROM\s*\[",
    re.IGNORECASE,
)


def _detect_mapping_tables(script_text: str) -> dict[str, dict]:
    """{map_name.casefold(): {"key_field", "value_field", "table_label"}}
    for every `mapping LOAD key, value FROM [...]` block — `table_label` is
    the mapping table's own script label (e.g. "map"), used to look its
    source filename up in `_detect_source_files`'s result (that function
    already treats a `mapping LOAD` label as its own table, same as any
    real one)."""
    out: dict[str, dict] = {}
    for m in _MAPPING_FROM_FILE_RE.finditer(script_text):
        out[m.group("name").casefold()] = {
            "key_field": m.group("key"), "value_field": m.group("value"), "table_label": m.group("name"),
        }
    return out


# Qlik's SubField(ApplyMap('map', Field, 'default'), 'sep', N) AS Target —
# looks Field up in a mapping table (falling back to 'default' when
# unmatched), then splits whatever comes back on 'sep' and takes the Nth
# (1-based) piece. Seen reproducing a "Region" field from a
# City -> "Zone-Region" mapping table via SubField(..., '-', 2). Anchored
# to the WHOLE field expression, same convention as every other computed-
# column detector here.
_APPLYMAP_SUBFIELD_RE = re.compile(
    r"^\s*SubField\(\s*ApplyMap\(\s*'(?P<map>[^']+)'\s*,\s*([A-Za-z_]\w*)\s*,\s*'(?P<default>[^']*)'\s*\)\s*,\s*"
    r"'(?P<sep>[^']*)'\s*,\s*(?P<part>\d+)\s*\)\s+[Aa][Ss]\s+([A-Za-z_]\w*)\s*$",
    re.IGNORECASE,
)


def _detect_applymap_subfield_columns(script_text: str) -> dict[str, dict[str, dict]]:
    """Find every `SubField(ApplyMap('map', Field, 'default'), 'sep', N) AS
    Target` field, per table. Left unhandled, `Target` matches no column in
    any source file (it's 100% computed from a SEPARATE mapping file, never
    a stored field of this table) and loads as blank for every row — which
    then breaks any measure/filter built on it (e.g. `WHERE Region =
    "Unassigned"` never matches when Region is always null).

    Returns {table_name: {target_field.casefold(): {"func":
    "ApplyMapSubfield", "map_name", "source" (the field looked up),
    "default", "sep", "part_index" (0-based)}}} — merged into the same
    `computed` dict the other script-derived-column detectors feed. The
    mapping table's own key/value fields and source filename are resolved
    separately (see _detect_mapping_tables / source_files_by_table) once
    both are available, in _assemble_semantic_inputs."""
    out: dict[str, dict[str, dict]] = {}
    for block in _LOAD_BLOCK_RE.finditer(script_text):
        table_name, field_list = block.group(1), block.group(2)
        found: dict[str, dict] = {}
        for item in _split_top_level_commas(field_list):
            m = _APPLYMAP_SUBFIELD_RE.match(item)
            if not m:
                continue
            map_name, source_field, default, sep, part, target_field = m.groups()
            found[target_field.casefold()] = {
                "func": "ApplyMapSubfield", "map_name": map_name, "source": source_field,
                "default": default, "sep": sep, "part_index": int(part) - 1,
            }
        if found:
            out[table_name] = found
    return out


# `left join(Target) LOAD <fields> RESIDENT Source ... GROUP BY <keys>;` —
# Qlik joins an aggregate computed from another already-loaded table's rows
# onto Target's own columns (matched on the shared key field names — Qlik's
# join is associative on common field names, no explicit ON clause). A
# script-parser that only reads FROM-file LOADs never sees this at all: the
# joined column has no source file of its own, so it silently loads as
# blank in Target regardless of what its source file actually contains,
# which then blanks out any measure built on it.
_JOIN_RESIDENT_GROUPBY_RE = re.compile(
    r"\bleft\s+join\s*\(\s*(\w+)\s*\)\s*\r?\n\s*load\s+(.*?)\s*"
    r"resident\s+(\w+)\s*(?:where\s+.*?)?group\s+by\s+.*?;",
    re.IGNORECASE | re.DOTALL,
)
_JOIN_AGG_ITEM_RE = re.compile(r"^\s*(sum|count|avg|min|max)\s*\(\s*([A-Za-z_]\w*)\s*\)\s+[Aa][Ss]\s+\"?([A-Za-z_]\w*)\"?\s*$")
_JOIN_KEYFUNC_ITEM_RE = re.compile(r"^\s*([A-Za-z_]\w*)\s*\(\s*([A-Za-z_]\w*)\s*\)\s+[Aa][Ss]\s+\"?([A-Za-z_]\w*)\"?\s*$")
_BARE_FIELD_ITEM_RE = re.compile(r"^\s*([A-Za-z_]\w*)\s*$")

# Qlik function -> M equivalent, for the (typically date-bucketing) key
# expression a GROUP BY commonly uses — only these are supported; a group
# key using any other function makes the whole table's detection back off
# (print a warning, leave the joined column blank as before) rather than
# guess at an unfamiliar function's M translation.
_QLIK_TO_M_DATE_FUNC = {
    "monthstart": "Date.StartOfMonth", "monthend": "Date.EndOfMonth",
    "year": "Date.Year", "month": "Date.Month",
    "weekstart": "Date.StartOfWeek", "weekend": "Date.EndOfWeek",
}
_QLIK_TO_M_AGG_FUNC = {"sum": "List.Sum", "count": "List.Count", "avg": "List.Average", "min": "List.Min", "max": "List.Max"}


def _detect_join_resident_aggregations(script_text: str) -> dict[str, dict]:
    """Returns {target_table: {"source_table": ..., "group_by": [{"alias",
    "func", "field"}, ...], "aggregations": [{"alias", "func", "field"},
    ...]}} for a `left join(Target) LOAD ... RESIDENT Source ... GROUP BY
    ...` block whose every field is one of: a bare group-by key, a
    recognized-function group-by key (see _QLIK_TO_M_DATE_FUNC), or a
    recognized aggregation (see _QLIK_TO_M_AGG_FUNC). Reproduced in
    csv_m.wrap_with_left_join_aggregation as Table.Group + Table.NestedJoin
    against the OTHER table's own M query, referenced by name — Power
    Query resolves cross-query references into the correct refresh order
    automatically, the same way Qlik's own script does this at load time."""
    out: dict[str, dict] = {}
    for m in _JOIN_RESIDENT_GROUPBY_RE.finditer(script_text):
        target_table, field_list, source_table = m.group(1), m.group(2), m.group(3)
        group_by: list[dict] = []
        aggregations: list[dict] = []
        ok = True
        for item in _split_top_level_commas(field_list):
            am = _JOIN_AGG_ITEM_RE.match(item)
            if am:
                func = am.group(1).casefold()
                if func not in _QLIK_TO_M_AGG_FUNC:
                    ok = False
                    break
                aggregations.append({"func": func, "field": am.group(2), "alias": am.group(3)})
                continue
            fm = _JOIN_KEYFUNC_ITEM_RE.match(item)
            if fm:
                func = fm.group(1).casefold()
                if func not in _QLIK_TO_M_DATE_FUNC:
                    ok = False
                    break
                group_by.append({"func": func, "field": fm.group(2), "alias": fm.group(3)})
                continue
            bm = _BARE_FIELD_ITEM_RE.match(item)
            if bm:
                group_by.append({"func": "identity", "field": bm.group(1), "alias": bm.group(1)})
                continue
            ok = False
            break
        if ok and group_by and aggregations:
            out[target_table] = {"source_table": source_table, "group_by": group_by, "aggregations": aggregations}
        elif not ok:
            print(f"[build] NOTE: '{target_table}' has a LEFT JOIN ... RESIDENT ... GROUP BY block that "
                  f"doesn't match a recognized simple aggregation shape — left unreproduced (the joined "
                  f"column(s) will load blank); add it by hand in Power BI Desktop if needed")
    return out


def _assemble_semantic_inputs(extracted_dir: str, converted_dir: str):
    raw_data_model = _load_json(extracted_dir, "data_model.json")
    if not raw_data_model.get("tables"):
        script_path = os.path.join(extracted_dir, "script.qvs")
        script_has_load = False
        if os.path.exists(script_path):
            with open(script_path, encoding="utf-8") as f:
                script_has_load = bool(re.search(r"\bLOAD\b", f.read(), re.IGNORECASE))
        if script_has_load:
            raise RuntimeError(
                f"'{os.path.basename(extracted_dir)}': data_model.json has 0 tables but "
                "script.qvs clearly has LOAD statements — extraction produced an "
                "incomplete result (a build would only produce synthetic tables like "
                "a what-if slider range, nothing real). Re-run extract for this app "
                "instead of building from this extracted data."
            )
    converted_data_model = _load_json(converted_dir, "data_model.converted.json")
    column_types = converted_data_model.get("column_types", {})
    calendar_table_names = _detect_master_calendar_tables(raw_data_model)

    script_path = os.path.join(extracted_dir, "script.qvs")
    month_derived: dict[str, dict] = {}
    groupby_count_tables: dict[str, dict] = {}
    renamed_by_table: dict[str, dict[str, str]] = {}
    chr_concat_by_table: dict[str, dict[str, dict]] = {}
    dual_key_by_table: dict[str, dict[str, dict]] = {}
    source_files_by_table: dict[str, list[str]] = {}
    inline_tables: dict[str, list[dict[str, str]]] = {}
    join_agg_by_table: dict[str, dict] = {}
    mapping_tables: dict[str, dict] = {}
    applymap_by_table: dict[str, dict[str, dict]] = {}
    if os.path.exists(script_path):
        with open(script_path, encoding="utf-8") as f:
            script_text = f.read()
        month_derived = _detect_month_derived_columns(script_text)
        groupby_count_tables = _detect_groupby_count_tables(script_text)
        renamed_by_table = _detect_simple_renamed_columns(script_text)
        chr_concat_by_table = _detect_chr_concat_columns(script_text)
        dual_key_by_table = _detect_dual_autonumber_keys(script_text)
        source_files_by_table = _detect_source_files(script_text)
        inline_tables = _detect_inline_tables(script_text)
        join_agg_by_table = _detect_join_resident_aggregations(script_text)
        mapping_tables = _detect_mapping_tables(script_text)
        applymap_by_table = _detect_applymap_subfield_columns(script_text)

    fields_by_table: dict[str, set[str]] = {}
    for rt in raw_data_model.get("tables", []):
        rt_name = rt.get("qName") or rt.get("name")
        if rt_name:
            fields_by_table[rt_name] = {
                f.get("qName") or f.get("name")
                for f in rt.get("qFields", rt.get("fields", []))
                if f.get("qName") or f.get("name")
            }

    tables: dict[str, dict] = {}
    # A RESIDENT/GROUP BY calculated table's own DAX does RELATED(<value
    # table>[...]) while iterating <source table> — that REQUIRES <source
    # table> to be the many side and <value table> the one side of whatever
    # relationship connects them, regardless of which direction that
    # relationship happens to already point (e.g. the LLM's data_model
    # conversion, or infer_relationships, may have called the OTHER table
    # "many" for unrelated reasons — DisputeFact can have more than one
    # dispute per invoice, making it genuinely the many side relative to
    # ARFact for this pair, even where ARFact is the many/fact side of
    # every OTHER relationship in the model). Collected here and applied
    # after relationships are merged, in build_project.
    required_relationship_orientations: list[tuple[str, str]] = []
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

        if table_name in groupby_count_tables:
            spec = groupby_count_tables[table_name]
            gb_table = _build_groupby_count_table(table_name, spec, fields_by_table)
            if gb_table:
                tables[table_name] = gb_table
                required_relationship_orientations.append((spec["source_table"], spec["value_source_table"]))
                continue
            print(f"[build] WARNING: '{table_name}' looks like a RESIDENT/GROUP BY aggregate table but its "
                  f"source fields/mapping couldn't be fully resolved — building it as a normal CSV-loaded "
                  f"table instead (it will need a CSV of its own, which Qlik never had either)")

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
            if (dtype in ("int64", "double") and "month" in fname.casefold()
                    and fname.casefold() not in month_derived):
                # A field whose NAME is specifically about a month (not
                # numeric fields generally) can genuinely hold either shape
                # depending on the real source file: "6"/"06" or "Jun"/
                # "June" — force-converting the text form to a number (or
                # nulling it) loses information either way. "variant" is a
                # real Tabular column type for exactly this — it stores
                # whatever the Power Query step actually produces, number or
                # text, without forcing a single type (see csv_m.py's
                # variant_cols handling: numeric text becomes a real number,
                # anything else passes through unchanged as text).
                # Excludes month_derived fields (e.g. InvoiceMonthNum) —
                # those are always computed via Date.Month(...) in Power
                # Query, never read from the source file, so they're
                # reliably int64 already; no ambiguity to guard against.
                dtype = "variant"
            columns.append({"name": fname, "data_type": dtype, "source_column": fname})
        if skipped_by_llm:
            print(f"[build] {table_name}: {skipped_by_llm} column(s) not classified by the data model "
                  f"conversion — inferred type from Qlik field tags instead")
        # Table/column names and types above come entirely from Qlik's own
        # metadata (data_model.json / data_model.converted.json) — never from
        # a CSV — so the partition M can always be generated from them. It
        # deliberately does NOT depend on an extracted data/<table>.csv
        # existing: extraction no longer pulls .qvf row data at all (see
        # qlik_extract/extractor.py), only structure. Every table's M is the
        # parameterized CSV-folder-or-SQL query (see csv_m.py) — point
        # SourceDataPath at wherever the real data actually lives (a CSV
        # export you provide yourself, a network share, ...) or fill in
        # SqlServer/SqlDatabase in Power BI Desktop's Manage Parameters to
        # read the same table names straight from SQL instead.
        column_names_here = {c["name"] for c in columns}
        table_chr_concat = chr_concat_by_table.get(table_name, {})
        table_dual_key = dual_key_by_table.get(table_name, {})
        table_applymap = applymap_by_table.get(table_name, {})

        def _spec_sources_present(spec: dict) -> bool:
            if spec["func"] == "ConcatKey":
                # field_a/field_b are trusted from the regex match against
                # the script itself, NOT required to already be in this
                # table's own declared columns — Qlik can (and here does)
                # consume a field purely to build a computed one without
                # ever exposing it as an output field of the table (see
                # csv_m.py's _extra_helper_columns, which selects them from
                # the source file as scratch inputs regardless).
                return True
            if spec["func"] == "ApplyMapSubfield":
                # Resolve the map it references against the mapping tables
                # actually found in the script, filling in the key/value
                # field names and source filename _computed_column_expr /
                # csv_m._mapping_dict_statement need — a map name that
                # doesn't match any `mapping LOAD` block found is left
                # unresolved (falls through to the normal "loads blank"
                # behavior) rather than guessed at.
                mapping = mapping_tables.get(spec["map_name"].casefold())
                if not mapping or spec["source"] not in column_names_here:
                    return False
                map_filenames = source_files_by_table.get(mapping["table_label"])
                if not map_filenames:
                    return False
                spec["map_key_field"] = mapping["key_field"]
                spec["map_value_field"] = mapping["value_field"]
                spec["map_filename"] = map_filenames[0]
                return True
            return spec["source"] in column_names_here

        computed = {}
        for c in columns:
            spec = (
                month_derived.get(c["name"].casefold())
                or table_chr_concat.get(c["name"].casefold())
                or table_dual_key.get(c["name"].casefold())
                or table_applymap.get(c["name"].casefold())
            )
            if spec and _spec_sources_present(spec):
                computed[c["name"]] = spec
        if computed:
            def _describe(s):
                if s["func"] == "ChrConcat":
                    return f'Chr({s["code"]}) & {s["source"]}' if s["prefix"] else f'{s["source"]} & Chr({s["code"]})'
                if s["func"] == "ApplyMapSubfield":
                    return f'SubField(ApplyMap(\'{s["map_name"]}\', {s["source"]}, \'{s["default"]}\'), \'{s["sep"]}\', {s["part_index"] + 1})'
                if s["func"] == "ConcatKey":
                    return f'{s["field_a"]} & "{s["sep"]}" & {s["field_b"]}'
                return f"{s['func']}({s['source']})"
            names = ", ".join(f"{n} = {_describe(s)}" for n, s in computed.items())
            print(f"[build] {table_name}: computing {names} in Power Query (per the Qlik LOAD script) "
                  f"instead of reading them from the CSV — they were never in the source file either")

        # A field Qlik's own script renames (`RiskBand AS PredictedRiskBand`)
        # is genuinely absent from a raw source file under its NEW name — the
        # file only ever had "RiskBand" — so Table.SelectColumns by the
        # model's name alone would load it as null even though the data is
        # right there under its other name. {target: source} tells the M
        # generator which raw column to actually pull, then rename.
        renames = {}
        for c in columns:
            if c["name"] in computed:
                continue
            source_name = renamed_by_table.get(table_name, {}).get(c["name"].casefold())
            if source_name:
                renames[c["name"]] = source_name
        if renames:
            print(f"[build] {table_name}: {', '.join(f'{s} -> {t}' for t, s in renames.items())} "
                  f"(per the Qlik LOAD script's own AS-rename — selecting by the source file's original "
                  f"name, then renaming, instead of expecting the file to already use the new name)")

        if table_name in inline_tables:
            # LOAD * INLINE [...] — literal rows typed straight into the
            # script, no source file at all. Embed them in the M directly
            # (see csv_m.generate_inline_partition_m) instead of expecting
            # a "{table}.csv" that was never going to exist ("Could not
            # find file").
            print(f"[build] {table_name}: LOAD ... INLINE in the Qlik script (no source file) — "
                  f"embedding its {len(inline_tables[table_name])} row(s) directly in the M instead of "
                  f"expecting a CSV file for it")
            tables[table_name] = {
                "columns": columns,
                "m_expression": generate_inline_partition_m(columns, inline_tables[table_name]),
                "computed": {}, "renames": {}, "multi_source_files": None, "csv_filename": None,
                "inline_rows": inline_tables[table_name],
            }
            continue

        # A `left join(Table) LOAD ... RESIDENT Other ... GROUP BY ...`
        # column (see _detect_join_resident_aggregations) has no source
        # file of its own — exclude it from what the base load selects
        # from CSV/SQL (it would just load null and collide with the
        # joined-in value added below), same idea as `computed`.
        join_spec = join_agg_by_table.get(table_name)
        join_agg_aliases = {a["alias"] for a in join_spec["aggregations"]} if join_spec else set()
        base_columns = [c for c in columns if c["name"] not in join_agg_aliases]

        # Use the script's own FROM filename(s) whenever they were found —
        # NEVER assume the source file is named after the table. Only fall
        # back to guessing "{table}.csv" when the script parse found no
        # FROM clause at all for this table (e.g. it's RESIDENT/INLINE-only
        # and genuinely has no file of its own).
        detected_files = source_files_by_table.get(table_name)
        multi_source_files = detected_files if detected_files and len(detected_files) > 1 else None
        csv_filename = None
        if multi_source_files:
            print(f"[build] {table_name}: built from {len(multi_source_files)} source files in the Qlik "
                  f"script, in this order — {', '.join(multi_source_files)} — combining all of them "
                  f"(Table.Combine) instead of loading only the first")
            m_expression = generate_combined_partition_m(
                table_name, multi_source_files, base_columns,
                source_ref=SOURCE_DATA_PARAM_NAME, sql=_SQL_PARTITION_REFS, computed=computed, renames=renames,
            )
        else:
            csv_filename = detected_files[0] if detected_files else f"{_safe(table_name)}.csv"
            if detected_files and csv_filename.casefold() != f"{_safe(table_name)}.csv".casefold():
                print(f"[build] {table_name}: source file is '{csv_filename}' per the Qlik script "
                      f"(not '{_safe(table_name)}.csv')")
            m_expression = generate_partition_m(
                table_name, csv_filename, base_columns,
                source_ref=SOURCE_DATA_PARAM_NAME, sql=_SQL_PARTITION_REFS, computed=computed, renames=renames,
            )

        if join_spec:
            print(f"[build] {table_name}: joining {', '.join(sorted(join_agg_aliases))} from a "
                  f"RESIDENT {join_spec['source_table']} GROUP BY aggregate (per the Qlik script's own "
                  f"LEFT JOIN) — grouping {join_spec['source_table']}'s own query and merging it on "
                  f"{', '.join(g['alias'] for g in join_spec['group_by'])} instead of leaving "
                  f"{', '.join(sorted(join_agg_aliases))} blank")
            m_expression = wrap_with_left_join_aggregation(m_expression, join_spec)

        tables[table_name] = {
            "columns": columns, "m_expression": m_expression, "computed": computed, "renames": renames,
            "multi_source_files": multi_source_files, "csv_filename": csv_filename, "join_spec": join_spec,
        }

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

    return (tables, measures_by_table, calc_cols_by_table, hierarchies_by_table, original_measure_table,
            required_relationship_orientations)


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
        # No dependency on an extracted CSV existing (extraction never
        # creates one) — regenerate from the table's own (now-corrected)
        # column list, same as the initial generation in
        # _assemble_semantic_inputs above.
        if table.get("inline_rows") is not None:
            table["m_expression"] = generate_inline_partition_m(table["columns"], table["inline_rows"])
        elif SOURCE_DATA_PARAM_NAME in table.get("m_expression", ""):
            join_spec = table.get("join_spec")
            join_agg_aliases = {a["alias"] for a in join_spec["aggregations"]} if join_spec else set()
            base_columns = [c for c in table["columns"] if c["name"] not in join_agg_aliases]
            multi_source_files = table.get("multi_source_files")
            if multi_source_files:
                m_expression = generate_combined_partition_m(
                    tname, multi_source_files, base_columns,
                    source_ref=SOURCE_DATA_PARAM_NAME, sql=_SQL_PARTITION_REFS,
                    computed=table.get("computed"), renames=table.get("renames"),
                )
            else:
                m_expression = generate_partition_m(
                    tname, table.get("csv_filename") or f"{_safe(tname)}.csv", base_columns,
                    source_ref=SOURCE_DATA_PARAM_NAME, sql=_SQL_PARTITION_REFS,
                    computed=table.get("computed"), renames=table.get("renames"),
                )
            if join_spec:
                m_expression = wrap_with_left_join_aggregation(m_expression, join_spec)
            table["m_expression"] = m_expression


_TABLE_QUALIFIED_REF_RE = re.compile(r"(?:'([^']+)'|(\b[A-Za-z_]\w*\b))\[([^\[\]]+)\]")

def _dax_protected_spans(expr: str) -> list[tuple[int, int]]:
    """Every `[...]` column/measure reference, `"..."` string literal, and
    `'...'` quoted table name in a DAX expression, as (start, end) spans —
    a single left-to-right scan rather than one regex per bracket style, so
    it can't miss a combination (a measure name matching text inside ANY of
    these was found to corrupt real DAX in practice: inside an existing
    `[Multi Word Measure]`, inside a `"string literal"`, and inside a
    `'Quoted Table Name'` — three different delimiters, same underlying
    mistake of only checking the single character immediately before a
    candidate match instead of "is this position inside a span at all").
    `''`/`""` doubled-quote escaping is honored so an embedded quote doesn't
    end the span early."""
    spans, i, n = [], 0, len(expr)
    while i < n:
        ch = expr[i]
        if ch == "[":
            j = expr.find("]", i + 1)
            if j == -1:
                break
            spans.append((i, j + 1))
            i = j + 1
        elif ch in ("'", '"'):
            j = i + 1
            while j < n:
                if expr[j] == ch:
                    if j + 1 < n and expr[j + 1] == ch:
                        j += 2
                        continue
                    break
                j += 1
            spans.append((i, min(j + 1, n)))
            i = j + 1
        else:
            i += 1
    return spans


def _wrap_bare_measure_refs(
    measures_by_table: dict[str, list[dict]],
    calc_cols_by_table: dict[str, list[dict]],
    original_measure_table: dict[str, tuple[str, str]],
) -> None:
    """A DAX measure reference is only valid in square brackets — `[Name]`,
    not bare `Name` — but the LLM occasionally writes the bracket-less form
    (seen: `PredictARFact[PredictedCollectionWeek] - vDunningShift -
    vEarlyPaymentDiscountPct`, missing brackets on both variables, while 4
    other measures in the same app reference the very same names correctly
    with brackets). Power BI's response to the bare form is a hard
    calculation error — "Failed to resolve name '<X>'. It is not a valid
    table, variable, or function name." — which can also cascade into
    unrelated-looking errors on other measures that read the same table via
    RELATED(), since a table with a calculation error effectively has no
    usable rows.

    Deterministic and narrowly scoped to genuinely bare occurrences: any
    text already inside an existing `[...]` span (found first, kept intact)
    is never touched even if a measure name happens to be a substring of
    what's in there — e.g. `[TOUR ADHERENCE]` must be left exactly as-is,
    not partly reprocessed just because a DIFFERENT, separately-registered
    measure happens to be named plain "Adherence". Measure names are tried
    longest-first so a bare multi-word name matches as one whole phrase
    rather than tripping on a shorter name that happens to be one of its
    words. The same protection covers `"..."` string literals and `'...'`
    quoted table names — a real measure named "Visited" once matched the
    word "Visited" inside `Fact_Sales[Status] = "Visited"` (an ordinary text
    comparison, nothing to do with the measure) and corrupted it into
    `"[Visited]"`, silently breaking that comparison against real data with
    no error anywhere; a measure named "Achievement %" similarly matched
    inside the quoted table name `'Sales Achievement %'`, corrupting it to
    `'Sales [Achievement %]'`."""
    # A "measure name" with no letters at all (seen: a constant-placeholder
    # measure literally named "1") is indistinguishable from an ordinary
    # numeric literal appearing anywhere else in any DAX expression — trying
    # to auto-bracket it would turn every bare "1" in the model (any
    # addition, any MAX(0, ...), any IF(...,1,0)) into a self-referencing
    # `[1]`. Skip anything that can't be told apart from plain DAX syntax
    # this way; a genuine bare reference to a number-only-named measure has
    # to be fixed by hand, but that's far safer than corrupting every
    # numeric literal in the model.
    names = sorted(
        {v[1] for v in original_measure_table.values() if re.search(r"[A-Za-z]", v[1])},
        key=len, reverse=True,
    )
    if not names:
        return
    bare_name_re = re.compile(
        r"(?<![\[\w'])(" + "|".join(re.escape(n) for n in names) + r")(?![\]\w])", re.IGNORECASE,
    )

    def fix(expr: str) -> str:
        if not expr:
            return expr
        protected_spans = _dax_protected_spans(expr)
        def is_protected(pos: int) -> bool:
            return any(start <= pos < end for start, end in protected_spans)
        pieces, cursor = [], 0
        for m in bare_name_re.finditer(expr):
            if is_protected(m.start()):
                continue
            found = original_measure_table.get(m.group(1).casefold())
            real_name = found[1] if found else m.group(1)
            pieces.append(expr[cursor:m.start()])
            pieces.append(f"[{real_name}]")
            cursor = m.end()
        if not pieces:
            return expr
        pieces.append(expr[cursor:])
        return "".join(pieces)

    for ms in measures_by_table.values():
        for mrow in ms:
            fixed = fix(mrow.get("expression", ""))
            if fixed != mrow.get("expression", ""):
                print(f"[build] wrapped bare measure reference(s) in '{mrow['name']}' with [] "
                      f"(DAX requires brackets around a measure name; the LLM wrote it bare)")
            mrow["expression"] = fixed
    for cs in calc_cols_by_table.values():
        for crow in cs:
            fixed = fix(crow.get("expression", ""))
            if fixed != crow.get("expression", ""):
                print(f"[build] wrapped bare measure reference(s) in calculated column '{crow['name']}' with [] "
                      f"(DAX requires brackets around a measure name; the LLM wrote it bare)")
            crow["expression"] = fixed


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
        for visual in page.get("visuals", []):
            _walk(visual)


_PLACEHOLDER_CONST_RE = re.compile(
    r"=?\s*Sum\(\s*(-?\d+(?:\.\d+)?)\s*\)|value\s*=\s*(-?\d+(?:\.\d+)?)", re.IGNORECASE,
)


def _collect_measure_properties(node, out: set[str]) -> None:
    """Every `Measure`/`Column` Property name referenced anywhere inside one
    visual — Property may already be hoisted to its canonical sibling
    position or still nested in Expression (this runs before
    _normalize_field_node_shapes), so check both."""
    if isinstance(node, dict):
        for key in ("Measure", "Column"):
            inner = node.get(key)
            if isinstance(inner, dict):
                prop = inner.get("Property") or inner.get("Expression", {}).get("Property")
                if prop:
                    out.add(prop)
        for v in node.values():
            _collect_measure_properties(v, out)
    elif isinstance(node, list):
        for v in node:
            _collect_measure_properties(v, out)


def _synthesize_placeholder_constant_measures(
    pages: list[dict],
    tables: dict[str, dict],
    measures_by_table: dict[str, list[dict]],
    original_measure_table: dict[str, tuple[str, str]],
) -> None:
    fallback_table = next((t for t in tables if not tables[t].get("is_calculated")), "")
    if not fallback_table:
        return
    for page in pages:
        for visual in page.get("visuals", []):
            m = _PLACEHOLDER_CONST_RE.search(visual.get("notes", ""))
            if not m:
                continue
            value = m.group(1) or m.group(2)
            props: set[str] = set()
            _collect_measure_properties(visual, props)
            for prop in props:
                if prop.casefold() in original_measure_table:
                    continue
                measures_by_table.setdefault(fallback_table, []).append({
                    "name": prop, "expression": value, "is_hidden": True,
                })
                original_measure_table[prop.casefold()] = (fallback_table, prop)
                print(f"[build] synthesized constant measure '{prop}' = {value} "
                      f"(Qlik KPI was a hard-coded Sum({value}) placeholder, per report_visuals' own notes)")


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

            for key, v in node.items():
                if key == "Aggregation" and isinstance(agg, dict):
                    # Already fully resolved directly above — do NOT also
                    # recurse into it. Aggregation.Expression is itself a
                    # {"Column": {...}} dict, so plain recursion would hand
                    # it to the generic `column = node.get("Column")` branch
                    # above as if it were a top-level field binding — which
                    # runs the measure-first check and can hijack an
                    # explicitly-aggregated real column (e.g. a
                    # countDistinct over InvoiceID) into a Measure reference
                    # just because some unrelated measure's label
                    # aggressively normalizes the same way ("Count of
                    # InvoiceID" -> "invoiceid"). A Measure has no business
                    # being wrapped in an Aggregation node either way, and
                    # _entity_and_property_of_field's Aggregation case only
                    # ever looks for a Column, so the hijacked projection
                    # would read as unresolved and get pruned.
                    continue
                _walk(v, visual_id)
        elif isinstance(node, list):
            for v in node:
                _walk(v, visual_id)

    for page in pages:
        for visual in page.get("visuals", []):
            _walk(visual, visual.get("name"))

    _fix_query_refs(pages)
    _prune_unresolved_projections(pages)


# PBIR's projection Aggregation node stores the aggregate as an integer
# QueryAggregateFunction code, not a name — report_visuals instead sometimes
# emits a plain top-level `"aggregate": "<name>"` string sibling of `field`
# on the projection, which isn't a property PBIR's schema allows anywhere
# ("An additional property 'aggregate' was included ..."). Map the name it
# used to the real code so the aggregation's MEANING survives, not just so
# the extra property goes away.
_AGGREGATE_NAME_TO_CODE = {
    "sum": 0, "average": 1, "avg": 1, "distinctcount": 2, "countdistinct": 2,
    "min": 3, "max": 4, "count": 5, "median": 6, "stdev": 7, "standarddeviation": 7,
    "variance": 8,
}
# Every stray-key spelling seen so far for this same mistake ("aggregate" one
# run, "aggregation" the next) — see _normalize_field_node_shapes.
_AGGREGATE_KEY_NAMES = ("aggregate", "aggregation", "agg", "aggFunc", "aggregateFunction")


def _normalize_field_node_shapes(pages: list[dict]) -> None:
    """Hoist a `Property` that the LLM nested inside `Expression` (next to
    `SourceRef`) up to its canonical position as a sibling of `Expression`,
    on every Measure/Column node anywhere in the page tree. Idempotent: a
    node that already has a sibling Property is left alone (and any stray
    nested copy removed so the two can't disagree later).

    Also converts a projection's stray sibling `"aggregate": "<name>"` (not a
    property PBIR's schema recognizes anywhere) into the real `Aggregation`
    wrapper around its `Column`, so e.g. "count-distinct of InvoiceID" keeps
    working as an actual distinct-count instead of silently becoming a plain
    (non-aggregated) column reference or being rejected outright.

    Also collapses an accidentally DOUBLY-NESTED Column — a whole extra
    `{"Column": {"Expression": {...}, "Property": ...}}` wrapped inside the
    outer Column's own `Expression`, instead of a plain `SourceRef` there —
    down to the innermost real `Expression`/`Property` pair. Left as-is, a
    later resolve step's `setdefault("Expression", {})` finds the Expression
    dict already non-empty (holding the wrong nested Column) and ADDS a
    sibling `SourceRef` next to it rather than replacing it, leaving
    `Expression` with two competing children — invalid PBIR ("must be
    provided" one-of-these-properties, not more than one).
    """
    def _flatten_nested_column(inner: dict) -> dict:
        while isinstance(inner.get("Expression"), dict) and isinstance(inner["Expression"].get("Column"), dict):
            inner = inner["Expression"]["Column"]
        return inner

    def walk(node):
        if isinstance(node, dict):
            for key in ("Measure", "Column"):
                inner = node.get(key)
                if isinstance(inner, dict):
                    flattened = _flatten_nested_column(inner)
                    if flattened is not inner:
                        node[key] = inner = flattened
                    expr = inner.get("Expression")
                    if isinstance(expr, dict) and "Property" in expr:
                        inner.setdefault("Property", expr["Property"])
                        del expr["Property"]

            # The LLM's own name for this stray key drifts between runs
            # ("aggregate" one time, "aggregation" the next, plausibly
            # others) — check every synonym rather than one literal string,
            # so a future rename doesn't reopen this exact bug again.
            agg_key = next((k for k in _AGGREGATE_KEY_NAMES if k in node), None)
            if agg_key and isinstance(node.get("field"), dict):
                agg_name = str(node.pop(agg_key)).strip().casefold()
                column = node["field"].get("Column")
                if isinstance(column, dict):
                    code = _AGGREGATE_NAME_TO_CODE.get(agg_name)
                    if code is not None:
                        node["field"] = {"Aggregation": {"Expression": {"Column": column}, "Function": code}}
                    else:
                        print(f"[build] WARNING: dropping unrecognized projection aggregate '{agg_name}' "
                              f"(queryRef='{node.get('queryRef')}') — binding as a plain column instead")

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


def _visual_has_system_field(visual: dict) -> bool:
    """True if any projection in this visual's queryState binds a Qlik
    SYSTEM field — $Table, $Field, $Rows, $Info, $Occurrence, and the like
    (Qlik's own built-in introspection fields, exposed by things like its
    "Data Model Viewer" sheet). These describe the Qlik app's OWN metadata,
    not real data, so no equivalent table/column exists (or ever will) in
    the Power BI model to bind them to — report_visuals recognizes them as
    such in its own notes but still points the binding at a fabricated
    table (seen: "Model"), which Power BI reports as fields that need
    fixing. Scans for a `"Property"` value anywhere in the query state
    (rather than assuming one particular field-node shape) since this runs
    before the shape normalizer that would otherwise standardize it."""
    query_state = visual.get("visual", {}).get("query", {}).get("queryState", {})
    def scan(node):
        if isinstance(node, dict):
            prop = node.get("Property")
            if isinstance(prop, str) and prop.startswith("$"):
                return True
            return any(scan(v) for v in node.values())
        if isinstance(node, list):
            return any(scan(v) for v in node)
        return False
    return scan(query_state)


def _replace_system_field_visuals(pages: list[dict]) -> None:
    for page in pages:
        for visual in page.get("visuals", []):
            if not _visual_has_system_field(visual):
                continue
            old_type = visual.get("visual", {}).get("visualType", "visual")
            print(f"[build] '{visual.get('name', '?')}' ({old_type}) is bound to a Qlik system field "
                  f"($Table/$Rows/etc. — Qlik's own app introspection data, not real fields) — replacing "
                  f"with a placeholder textbox, the same fallback used for any other Qlik object Power BI "
                  f"has no way to reproduce")
            visual["visual"] = {
                "visualType": "textbox",
                "objects": {
                    "general": [{"properties": {"paragraphs": [{"textRuns": [{
                        "value": "Qlik system-field visual (e.g. its Data Model Viewer) — "
                                 "not reproducible as bound data in Power BI"
                    }]}]}}]
                },
            }


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

            # PBIR: a textbox's rich text belongs at
            # objects.general[0].properties.paragraphs[...], not as its own
            # top-level objects.paragraphs object — report_visuals instead
            # emits `objects: {"paragraphs": [{"text": "..."}]}`, which is
            # both the wrong location AND missing the "properties" wrapper
            # every PBIR object instance requires ("Required property
            # 'properties' was not included" / "An additional property
            # 'text' was included"). Relocate it to the real shape instead
            # of leaving objects.paragraphs behind.
            objects = visual_obj.get("objects")
            if isinstance(objects, dict):
                bad_paragraphs = objects.pop("paragraphs", None)
                if isinstance(bad_paragraphs, list):
                    text_runs = [
                        {"textRuns": [{"value": str(p["text"])}]}
                        for p in bad_paragraphs
                        if isinstance(p, dict) and "text" in p
                    ]
                    if text_runs:
                        general = objects.setdefault("general", [])
                        if not general or not isinstance(general[0], dict):
                            general.insert(0, {"properties": {}})
                        general[0].setdefault("properties", {})["paragraphs"] = text_runs

            # PBIR: every `visual.objects.<name>` value must be a LIST of
            # {"properties": {...}} entries (real visual.json: "objects":
            # {"title": [{"properties": {...}}]}). report_visuals sometimes
            # emits a bare dict for one (seen: `general`, `text`) —
            # "Property /visual/objects/<name> was not provided as the
            # correct type". Coerce to the list shape.
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


def _orient_relationships_for_related(
    relationships: list[dict], required_orientations: list[tuple[str, str]],
) -> list[dict]:
    """Flip a relationship's from/to (many/one) direction when a calculated
    table's own DAX needs it the other way — see the
    required_relationship_orientations comment in _assemble_semantic_inputs.
    `(many_table, one_table)`: if an existing relationship connects the same
    two tables but with the direction reversed, swap its from/to so the
    "many" side really is the from_table (TMDL's implicit default then
    renders RELATED()'s required direction correctly, with no cardinality
    override needed)."""
    for many_table, one_table in required_orientations:
        for rel in relationships:
            if rel["from_table"] == one_table and rel["to_table"] == many_table:
                rel["from_table"], rel["to_table"] = rel["to_table"], rel["from_table"]
                rel["from_column"], rel["to_column"] = rel["to_column"], rel["from_column"]
                print(f"[build] reoriented relationship {one_table}<->{many_table} to {many_table} (many) -> "
                      f"{one_table} (one) — a calculated table's own DAX needs RELATED({one_table}[...]) while "
                      f"iterating {many_table}, which requires {many_table} to be the many side regardless of "
                      f"which table looks more \"fact-like\" for the model's other relationships")
                break
    return relationships


def _drop_relationships_into_related_calc_tables(relationships: list[dict], tables: dict[str, dict]) -> list[dict]:
    """A statically-declared TMDL relationship pointing at (or from) a DAX
    calculated table's column fails Power BI Desktop's static, pre-refresh
    project-load validation — "Relationship '<guid>' uses an invalid column
    ID <n>" — REGARDLESS of cardinality/crossFilter settings and regardless
    of whether the calculated table's own expression depends on another
    relationship (RELATED()) or is fully self-contained (a plain
    CALENDARAUTO() calendar table hits this exactly the same way — a
    calculated table's columns simply don't exist yet at the point Desktop
    validates the static relationship list, whatever the DAX behind them).
    Confirmed across two different apps/tables (DisputeCount90d, which uses
    RELATED(); MasterCalendar, which doesn't) before landing on this as the
    real, general rule rather than the narrower RELATED()-only guess this
    function started as.

    Dropping the relationship here doesn't lose the connection permanently —
    it just can't be pre-declared in the static TMDL. Add it by hand in
    Power BI Desktop's Model view (drag the key column across) once the
    project is open, at which point the calculated table's schema already
    exists and Desktop resolves it correctly."""
    risky_tables = {name for name, t in tables.items() if t.get("is_calculated")}
    if not risky_tables:
        return relationships
    kept, dropped = [], []
    for rel in relationships:
        if rel["from_table"] in risky_tables or rel["to_table"] in risky_tables:
            dropped.append(rel)
        else:
            kept.append(rel)
    for rel in dropped:
        calc_side = rel['from_table'] if rel['from_table'] in risky_tables else rel['to_table']
        print(f"[build] NOTE: not declaring the relationship {rel['from_table']}[{rel['from_column']}] <-> "
              f"{rel['to_table']}[{rel['to_column']}] in TMDL — one side ('{calc_side}') is a DAX calculated "
              f"table, and Power BI Desktop's static project-load validation can't resolve a relationship "
              f"into one ('uses an invalid column ID'), regardless of cardinality or what the calculated "
              f"table's own expression does. Add it by hand in Model view after opening the .pbip — Desktop "
              f"creates it correctly once the calculated table's schema exists.")
    return kept


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
