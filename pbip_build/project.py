"""Assemble converted artifacts into a *.pbip project (Report + SemanticModel)
under output/<app_name>/, then compile it to a real .pbix."""

from __future__ import annotations

import glob
import json
import os
import re

from .semantic_model import write_semantic_model
from .report import write_report
from .pbix_compile import compile_pbix
from .csv_m import generate_csv_partition_m
from .infer_relationships import infer_relationships
from .what_if_params import detect_what_if_parameters, generate_range_m

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXTRACTED_ROOT = os.path.join(ROOT, "extracted")
CONVERTED_ROOT = os.path.join(ROOT, "converted")
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
    _synthesize_adhoc_measures(pages, tables, measures_by_table, original_measure_table, label_to_agg)

    # A Qlik "variable input" slider (e.g. qlik-variable-input) lets the user
    # manually drive a variable's value within a numeric range — other
    # measures reference it by the slider's display label the same way they'd
    # reference a real measure. Give it a real Power BI equivalent: a small
    # parameter table holding the range plus a SELECTEDVALUE() measure named
    # after that same label, so those existing references resolve as-is.
    _apply_what_if_parameters(extracted_dir, tables, measures_by_table, original_measure_table)

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
    _fix_measure_self_references(measures_by_table, calc_cols_by_table, tables)

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
    )

    _fix_field_and_measure_refs(pages, tables, calc_cols_by_table, original_measure_table, rename_map, label_to_field)
    report_dir = os.path.join(project_dir, f"{app_name}.Report")
    write_report(report_dir, pages, app_name=app_name)

    _write_pbip_file(project_dir, app_name)

    pbix_path = os.path.join(project_dir, f"{app_name}.pbix")
    compile_pbix(project_dir, pbix_path)
    return pbix_path


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


def _assemble_semantic_inputs(extracted_dir: str, converted_dir: str):
    raw_data_model = _load_json(extracted_dir, "data_model.json")
    converted_data_model = _load_json(converted_dir, "data_model.converted.json")
    column_types = converted_data_model.get("column_types", {})

    tables: dict[str, dict] = {}
    for raw_table in raw_data_model.get("tables", []):
        table_name = raw_table.get("qName") or raw_table.get("name")
        if not table_name:
            continue
        raw_fields = raw_table.get("qFields", raw_table.get("fields", []))
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
        csv_path = os.path.join(extracted_dir, "data", f"{_safe(table_name)}.csv")
        if os.path.exists(csv_path):
            # Real data extracted straight from the Qlik app takes priority
            # over the LLM's best-effort reconstruction of the original load
            # script, which usually points at a source (file path / DB) only
            # reachable from the machine that authored the .qvf.
            m_expression = generate_csv_partition_m(csv_path, columns)
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
    for m in _load_json(converted_dir, "measures.converted.json").get("measures", []):
        table = m.get("table", "")
        measures_by_table.setdefault(table, []).append(m)
        original_measure_table[m["name"].casefold()] = (table, m["name"])

    for v in _load_json(converted_dir, "variables.converted.json").get("variables", []):
        if v.get("target") == "dax_measure" and v.get("dax") and v["dax"].get("expression"):
            table = v.get("table", "")
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

    return tables, measures_by_table, calc_cols_by_table, hierarchies_by_table, original_measure_table


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
) -> None:
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
        print(f"[build] added what-if parameter '{table_name}' (range {param['min']}-{param['max']} "
              f"step {param['step']}, default {param['default']}) from Qlik variable '{param['variable']}'")


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

    for m in data.get("measures", []):
        table, name, expr = m.get("table"), m.get("name"), m.get("expression")
        if not (table and name and expr and table in tables):
            continue
        measures_by_table.setdefault(table, []).append({
            "name": name, "expression": expr,
            "format_string": m.get("format_string"), "is_hidden": m.get("is_hidden", False),
        })
        original_measure_table[name.casefold()] = (table, name)
        print(f"[build] converted ad-hoc chart expression -> measure '{name}' on '{table}'")

    for item in data.get("items", []):
        table, name = item.get("table"), item.get("name")
        if not (table and name and table in tables):
            continue
        if item.get("type") == "calculated_column" and item.get("expression"):
            calc_cols_by_table.setdefault(table, []).append({"name": name, "expression": item["expression"]})
            label_to_field.setdefault(name.casefold(), name)
            print(f"[build] converted ad-hoc chart expression -> calculated column '{name}' on '{table}'")


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


def _fix_field_and_measure_refs(
    pages: list[dict],
    tables: dict[str, dict],
    calc_cols_by_table: dict[str, list[dict]],
    original_measure_table: dict[str, tuple[str, str]],
    rename_map: dict[tuple[str, str], str],
    label_to_field: dict[str, str],
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
    column_owner: dict[str, list[str]] = {}
    for table_name, table in tables.items():
        for col in table["columns"]:
            column_owner.setdefault(col["name"].casefold(), []).append(table_name)
    for table_name, cols in calc_cols_by_table.items():
        for col in cols:
            column_owner.setdefault(col["name"].casefold(), []).append(table_name)
    normalized_column_index: dict[str, str] = {}
    for key in column_owner:
        normalized_column_index.setdefault(_normalize_label(key), key)

    def resolve_column_entity(prop: str, guessed: str) -> str | None:
        if not prop:
            return None
        key = prop.casefold()
        owners = column_owner.get(key)
        if not owners:
            fallback_key = normalized_column_index.get(_normalize_label(key))
            if fallback_key is not None:
                owners = column_owner.get(fallback_key)
        if not owners:
            return None
        if guessed in owners:
            return guessed
        if len(owners) > 1:
            print(f"[build] ambiguous field '{prop}' exists on {owners}; using '{owners[0]}'")
        return owners[0]

    # Independent LLM calls transcribing the same raw Qlik label can drift
    # slightly (e.g. report_visuals renders "Achv %" while the ad-hoc
    # measure conversion — a separate call over the same source text —
    # renders "Achv. %"). An exact casefold match won't bridge that; a
    # punctuation/whitespace-stripped index will, as a fallback only (exact
    # match always wins first, so this never masks a genuinely different
    # name that just happens to normalize the same).
    normalized_measure_index: dict[str, str] = {}
    for key in original_measure_table:
        normalized_measure_index.setdefault(_normalize_label(key), key)

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
            return None
        table, true_original = found
        final_name = rename_map.get((table, true_original), true_original)
        return table, final_name

    def _walk(node):
        if isinstance(node, dict):
            measure = node.get("Measure")
            if isinstance(measure, dict):
                resolved = resolve_measure(measure.get("Property"))
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
                if measure_resolved:
                    del node["Column"]
                    node["Measure"] = {
                        "Expression": {"SourceRef": {"Entity": measure_resolved[0]}},
                        "Property": measure_resolved[1],
                    }
                else:
                    entity = resolve_column_entity(prop, guessed)
                    if entity:
                        column.setdefault("Expression", {}).setdefault("SourceRef", {})["Entity"] = entity
                    elif prop and prop.casefold() in label_to_field:
                        real_field = label_to_field[prop.casefold()]
                        entity2 = resolve_column_entity(real_field, guessed)
                        if entity2:
                            column["Property"] = real_field
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
                    entity = resolve_column_entity(inner.get("Property"), guessed)
                    if entity:
                        inner.setdefault("Expression", {}).setdefault("SourceRef", {})["Entity"] = entity

            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for v in node:
                _walk(v)

    for page in pages:
        _walk(page)


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
            visual_type = visual.get("visual", {}).get("visualType")
            query_state = visual.get("visual", {}).get("query", {}).get("queryState", {})
            if not isinstance(query_state, dict):
                continue
            for role, value in list(query_state.items()):
                if isinstance(value, list):
                    query_state[role] = {"projections": value}

            # A card/multiRowCard's single data role is named "Values" in
            # Power BI's built-in Card visual — a projection filed under "Y"
            # instead (the role name for axis-based charts) renders blank
            # with no visible error, a common cause of "the KPI isn't
            # showing." Rename rather than trust every conversion to use
            # the exact role name for this specific visual type.
            if visual_type in _CARD_TYPES and "Y" in query_state and "Values" not in query_state:
                query_state["Values"] = query_state.pop("Y")


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
