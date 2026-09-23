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
import sys
import tempfile

from app.setting import settings

from .semantic_model import write_semantic_model
from .report import write_report
from .theme import load_theme, build_custom_theme
from .pbix_compile import compile_pbix
from .csv_m import (
    generate_partition_m, generate_combined_partition_m, generate_inline_partition_m,
    wrap_with_left_join_aggregation, generate_groupby_count_partition_m,
)
from .infer_relationships import infer_relationships
from .what_if_params import detect_what_if_parameters, generate_range_m
from .scenario_buttons import detect_variable_scenarios, generate_scenario_table_m
from .bookmarks import write_bookmarks

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


# Every one of these substrings, found (case-insensitively) anywhere in a
# "[build]"/"[convert]"/"[extract]" log line, marks that line as something a
# PERSON needs to look at or decide on by hand — a gap the pipeline could
# not close automatically, as opposed to the majority of build-log lines
# (which just narrate a decision the pipeline made confidently and
# correctly, e.g. "source file is X per the Qlik script"). Kept as one
# explicit list rather than inferring it structurally, since "worth a
# person's attention" is inherently a judgment call the code throughout
# this file already makes at each print() call site by choosing its own
# wording — this just recognizes that wording after the fact, generically,
# so a new manual-attention message anywhere in the pipeline is picked up
# automatically the moment its own wording includes one of these markers
# (all genuinely manual-follow-up messages in this codebase already do),
# with no separate registration step required.
_MANUAL_REVIEW_MARKERS = (
    "warning", "note:", "skipped table", "isn't a real table",
    "bound to a qlik system field", "marked inactive",
    "ambiguous field", "fields that need to be fixed", "not found —",
    "dropping relationship",
)


class _ManualReviewCapture:
    """Tees stdout during a build: every printed line still reaches the
    real console exactly as before (nothing about the live build log
    changes), while any line matching _MANUAL_REVIEW_MARKERS is ALSO kept
    here so build_project can write it to a standalone file afterward —
    see _write_manual_review_file."""

    def __init__(self, real_stdout):
        self._real = real_stdout
        self.notes: list[str] = []
        self._line_buffer = ""

    def write(self, text: str) -> int:
        try:
            self._real.write(text)
        except UnicodeEncodeError:
            # Windows' console stdout is often cp1252, which can't encode
            # some Unicode characters a dependency (e.g. pbip_compiler's own
            # progress prints, which use '→') writes directly — that's
            # not a build failure, just a terminal encoding limitation, so
            # degrade to a safe ASCII substitution instead of crashing the
            # whole build over a cosmetic console character.
            encoding = getattr(self._real, "encoding", None) or "ascii"
            self._real.write(text.encode(encoding, errors="replace").decode(encoding))
        self._line_buffer += text
        while "\n" in self._line_buffer:
            line, self._line_buffer = self._line_buffer.split("\n", 1)
            lowered = line.casefold()
            if any(marker in lowered for marker in _MANUAL_REVIEW_MARKERS):
                self.notes.append(line)
        return len(text)

    def flush(self) -> None:
        self._real.flush()


def _write_manual_review_file(project_dir: str, app_name: str, notes: list[str]) -> None:
    """Writes output/<app_name>/MANUAL_REVIEW.md — a standalone checklist of
    every build-time gap this run couldn't close automatically (unresolved
    fields, skipped Qlik-internal tables, relationships/hierarchies left
    out of TMDL, placeholder fallbacks, ambiguous field choices, etc.),
    pulled straight from the same log lines already printed during the
    build — see _ManualReviewCapture. Always written, even with zero notes
    (a short "nothing found" file), so there's one consistent, predictable
    place to check after every build rather than needing to scroll back
    through build console output to find out whether anything needs
    attention."""
    path = os.path.join(project_dir, "MANUAL_REVIEW.md")
    lines = [
        f"# Manual review — {app_name}",
        "",
        f"Generated {datetime.datetime.now().isoformat(timespec='seconds')} by the QVF -> PBIX build.",
        "",
        "Everything below is something the automated conversion could not resolve on its own — ",
        "a field/measure it couldn't bind, a Qlik construct with no Power BI equivalent, a relationship ",
        "or hierarchy it deliberately left out of the model, or a judgment call worth double-checking. ",
        "Nothing else in the app needed manual attention.",
        "",
    ]
    # Dedupe while preserving first-seen order — the same warning can
    # legitimately print more than once (e.g. once per page a broken
    # binding appears on).
    seen: set[str] = set()
    deduped = []
    for note in notes:
        if note not in seen:
            seen.add(note)
            deduped.append(note)

    if not deduped:
        lines.append("No manual follow-up items were detected in this build.")
    else:
        for note in deduped:
            # Strip the leading "[build] "/"[convert] "/"[extract] " tag —
            # redundant once every line in this file is already known to
            # be from the build.
            text = re.sub(r"^\[(build|convert|extract)\]\s*", "", note)
            lines.append(f"- {text}")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"[build] wrote manual-review checklist -> {path} ({len(deduped)} item(s))")


def build_project(app_name: str) -> str:
    """Returns the path to the compiled .pbix file. Also writes
    output/<app_name>/MANUAL_REVIEW.md summarizing every gap this build
    couldn't close automatically — see _write_manual_review_file."""
    project_dir = os.path.join(OUTPUT_ROOT, app_name)
    os.makedirs(project_dir, exist_ok=True)
    real_stdout = sys.stdout
    capture = _ManualReviewCapture(real_stdout)
    sys.stdout = capture
    try:
        pbix_path = _build_project_impl(app_name)
    finally:
        sys.stdout = real_stdout
        _write_manual_review_file(project_dir, app_name, capture.notes)
    return pbix_path


def _build_project_impl(app_name: str) -> str:
    extracted_dir = os.path.join(EXTRACTED_ROOT, app_name)
    converted_dir = os.path.join(CONVERTED_ROOT, app_name)
    project_dir = os.path.join(OUTPUT_ROOT, app_name)
    os.makedirs(project_dir, exist_ok=True)

    (tables, measures_by_table, calc_cols_by_table, hierarchies_by_table,
     original_measure_table, required_relationship_orientations) = _assemble_semantic_inputs(extracted_dir, converted_dir)

    llm_relationships = _load_json(converted_dir, "data_model.converted.json").get("relationships", [])
    inferred_relationships = infer_relationships(tables, extracted_dir)
    relationships = _merge_relationships(llm_relationships, inferred_relationships)
    # data_model.converted.json is LLM output describing Qlik's OWN
    # associative model — it can propose a relationship touching a table
    # this build deliberately never created (e.g. a Qlik report-distribution
    # table skipped per _DISTRIBUTION_TABLE_NAME_RE above, or any other
    # table the LLM saw fields for but this pass excluded). write_semantic_model
    # only ever iterates real entries in `tables`, so a relationship
    # pointing at a name outside it isn't just inert — the TMDL writer would
    # emit a reference to a table/column that doesn't exist. Drop those here.
    relationships = [r for r in relationships if r.get("from_table") in tables and r.get("to_table") in tables]
    relationships = _orient_relationships_for_related(relationships, required_relationship_orientations)
    relationships = _drop_relationships_into_related_calc_tables(relationships, tables)
    relationships = _drop_relationships_with_non_unique_one_side(relationships, tables)
    roles = _load_json(converted_dir, "rls.converted.json").get("roles", [])
    _warn_userelationship_rls_collision(relationships, roles)
    parameters = _load_json(converted_dir, "variables.converted.json").get("variables", [])
    parameters = [p for p in parameters if p.get("target") == "power_query_parameter"]

    pages = _load_pages(converted_dir)
    _apply_deterministic_visual_types(pages, extracted_dir)

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
    container_id_map = _build_container_id_map(extracted_dir)
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

    # A Qlik "scenario picker" — several action-buttons on one sheet each
    # setting the SAME variable to a different fixed value (e.g.
    # "Standard"/"Enhanced"/"Aggressive" buttons all setting `vDunning`) —
    # has no working Power BI button equivalent (no button action can set
    # a DAX value); replace it with a disconnected scenario table +
    # SELECTEDVALUE() measure + a real, interactive Slicer instead.
    _apply_variable_scenarios(extracted_dir, tables, measures_by_table, pages, original_measure_table)

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

    # The relationship-direction fix above (_orient_relationships_for_related)
    # only ran against the deterministic join-aggregate case known at that
    # point — but an LLM-converted DAX measure can ALSO call RELATED() into
    # another table, and the LLM's own data-model relationship inference
    # (§2 of data_model.skill.md) and its DAX conversion (§Task A of
    # sheets_convert.skill.md) are two SEPARATE calls with no shared
    # knowledge of each other's judgment call — one can decide "PredictARFact
    # is the many side" while the other writes a measure that only works if
    # ARFact is the many side, producing a real Power BI Desktop error
    # ("doesn't have a relationship to any table available in the current
    # context") despite every column genuinely existing. Now that every
    # measure/calculated column is in its final form, scan all of them for
    # RELATED() calls and re-apply the same orientation fix for whatever
    # this pass finds, overriding the LLM's relationship-direction guess
    # with what the DAX that's actually shipping requires.
    related_orientations = _collect_related_orientations_from_measures(measures_by_table, calc_cols_by_table)
    relationships = _orient_relationships_for_related(relationships, related_orientations)

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
    # _merge_container_kpi_siblings(pages, container_id_map) — REVERTED per
    # explicit user report: merging container-sibling KPI cards into one
    # multiRowCard produced worse output and more chart errors once
    # extraction started including every container child unfiltered
    # (constant-KPI badges, buttons, shapes all now flow into the same
    # container grouping this merge acts on). Left disabled rather than
    # deleted — the function/detection still exist if this is worth
    # revisiting with a narrower trigger condition later.
    _sanitize_visual_shapes(pages)
    report_dir = os.path.join(project_dir, f"{app_name}.Report")
    theme_data = load_theme(extracted_dir)
    custom_theme = build_custom_theme(app_name, theme_data.get("colors", []))
    if custom_theme:
        print(f"[build] applying custom report theme '{custom_theme['name']}' "
              f"({len(custom_theme['dataColors'])} data colors, from the app's own extracted palette)")

    # Real bookmark files must exist BEFORE write_report runs, so its
    # button-action relocator (_relocate_button_action in report.py) can
    # resolve a "Bookmark" action's destination against a bookmark that
    # actually exists in the project, instead of a dangling reference to
    # the raw Qlik bookmark id/title the LLM happened to emit.
    page_order = [p.get("page", {}).get("name") for p in pages if p.get("page", {}).get("name")]
    bookmarks = _load_json(extracted_dir, "bookmarks.json")
    bookmarks = bookmarks if isinstance(bookmarks, list) else []
    bookmark_lookup = write_bookmarks(report_dir, bookmarks, page_order)

    write_report(report_dir, pages, app_name=app_name, custom_theme=custom_theme, bookmark_lookup=bookmark_lookup)

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

    # EXPERIMENTAL, opt-in via env var (see pbix_parameter_patch.py's own
    # docstring for the full explanation): try keeping SourceDataPath as a
    # REAL, editable Power Query Parameter in the compiled .pbix instead of
    # inlining it to a literal. UNVERIFIED against real Power BI Desktop —
    # never on by default, so every existing build keeps the safe,
    # proven-working literal-inlined behavior unless explicitly requested.
    use_real_parameters = os.environ.get("PBIX_REAL_PARAMETERS") == "1"
    if use_real_parameters:
        from .pbix_parameter_patch import enable_real_parameters, queue_parameter
        enable_real_parameters()
        literal = '"' + source_data_path.replace('"', '""') + '"'
        queue_parameter("SourceDataPath", literal, default_value=source_data_path)
        tables_for_compile = tables  # keep the real SourceDataPath reference, don't inline it
        print(f"[build] PBIX_REAL_PARAMETERS=1: attempting to keep 'SourceDataPath' as a real "
              f"Power Query Parameter in the compiled .pbix (EXPERIMENTAL — verify it actually "
              f"opens in Power BI Desktop before trusting this).")
    else:
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


_VALID_COLUMN_TYPES = {"string", "int64", "double", "dateTime", "boolean", "variant"}


def _load_type_overrides(extracted_dir: str) -> dict[str, str]:
    """A user-maintained escape hatch for a column whose type keeps getting
    (re)detected wrong from the Qlik metadata/CSV sampling alone, no matter
    which automatic pass runs — every automatic classification in this file
    (data_model.skill.md's LLM tagging, _infer_column_type's Qlik-tag
    fallback, _reconcile_column_types' aggregation-driven promotion) is a
    best-effort GUESS from indirect signals; this file lets a person just
    say what the type actually is, and that decision survives every future
    re-build (it lives next to the extracted data, not inside build output,
    and re-extracting never overwrites an existing one — see extract_app).

    File: extracted/<app_name>/type_overrides.json — a flat
    `{"TableName.ColumnName": "string"|"int64"|"double"|"dateTime"|
    "boolean"|"variant"}` map. Missing or absent file = no overrides
    (the default, automatic behavior is unchanged)."""
    path = os.path.join(extracted_dir, "type_overrides.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (ValueError, OSError) as exc:
        print(f"[build] WARNING: type_overrides.json exists but couldn't be read ({exc}) — ignoring it")
        return {}
    if not isinstance(raw, dict):
        print("[build] WARNING: type_overrides.json's top level must be an object — ignoring it")
        return {}
    out: dict[str, str] = {}
    for key, value in raw.items():
        if value not in _VALID_COLUMN_TYPES:
            print(f"[build] WARNING: type_overrides.json entry '{key}': '{value}' isn't a recognized type "
                  f"({', '.join(sorted(_VALID_COLUMN_TYPES))}) — ignoring this entry")
            continue
        out[key] = value
    return out


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

    m_expression = generate_groupby_count_partition_m(spec)
    columns = [
        {"name": spec["group_alias"], "data_type": "string", "source_column": spec["group_alias"]},
        {"name": spec["measure_name"], "data_type": "int64", "source_column": spec["measure_name"]},
    ]
    print(f"[build] '{table_name}' detected as a Qlik RESIDENT/GROUP BY aggregate over '{source_table}' "
          f"(the group key resolves via mapping to '{value_table}[{spec['value_field']}]') — building it "
          f"as a real Power Query table (Table.Group over a reference to '{source_table}''s own M), not a "
          f"DAX calculated table: a calculated table's relationships get silently dropped from TMDL by "
          f"Power BI Desktop's own project-load validation, which left this exact table's key column "
          f"disconnected from the rest of the model in a real app before this fix (every measure on it read "
          f"blank in any customer-grouped visual despite a correct relationship being proposed).")
    return {"columns": columns, "m_expression": m_expression, "is_calculated": False}


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
    # `REPLACE LOAD` (drops the target's existing rows first) is just as
    # common a qualifier here as plain LOAD — without tolerating it, every
    # rename/computed-column detector built on this regex silently sees
    # NOTHING for any table whose script uses REPLACE LOAD.
    r"\b(\w+):\s*(?:REPLACE\s+)?LOAD\s+(.*?)\s*(?:\bFROM\b|\bRESIDENT\b|;)", re.IGNORECASE | re.DOTALL,
)


_EXIT_SCRIPT_RE = re.compile(r"\bexit\s+script\b", re.IGNORECASE)


def _truncate_at_exit_script(script_text: str) -> str:
    """Qlik stops executing a LOAD script at its first `Exit Script`
    statement — anything after it (an author's own draft/scratch/disabled
    LOAD blocks, notes, "old version kept for reference" tables, etc.)
    never actually runs in Qlik itself and has no bearing on the real data
    model. Every detector in this file works directly off the raw script
    text, so leaving that dead tail in place risks parsing a table/column/
    idiom that LOOKS real but was never live in the source app at all —
    truncate it away here, once, so every detector downstream automatically
    only ever sees the part of the script Qlik would actually run.

    Only the FIRST occurrence matters (a second `Exit Script` further down
    is already unreachable dead code itself, same as everything else past
    the first one) — cuts at the start of the keyword itself, which is
    always inside a statement already terminated by the previous `;`, so
    nothing live is ever lost."""
    m = _EXIT_SCRIPT_RE.search(script_text)
    return script_text[:m.start()] if m else script_text


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


_SIMPLE_RENAME_RE = re.compile(
    # Source field name as a bare identifier, double-quoted string, or
    # [bracketed] name (`"Name" AS [DL_DISTRIBUTION_GROUP_NAMES]` is just as
    # much a plain rename as `RiskBand AS PredictedRiskBand` — Qlik quotes/
    # brackets a field name whenever it contains a space or is a reserved
    # word, most commonly seen straight off an Excel header like "Name").
    # Target is always a bare identifier or bracketed name — never quoted
    # (a NEW field being declared, not a lookup).
    r'^\s*(?:"([^"]+)"|\[([^\]]+)\]|([A-Za-z_]\w*))\s+[Aa][Ss]\s+(?:\[([^\]]+)\]|([A-Za-z_]\w*))\s*$'
)


_TABLE_LABEL_LOAD_RE = re.compile(
    # A `Crosstable(...)` (unpivot qualifier) can sit between the table
    # label and the LOAD/SELECT it modifies — `KPI_Metadata:\n
    # Crosstable(KPIParams, KPIParamsValue, 2)\nLoad * Inline ...` — without
    # tolerating it here, every detector built on this regex (source-file
    # detection, date-format detection, etc.) loses track of "current
    # table" partway through and misattributes whatever follows to no table
    # at all.
    # `REPLACE LOAD` (drops the target table's existing rows before
    # reloading — a normal, common Qlik LOAD qualifier alongside plain
    # LOAD) must be tolerated here too: `TableName:\nREPLACE LOAD ...` is
    # just as much a fresh "current table" as a plain `TableName:\nLOAD`,
    # but without this the label was silently never recognized at all,
    # leaving whatever table was current BEFORE it wrongly still "current"
    # for the whole REPLACE LOAD block that follows.
    r"\b(\w+):\s*\r?\n\s*(?:Crosstable\s*\([^)]*\)\s*\r?\n\s*)?(?:mapping\s+)?(?:REPLACE\s+)?(?:LOAD|SELECT)\b",
    re.IGNORECASE,
)
_CROSSTABLE_RE = re.compile(
    r"\bCrosstable\s*\(\s*([A-Za-z_]\w*)\s*,\s*([A-Za-z_]\w*)\s*(?:,\s*(\d+)\s*)?\)",
    re.IGNORECASE,
)
_CONCAT_JOIN_PREFIX_RE = re.compile(
    r"\b(?:concatenate|join|left\s+join|right\s+join|inner\s+join|outer\s+join)\s*\(\s*(\w+)\s*\)",
    re.IGNORECASE,
)
# Qlik accepts a `FROM` connection string in EITHER `[...]` or `"..."`
# delimiters (a script author's own stylistic choice, both equally valid
# and equally common) — matching only the bracket form silently misses
# every quoted `FROM "lib://...file.xlsx"` table entirely, which then
# falls back to the "{table}.csv" default filename guess and fails outright
# ("Could not find file") even though the script names a perfectly real
# source right there, just in the other delimiter style.
_FROM_FILE_RE = re.compile(r'\bFROM\s*(?:\[([^\]]+)\]|"([^"]+)")', re.IGNORECASE)
# An Excel source (`FROM [....xlsx] (ooxml, embedded labels, table is
# Dim_Rep)`) names which WORKSHEET to read right after the FROM clause —
# unlike a CSV, one .xlsx file commonly backs EVERY table in the script,
# distinguished only by this "table is X" qualifier (a real app: 8 Qlik
# tables, one shared .xlsx, one sheet per table). Captured separately from
# _FROM_FILE_RE (which only needs the filename) since a CSV source has no
# such qualifier and this must stay optional/None for those. Same
# bracket-or-quote tolerance as _FROM_FILE_RE above.
_FROM_FILE_SHEET_RE = re.compile(
    r'\bFROM\s*(?:\[[^\]]+\]|"[^"]+")\s*\r?\n?\s*\(\s*ooxml\s*,[^)]*?\btable\s+is\s+([^),]+?)\s*\)',
    re.IGNORECASE | re.DOTALL,
)
# A `LOAD`/`REPLACE LOAD ... FROM [...] (ooxml, ..., table is X)` with NO
# table label and NO Concatenate(...)/Join(...) prefix of its own doesn't
# stay "unnamed" in real Qlik — the engine names the resulting table after
# the ooxml qualifier's own "table is X" sheet name (seen: a script tab
# with two bare `Replace LOAD ... (ooxml, ..., table is DL_..._QCS);`
# blocks and no labels anywhere near them; the real app's data_model.json
# confirms the tables ARE named `DL_..._QCS`, exactly the ooxml qualifier
# text). Anchored on a `;`/script-start immediately before the LOAD
# (allowing only whitespace/line-comments between) so a genuinely LABELED
# block (`Name:\nLOAD ...`) or a real continuation
# (`Concatenate(Table)\nLOAD ...`) — both of which have something other
# than a bare `;` immediately preceding LOAD — are correctly NOT matched
# here; those already have a perfectly good table name from their own
# label/prefix.
_UNLABELED_OOXML_LOAD_RE = re.compile(
    r'(?:;|\A)(?:\s*//[^\n]*)*\s*(?:REPLACE\s+)?LOAD\b(?:(?!;).)*?'
    r'\bFROM\s*(?:\[[^\]]+\]|"[^"]+")\s*\r?\n?\s*\(\s*ooxml\s*,[^)]*?\btable\s+is\s+([^),]+?)\s*\)',
    re.IGNORECASE | re.DOTALL,
)


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
    return {table: [f for f, _ in pairs] for table, pairs in _detect_source_files_and_sheets(script_text).items()}


def _detect_source_sheets(script_text: str) -> dict[str, list[str | None]]:
    """The Excel worksheet name for each entry in `_detect_source_files`'
    result for the same table, aligned POSITIONALLY (same index = same
    file/worksheet pair) — None for a CSV source, which has no worksheet
    concept at all. A table with an entry here at index i names the
    worksheet backing `_detect_source_files(script_text)[table][i]`."""
    return {table: [s for _, s in pairs] for table, pairs in _detect_source_files_and_sheets(script_text).items()}


def _detect_source_files_and_sheets(script_text: str) -> dict[str, list[tuple[str, str | None]]]:
    """Shared walk behind `_detect_source_files`/`_detect_source_sheets` —
    tracks (filename, worksheet) as one PAIR per `FROM [...]` so the two
    stay aligned even when the same file is loaded twice for one table
    under DIFFERENT worksheets (a `left join` block re-reading the same
    .xlsx for a second sheet is the normal way Qlik enriches one table from
    two tabs of the same workbook) — deduping by filename alone, as the
    original single-purpose function did, would silently collapse that
    second (filename, sheet) pair and misalign the two lists against each
    other by one for every table after it.

    Walks the script sequentially tracking which table is "current" (set by
    a `TableName:` label or a Concatenate/Join(...) prefix) and records
    every `FROM [...]` filename/worksheet pair against it, in the order the
    script loads them — a `Join`/`Left Join`/etc. onto a RESIDENT
    aggregation (not a file) naturally contributes no FROM and so never
    shows up here."""
    events: list[tuple[int, str, tuple[str, str | None] | str]] = []
    for m in _TABLE_LABEL_LOAD_RE.finditer(script_text):
        events.append((m.start(), "current_table", m.group(1)))
    for m in _CONCAT_JOIN_PREFIX_RE.finditer(script_text):
        events.append((m.start(), "current_table", m.group(1)))
    for m in _UNLABELED_OOXML_LOAD_RE.finditer(script_text):
        # This block's own FROM event fires later at its own match position
        # (via _FROM_FILE_RE below) — placing this "current_table" event at
        # the LOAD's own start (m.start(1) is inside the match, before the
        # nested FROM) ensures it sorts ahead of that FROM event and is
        # already in effect when it fires.
        events.append((m.start(), "current_table", m.group(1).strip()))
    for m in _FROM_FILE_RE.finditer(script_text):
        # Just the filename — the lib:// connection path is only valid on
        # the machine that authored the .qvf; every table's M already
        # resolves against the shared SourceDataPath parameter instead.
        # group(1) is the bracket-delimited form, group(2) the quoted form —
        # exactly one of the two is populated per match.
        raw_path = m.group(1) if m.group(1) is not None else m.group(2)
        filename = re.split(r"[/\\]", raw_path)[-1]
        sheet_match = _FROM_FILE_SHEET_RE.match(script_text, m.start())
        sheet = sheet_match.group(1).strip() if sheet_match else None
        events.append((m.start(), "from", (filename, sheet)))
    events.sort(key=lambda e: e[0])

    return _pairs_by_table_from_events(events)


_RESIDENT_ONLY_TABLE_RE = re.compile(
    r"\b(\w+):\s*(?:REPLACE\s+)?LOAD\s+((?:(?!;|\bFROM\b|\bRESIDENT\b).)*?)\bRESIDENT\s+(\w+)\b"
    r"((?:(?!;).)*)",
    re.IGNORECASE | re.DOTALL,
)
# A function name here, found anywhere in a Resident block's own field list
# or trailing WHERE/GROUP BY clause, means this is a real AGGREGATION/
# filtering pass (a summary table — new rows computed FROM the source
# table's rows, not the same rows just reselected/renamed), never the
# simple "reshape and rename" idiom _detect_resident_only_tables exists
# for. Reusing that idiom's "borrow the source table's own file, select by
# these exact column names" strategy for a block like this would silently
# select NONEXISTENT column names straight off the source FILE (an
# aggregated/computed field like `Count(DISTINCT DisputeID) AS
# DisputeCount90d` was never a real file column at all) — every such
# column would come back null with no visible error, exactly the kind of
# silent data corruption this whole file's detectors exist to avoid.
_RESIDENT_AGGREGATION_SIGNAL_RE = re.compile(
    r"\b(?:count|sum|avg|min|max|group\s+by|where)\b", re.IGNORECASE
)


def _detect_resident_only_tables(script_text: str) -> dict[str, str]:
    """{table_name: source_table_name} for a table whose LOAD reads ONLY
    from another already-loaded table via `Resident <OtherTable>`, with no
    FROM/INLINE of its own at all — e.g. a script tab that loads
    `TempGroups: LOAD * FROM [file.xlsx] (...)`, then
    `DL_DISTRIBUTION_SVC_GROUPS_QCS: REPLACE LOAD ... Resident TempGroups;`,
    then `Drop Table TempGroups;` (a common Qlik pattern: load a scratch
    table from a file, reshape/select/rename it via one or more Resident
    passes, discard the scratch table). The derived table has no file of
    its own to point at directly, but its rows originally came from the
    SAME file/sheet the Resident source table loaded — callers can borrow
    that source table's already-detected file(s)/sheet(s) as a faithful
    stand-in, since selecting-and-renaming those same source columns (see
    _detect_simple_renamed_columns, which also understands the quoted
    `"Field" AS [Alias]` form this idiom commonly pairs with) reproduces
    the same final columns without needing the (already-dropped-in-Qlik,
    never-extracted-as-its-own-file) scratch table at all.

    A table whose script conditionally rebuilds it in more than one branch
    (`if ... then TableName: REPLACE LOAD ... Resident X; else TableName:
    REPLACE LOAD ... Resident X; end if` — same Resident source either way,
    just different field expressions per branch) still resolves to ONE
    source table name here, which is all this needs; only the FIRST
    Resident source seen per table is kept if they ever genuinely differ
    (script control flow can't be statically resolved, and the first
    occurrence is as reasonable a guess as any)."""
    out: dict[str, str] = {}
    for m in _RESIDENT_ONLY_TABLE_RE.finditer(script_text):
        table_name, field_list, source_table, trailer = m.group(1), m.group(2), m.group(3), m.group(4)
        if _RESIDENT_AGGREGATION_SIGNAL_RE.search(field_list) or _RESIDENT_AGGREGATION_SIGNAL_RE.search(trailer):
            continue
        if table_name not in out:
            out[table_name] = source_table
    return out


# A Qlik app's own in-application report-distribution/bursting
# infrastructure — a recipient list, recipient groups, per-recipient
# filters, an enabled/disabled flag per recipient/group — configures
# QLIK'S reporting/NPrinting distribution feature itself, never business
# data a Power BI report/model should reproduce as a table (this pipeline's
# scope is producing the report/model, not an automated distribution
# system). Matched by table NAME convention — every real-world example
# seen uses the `DL_DISTRIBUTION...` prefix (DL_DISTRIBUTION_SVC_USERS_QCS,
# DL_DISTRIBUTION_SVC_GROUPS_QCS, or a bare DL_DISTRIBUTION_<anything>).
_DISTRIBUTION_TABLE_NAME_RE = re.compile(r"^DL_DISTRIBUTION", re.IGNORECASE)
# Qlik's own tag namespace for a report-distribution field — `TAG FIELD
# DL_DISTRIBUTION_EMAIL with 'DL_DISTRIBUTION_SVC__recipientEmail'` —
# matched by PREFIX (not an exhaustive fixed list of tag names) since an
# app can tag additional fields (a new recipient attribute, say) under this
# same namespace that a hardcoded list would silently miss.
_DISTRIBUTION_TAG_PREFIX_RE = re.compile(r"DL_DISTRIBUTION_SVC__", re.IGNORECASE)
_TAG_FIELD_RE = re.compile(r"\btag\s+field\s+(\S+?)\s+with\s+'([^']*)'", re.IGNORECASE)


def _detect_report_distribution_tags(script_text: str) -> dict[str, str]:
    """{field_name: tag} for every `TAG FIELD ... with '<tag>'` statement
    whose tag falls under Qlik's report-distribution namespace (see
    _DISTRIBUTION_TAG_PREFIX_RE) — used only to document, in the build log,
    which specific business concept (recipient name/email/filter, group
    name/description/enabled flag) a skipped distribution table's field
    represented. Per the migration rule this implements: the Qlik
    MECHANISM (its own in-app distribution tables) is never reproduced in
    a report-only conversion, but the underlying business information it
    carried must never just silently vanish — a person picking a real
    Power BI distribution mechanism (subscriptions, Power Automate, RLS)
    later needs to know it was there at all."""
    out: dict[str, str] = {}
    for m in _TAG_FIELD_RE.finditer(script_text):
        field_name = m.group(1).strip('"[]')
        tag = m.group(2)
        if _DISTRIBUTION_TAG_PREFIX_RE.search(tag):
            out[field_name] = tag
    return out


def _pairs_by_table_from_events(
    events: list[tuple[int, str, tuple[str, str | None] | str]],
) -> dict[str, list[tuple[str, str | None]]]:
    pairs_by_table: dict[str, list[tuple[str, str | None]]] = {}
    current_table: str | None = None
    for _, kind, value in events:
        if kind == "current_table":
            current_table = value
        elif current_table:
            pairs = pairs_by_table.setdefault(current_table, [])
            if value not in pairs:
                pairs.append(value)
    return pairs_by_table


_LOAD_FIELDS_FROM_FILE_RE = re.compile(
    # The field-list group must NOT cross a ";" (statement end), another
    # "RESIDENT" (a different block shape entirely), or another "LOAD" (an
    # earlier or later block's own keyword) before reaching "FROM" — a
    # plain non-greedy `.*?` here previously matched straight through an
    # intervening `LOAD * INLINE [...]` block that has no FROM of its own
    # at all, wrongly attributing THAT block's fields (and none of its own)
    # to the next real FROM clause it found later in the script.
    r"\bLOAD\s+((?:(?!;|\bFROM\b|\bRESIDENT\b|\bLOAD\b).)*?)\s*\bFROM\s*\[([^\]]+)\]",
    re.IGNORECASE | re.DOTALL,
)
_LITERAL_AS_RE = re.compile(r"^\s*'([^']*)'\s+[Aa][Ss]\s+([A-Za-z_]\w*)\s*$")
_TEXTFUNC_AS_RE = re.compile(
    r"^\s*(upper|lower|trim|capitalize)\s*\(\s*([A-Za-z_]\w*)\s*\)\s+[Aa][Ss]\s+([A-Za-z_]\w*)\s*$",
    re.IGNORECASE,
)
_ARITH_AS_RE = re.compile(r"^\s*(-?[\w\s()+\-*/.]+?)\s+[Aa][Ss]\s+([A-Za-z_]\w*)\s*$")
# Function names already handled by their OWN dedicated detector elsewhere
# in this file (Chr concat, dual/autonumber keys, ApplyMap+SubField, Month/
# MonthName, mapping tables, date#() parsing) — if any of these appear in
# an "arithmetic-looking" candidate expression, it's actually one of those
# more specific patterns and must NOT also be claimed here.
_ARITH_DISALLOWED_WORDS = {
    "chr", "dual", "autonumber", "applymap", "subfield", "month", "monthname",
    "num", "date", "date#", "sum", "count", "avg", "if", "pick", "match",
    "class", "weekday", "week", "floor", "ceil", "round", "alt", "upper",
    "lower", "trim", "capitalize",
}
_DATE_FMT_AS_RE = re.compile(
    r"^\s*date\s*\(\s*date#\s*\(\s*([A-Za-z_]\w*)\s*,\s*(.+?)\)\s*\)\s+[Aa][Ss]\s+([A-Za-z_]\w*)\s*$",
    re.IGNORECASE,
)


def _is_valid_arith_expr(expr: str) -> bool:
    """True if `expr` looks like a safe, purely-arithmetic Qlik expression
    (field names, `+ - * /`, parens, numeric literals only) that this
    project's general arithmetic-computed-column handling can reproduce
    faithfully in M — e.g. `(Qty*UnitPrice)-Discount`,
    `-(ReturnQty*UnitPrice)`, or a bare UNARY negation of a single field
    with no other operator at all (`-ReturnQty as Qty` — a real, common
    Qlik idiom for "returns subtract from the running total"; rejecting
    this as "not really arithmetic" left `Qty` uncaught by every detector,
    selected literally from a file that only ever had "ReturnQty", and
    silently blank). Deliberately conservative otherwise: anything
    containing a function name already owned by a more specific detector,
    a comma (multi-arg function call), or any character outside the
    allowed set is rejected rather than guessed at."""
    if "," in expr or not re.fullmatch(r"[\w\s()+\-*/.]+", expr):
        return False
    stripped = expr.lstrip("-").strip()
    has_operator = bool(re.search(r"[+\-*/]", stripped))
    is_unary_single_field = expr.strip().startswith("-") and bool(re.fullmatch(r"[A-Za-z_]\w*", stripped))
    if not (has_operator or is_unary_single_field):
        return False  # no operator and not a unary negation -> a bare rename, handled elsewhere
    words = {w.lower() for w in re.findall(r"[A-Za-z_]\w*", expr)}
    return not (words & _ARITH_DISALLOWED_WORDS)


def _arith_expr_fields(expr: str) -> list[str]:
    return list(dict.fromkeys(re.findall(r"[A-Za-z_]\w*", expr)))


def _detect_expr_columns_per_file(script_text: str) -> dict[str, list[dict[str, dict]]]:
    """A column the Qlik script computes for a SPECIFIC source block rather
    than reading it directly from the file — never a real column in any
    source CSV, so selecting it via Table.SelectColumns would silently null
    it (MissingField.UseNull), which then makes every DAX filter/measure
    keyed on it quietly return BLANK everywhere with no visible error.
    Covers three common LOAD idioms, one per detected `"kind"`:
    - a fixed STRING LITERAL (`'Sale' as RecordType` in one LOAD block,
      `'Return' as RecordType` in another feeding the same concatenated
      table — a Fact_Orders built from 4 "Sale" files + 1 "Return" file,
      each block literally hard-coding which one it is);
    - a single-argument text function wrapping one real field
      (`upper(ZoneName) as ZM_ZONENAME` — the real source column is
      "ZoneName", under a DIFFERENT name, so this is also a rename, just
      one the simpler bare-rename detector correctly declines to handle
      since the whole expression isn't just one identifier);
    - a general arithmetic expression combining real fields
      (`(Qty*UnitPrice)-Discount as NetAmount`, or a DIFFERENT formula in
      another block feeding the same table, e.g. Returns'
      `-(ReturnQty*UnitPrice) as NetAmount` — genuinely not expressible as
      one global post-combine formula since the fields/sign differ per file).

    Walks the script the same way _detect_source_files does (tracking the
    "current table" via a `TableName:` label or a Concatenate/Join(...)
    prefix), but matches `LOAD ... FROM [...]` as ONE combined regex so
    each file's own computed columns are captured paired with THAT same
    file — guaranteeing the same order, one dict per file (possibly empty),
    that _detect_source_files produces for that table's filename list.

    Returns {table_name: [{"ColumnName": {"kind": ..., ...}, ...}, ...]} —
    the list is positionally aligned with
    _detect_source_files()[table_name]."""
    events: list[tuple[int, str, object]] = []
    for m in _TABLE_LABEL_LOAD_RE.finditer(script_text):
        events.append((m.start(), "current_table", m.group(1)))
    for m in _CONCAT_JOIN_PREFIX_RE.finditer(script_text):
        events.append((m.start(), "current_table", m.group(1)))
    for m in _LOAD_FIELDS_FROM_FILE_RE.finditer(script_text):
        field_list = m.group(1)
        specs: dict[str, dict] = {}
        for item in _split_top_level_commas(field_list):
            # Date-format columns are handled by a separate detector (they
            # stay as a real selected+renamed column with custom parsing,
            # not an excluded-and-computed one) — skip them here so an
            # arithmetic-looking date#() call never gets double-claimed.
            if _DATE_FMT_AS_RE.match(item):
                continue
            lm = _LITERAL_AS_RE.match(item)
            if lm:
                specs[lm.group(2)] = {"kind": "literal", "value": lm.group(1)}
                continue
            tm = _TEXTFUNC_AS_RE.match(item)
            if tm:
                specs[tm.group(3)] = {"kind": "textfunc", "func": tm.group(1).lower(), "source": tm.group(2)}
                continue
            am = _ARITH_AS_RE.match(item)
            if am:
                expr_text, alias = am.group(1).strip(), am.group(2)
                if _is_valid_arith_expr(expr_text):
                    fields = _arith_expr_fields(expr_text)
                    if fields:
                        specs[alias] = {"kind": "arith", "expr": expr_text, "fields": fields}
        events.append((m.start(), "from_block", specs))
    events.sort(key=lambda e: e[0])

    out: dict[str, list[dict[str, dict]]] = {}
    current_table: str | None = None
    for _, kind, value in events:
        if kind == "current_table":
            current_table = value
        elif current_table:
            out.setdefault(current_table, []).append(value)
    return out


def _detect_date_format_columns_per_file(script_text: str) -> dict[str, list[dict[str, dict]]]:
    """A date column parsed with an EXPLICIT Qlik date#() format string —
    `date(date#(OrderDate,'MM/DD/YYYY')) as Date` — possibly a DIFFERENT
    format (and even a different source field name) per source block
    feeding the same concatenated table (seen: 'MM/DD/YYYY' in one file,
    'DD-MM-YYYY' in another, 'YYYYMMDD' in a third, all feeding the same
    Fact_Orders). A single generic date parser can't reliably tell these
    apart — 'YYYYMMDD' text and ambiguous 'DD-MM-YYYY' text don't survive
    a locale-guessing fallback, which is exactly why every one of that
    table's dates was coming back null. `alt('fmt1','fmt2')` is captured
    as multiple formats, tried in order.

    Same per-file event-walking as _detect_expr_columns_per_file — see
    that function's docstring for the general mechanism.

    Returns {table_name: [{"ColumnName": {"source": field, "formats":
    [fmt, ...]}, ...}, ...]}, positionally aligned with
    _detect_source_files()[table_name]."""
    events: list[tuple[int, str, object]] = []
    for m in _TABLE_LABEL_LOAD_RE.finditer(script_text):
        events.append((m.start(), "current_table", m.group(1)))
    for m in _CONCAT_JOIN_PREFIX_RE.finditer(script_text):
        events.append((m.start(), "current_table", m.group(1)))
    for m in _LOAD_FIELDS_FROM_FILE_RE.finditer(script_text):
        field_list = m.group(1)
        specs: dict[str, dict] = {}
        for item in _split_top_level_commas(field_list):
            dm = _DATE_FMT_AS_RE.match(item)
            if not dm:
                continue
            source_field, fmt_arg, alias = dm.group(1), dm.group(2), dm.group(3)
            formats = re.findall(r"'([^']*)'", fmt_arg)
            if formats:
                specs[alias] = {"source": source_field, "formats": formats}
        events.append((m.start(), "from_block", specs))
    events.sort(key=lambda e: e[0])

    out: dict[str, list[dict[str, dict]]] = {}
    current_table: str | None = None
    for _, kind, value in events:
        if kind == "current_table":
            current_table = value
        elif current_table:
            out.setdefault(current_table, []).append(value)
    return out


_INLINE_TABLE_RE = re.compile(
    # Qlik accepts EITHER `[...]` (the common case) or a backtick-delimited
    # `` `...` `` block for INLINE data — the backtick form shows up whenever
    # the literal data itself contains a `]` or `[` character, or just by
    # author preference. Only matching `[...]` silently found ZERO fields
    # for a backtick table (the table extracts as empty — genuinely broken,
    # not just "using the default {table}.csv fallback"), so both forms are
    # matched here as alternatives.
    r"\b(\w+):\s*\r?\n\s*(?:Crosstable\s*\([^)]*\)\s*\r?\n\s*)?LOAD\s+(?:\*|[^\[\]`;]*?)\s*INLINE\s*(?:\[(.*?)\]|`(.*?)`)",
    re.IGNORECASE | re.DOTALL,
)


def _split_inline_csv_line(line: str, delimiter: str = ",") -> list[str]:
    if delimiter == "\t":
        return [cell.strip().strip("'\"") for cell in line.split("\t")]
    return [cell.strip().strip("'\"") for cell in _split_top_level_commas(line)]


def _detect_crosstable_specs(script_text: str) -> dict[str, dict]:
    """`Crosstable(AttributeField, ValueField[, N])` immediately preceding a
    table's own LOAD/SELECT — Qlik's built-in "unpivot the wide part of this
    table" qualifier (the first N fields the LOAD itself declares/reads pass
    through unchanged as "qualifier" columns; every OTHER field becomes one
    row per column, under AttributeField=<original column name>,
    ValueField=<that column's value> — the standard way a Qlik script
    ingests a wide per-period/per-metric matrix as a narrow fact table).
    `N` defaults to 1 (Qlik's own default) when omitted.

    This is a genuinely different LOAD shape from every other detector in
    this file — the FINAL field list Qlik itself reports (data_model.json,
    already correct, straight from the real Engine API) simply won't match
    the source file's/inline block's own wide header row at all, so
    anything that selects columns by name from the raw source 1:1 needs to
    know this transformation happened. See `_detect_inline_tables`'s use of
    this for the INLINE case (the only source shape currently unpivoted
    automatically — a FROM-file crosstable is detected here too but only
    warned about, see project.py's main assembly loop).

    Returns {table_name: {"attribute_field": str, "value_field": str,
    "qualifier_count": int}}."""
    out: dict[str, dict] = {}
    for m in _TABLE_LABEL_LOAD_RE.finditer(script_text):
        table_name = m.group(1)
        between = script_text[m.start():m.end()]
        ct = _CROSSTABLE_RE.search(between)
        if ct:
            out[table_name] = {
                "attribute_field": ct.group(1),
                "value_field": ct.group(2),
                "qualifier_count": int(ct.group(3)) if ct.group(3) else 1,
            }
    return out


def _detect_inline_tables(script_text: str) -> dict[str, list[dict[str, str]]]:
    """A Qlik `LOAD * INLINE [header\\nrow1\\nrow2...]` (or backtick-delimited)
    table — literal rows typed directly into the script, not sourced from
    any file at all (seen: a small lookup/config table like a manager/zone
    assignment list, or a wide per-KPI config matrix fed through
    Crosstable() — see below). A build that assumes every table has a
    `{TableName}.csv` (or even any file) to load from fails outright for
    one of these — "Could not find file" — since no such file was ever
    meant to exist.

    Delimiter is auto-detected per block: if the header line contains a tab
    and no comma, it's tab-separated (Qlik's own INLINE default has no
    explicit delimiter option in that case, and a comma-splitter would
    wrongly treat the whole tab-separated line as one field); otherwise the
    original comma-separated behavior applies unchanged.

    If this table is ALSO wrapped in `Crosstable(...)` (see
    `_detect_crosstable_specs`), the raw wide rows are unpivoted here
    before being returned — Qlik's real qFields list (data_model.json)
    already reflects the crosstable's NARROW post-unpivot shape (qualifier
    columns + attribute field + value field), so the rows handed to the
    builder must match that shape too, not the block's own wide layout.

    Returns {table_name: [{"ColName": "value", ...}, ...]}."""
    crosstable_specs = _detect_crosstable_specs(script_text)
    out: dict[str, list[dict[str, str]]] = {}
    for m in _INLINE_TABLE_RE.finditer(script_text):
        table_name = m.group(1)
        block = m.group(2) if m.group(2) is not None else m.group(3)
        lines = [ln for ln in (raw.strip() for raw in block.splitlines()) if ln]
        if not lines:
            continue
        delimiter = "\t" if ("\t" in lines[0] and "," not in lines[0]) else ","
        header = _split_inline_csv_line(lines[0], delimiter)
        rows = []
        for line in lines[1:]:
            cells = _split_inline_csv_line(line, delimiter)
            rows.append({h: (cells[i] if i < len(cells) else "") for i, h in enumerate(header)})

        spec = crosstable_specs.get(table_name)
        if spec and header:
            qual_count = min(spec["qualifier_count"], len(header))
            qual_fields = header[:qual_count]
            attr_fields = header[qual_count:]
            attr_field, value_field = spec["attribute_field"], spec["value_field"]
            unpivoted = []
            for row in rows:
                qualifiers = {f: row.get(f, "") for f in qual_fields}
                for f in attr_fields:
                    unpivoted.append({**qualifiers, attr_field: f, value_field: row.get(f, "")})
            rows = unpivoted

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
            source_field = m.group(1) or m.group(2) or m.group(3)
            target_field = m.group(4) or m.group(5)
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
    type_overrides = _load_type_overrides(extracted_dir)

    script_path = os.path.join(extracted_dir, "script.qvs")
    month_derived: dict[str, dict] = {}
    groupby_count_tables: dict[str, dict] = {}
    renamed_by_table: dict[str, dict[str, str]] = {}
    chr_concat_by_table: dict[str, dict[str, dict]] = {}
    dual_key_by_table: dict[str, dict[str, dict]] = {}
    source_files_by_table: dict[str, list[str]] = {}
    source_sheets_by_table: dict[str, list[str | None]] = {}
    expr_columns_by_table: dict[str, list[dict[str, dict]]] = {}
    date_formats_by_table: dict[str, list[dict[str, dict]]] = {}
    inline_tables: dict[str, list[dict[str, str]]] = {}
    join_agg_by_table: dict[str, dict] = {}
    mapping_tables: dict[str, dict] = {}
    applymap_by_table: dict[str, dict[str, dict]] = {}
    crosstable_by_table: dict[str, dict] = {}
    resident_only_by_table: dict[str, str] = {}
    distribution_tags: dict[str, str] = {}
    if os.path.exists(script_path):
        with open(script_path, encoding="utf-8") as f:
            script_text = _truncate_at_exit_script(f.read())
        month_derived = _detect_month_derived_columns(script_text)
        groupby_count_tables = _detect_groupby_count_tables(script_text)
        renamed_by_table = _detect_simple_renamed_columns(script_text)
        chr_concat_by_table = _detect_chr_concat_columns(script_text)
        dual_key_by_table = _detect_dual_autonumber_keys(script_text)
        source_files_by_table = _detect_source_files(script_text)
        source_sheets_by_table = _detect_source_sheets(script_text)
        expr_columns_by_table = _detect_expr_columns_per_file(script_text)
        date_formats_by_table = _detect_date_format_columns_per_file(script_text)
        inline_tables = _detect_inline_tables(script_text)
        join_agg_by_table = _detect_join_resident_aggregations(script_text)
        mapping_tables = _detect_mapping_tables(script_text)
        applymap_by_table = _detect_applymap_subfield_columns(script_text)
        crosstable_by_table = _detect_crosstable_specs(script_text)
        resident_only_by_table = _detect_resident_only_tables(script_text)
        distribution_tags = _detect_report_distribution_tags(script_text)
        # A table built purely via `Resident <OtherTable>` (see
        # _detect_resident_only_tables) has no FROM of its own — borrow
        # whatever file(s)/sheet(s) its Resident SOURCE table itself
        # resolved to, following the chain more than one hop deep if the
        # source table is ALSO Resident-only (a scratch table built from
        # another scratch table before the real one is derived — seen in
        # practice as a 2-stage TempX -> TempY -> RealTable chain).
        for table_name, source_table in list(resident_only_by_table.items()):
            if table_name in source_files_by_table:
                continue
            seen = {table_name}
            hop = source_table
            while hop not in source_files_by_table and hop in resident_only_by_table and hop not in seen:
                seen.add(hop)
                hop = resident_only_by_table[hop]
            if hop in source_files_by_table:
                source_files_by_table[table_name] = source_files_by_table[hop]
                source_sheets_by_table[table_name] = source_sheets_by_table.get(hop, [])

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

        if _DISTRIBUTION_TABLE_NAME_RE.match(table_name):
            # Qlik's own in-application report-distribution/bursting
            # infrastructure (recipient list, groups, per-recipient
            # filters) — not business data, and out of scope for a
            # report-only conversion (this pipeline produces a Power BI
            # report/model, not an automated distribution system). Never
            # created as a Power BI table; the fields it carried are
            # logged instead so the underlying business requirement
            # (who receives what, filtered how, via which groups) isn't
            # silently lost — a person can map that onto whichever real
            # Power BI distribution mechanism (subscriptions, Power
            # Automate, RLS) the actual scope needs, if any.
            field_names = [f.get("qName") or f.get("name") for f in raw_fields if f.get("qName") or f.get("name")]
            described = [f"{f} ({distribution_tags[f]})" if f in distribution_tags else f for f in field_names]
            print(f"[build] SKIPPED table '{table_name}': Qlik in-app report-distribution infrastructure "
                  f"(recipient/group/filter configuration for Qlik's own reporting feature), not business "
                  f"data — not reproduced as a Power BI table. Fields it carried, for manual mapping onto "
                  f"whichever Power BI distribution mechanism the actual scope needs: "
                  f"{', '.join(described) if described else '(none)'}")
            continue

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
            override_key = f"{table_name}.{fname}"
            if override_key in type_overrides:
                if type_overrides[override_key] != dtype:
                    print(f"[build] {table_name}[{fname}]: type_overrides.json says '{type_overrides[override_key]}' "
                          f"(auto-detected would have been '{dtype}') — using the override")
                dtype = type_overrides[override_key]
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

        if table_name in crosstable_by_table and table_name not in inline_tables:
            # Crosstable() over a FROM-file source (not INLINE) — the wide
            # source file itself would need an M unpivot step
            # (Table.UnpivotOtherColumns) to match the narrow qualifier +
            # attribute + value shape Qlik's own qFields list already
            # reports in data_model.json. Only the INLINE case is unpivoted
            # automatically right now (see _detect_inline_tables) — flag
            # this loudly instead of silently producing a table whose
            # columns don't match what's actually in the source file.
            spec = crosstable_by_table[table_name]
            print(f"[build] WARNING: {table_name} uses Crosstable({spec['attribute_field']}, "
                  f"{spec['value_field']}) over a FROM-file source — this unpivot isn't reproduced "
                  f"automatically yet (only a Crosstable over LOAD ... INLINE is); the generated M will "
                  f"select {spec['attribute_field']}/{spec['value_field']} from the CSV directly, which "
                  f"will come back blank since the file's real columns are still in their original wide "
                  f"shape. Add a Table.UnpivotOtherColumns step by hand in Power BI Desktop's Advanced "
                  f"Editor for this table.")

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
        # A computed column ('Sale' as RecordType, upper(ZoneName) as
        # ZM_ZONENAME, (Qty*UnitPrice)-Discount as NetAmount) is never a
        # real column in any of this table's source files either — same
        # exclusion reasoning as join_agg_aliases/computed, see
        # _detect_expr_columns_per_file's own docstring for why this
        # matters (a silently-nulled column makes every DAX filter/measure
        # keyed on it quietly return BLANK). NOTE: this exclusion must NOT
        # be applied here, globally, across every file — a name can be a
        # REAL raw column in one source file and a COMPUTED alias in
        # another (seen: "Qty" is a genuine column in the 4 Sale files but
        # `-ReturnQty as Qty` in the Returns file) — excluding it from the
        # column list passed to EVERY file would wrongly drop it from the
        # files where it's real too, leaving it blank there once
        # Table.Combine merges everything by name. `generate_partition_m`/
        # `generate_combined_partition_m` already do this exclusion
        # correctly PER FILE internally (each file's own `expr_columns`
        # dict, threaded through separately) — only `join_agg_aliases`
        # (genuinely never a real column in ANY file) belongs here.
        expr_columns_per_file = expr_columns_by_table.get(table_name)
        expr_column_names = {name for d in (expr_columns_per_file or []) for name in d}
        date_formats_per_file = date_formats_by_table.get(table_name)
        base_columns = [c for c in columns if c["name"] not in join_agg_aliases]

        # Use the script's own FROM filename(s) whenever they were found —
        # NEVER assume the source file is named after the table. Only fall
        # back to guessing "{table}.csv" when the script parse found no
        # FROM clause at all for this table (e.g. it's RESIDENT/INLINE-only
        # and genuinely has no file of its own).
        detected_files = source_files_by_table.get(table_name)
        detected_sheets = source_sheets_by_table.get(table_name)
        multi_source_files = detected_files if detected_files and len(detected_files) > 1 else None
        csv_filename = None
        if multi_source_files:
            print(f"[build] {table_name}: built from {len(multi_source_files)} source files in the Qlik "
                  f"script, in this order — {', '.join(multi_source_files)} — combining all of them "
                  f"(Table.Combine) instead of loading only the first")
            if expr_column_names:
                print(f"[build] {table_name}: {', '.join(sorted(expr_column_names))} computed from the Qlik "
                      f"script per source file (a literal/text-function/arithmetic expression, not a real "
                      f"file column) — reproducing each file's own computation instead of selecting it "
                      f"from the CSV (which would silently null it)")
            if date_formats_per_file and any(date_formats_per_file):
                fmt_cols = {c for d in date_formats_per_file for c in d}
                print(f"[build] {table_name}: {', '.join(sorted(fmt_cols))} parsed with each source file's "
                      f"own explicit Qlik date#() format string (can differ per file) instead of one "
                      f"generic guess")
            m_expression = generate_combined_partition_m(
                table_name, multi_source_files, base_columns,
                source_ref=SOURCE_DATA_PARAM_NAME, sql=_SQL_PARTITION_REFS, computed=computed, renames=renames,
                expr_columns_per_file=expr_columns_per_file, date_formats_per_file=date_formats_per_file,
                sheet_names=detected_sheets,
            )
        else:
            csv_filename = detected_files[0] if detected_files else f"{_safe(table_name)}.csv"
            sheet_name = detected_sheets[0] if detected_sheets else None
            if detected_files and csv_filename.casefold() != f"{_safe(table_name)}.csv".casefold():
                print(f"[build] {table_name}: source file is '{csv_filename}' per the Qlik script "
                      f"(not '{_safe(table_name)}.csv')" + (f", sheet '{sheet_name}'" if sheet_name else ""))
            elif sheet_name:
                print(f"[build] {table_name}: reading worksheet '{sheet_name}' from '{csv_filename}' "
                      f"per the Qlik script's (ooxml, ..., table is {sheet_name}) qualifier")
            single_expr_columns = expr_columns_per_file[0] if expr_columns_per_file else None
            single_date_formats = date_formats_per_file[0] if date_formats_per_file else None
            m_expression = generate_partition_m(
                table_name, csv_filename, base_columns,
                source_ref=SOURCE_DATA_PARAM_NAME, sql=_SQL_PARTITION_REFS, computed=computed, renames=renames,
                expr_columns=single_expr_columns, date_formats=single_date_formats,
                sheet_name=sheet_name,
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
            "expr_columns_per_file": expr_columns_per_file, "date_formats_per_file": date_formats_per_file,
            "sheet_names": detected_sheets, "sheet_name": (detected_sheets[0] if detected_sheets else None),
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

    # Ground-truth number format per master measure, keyed by Qlik's own
    # title (the DAX conversion's own "name" output is that same title
    # verbatim — confirmed: extracted measures.json's "title" and
    # measures.converted.json's "name" match exactly for every master
    # measure) — see _qlik_num_format_to_dax's own docstring for why this
    # overrides whatever (if anything) the LLM guessed.
    raw_measures = _load_json(extracted_dir, "measures.json")
    raw_measures_list = raw_measures if isinstance(raw_measures, list) else raw_measures.get("measures", [])
    num_format_by_title = {
        item["title"].casefold(): item.get("num_format")
        for item in raw_measures_list if item.get("title")
    }

    for m in _load_json(converted_dir, "measures.converted.json").get("measures", []):
        table = _real_table(m.get("table", ""))
        m = {**m, "table": table}
        dax_format = _qlik_num_format_to_dax(num_format_by_title.get((m.get("name") or "").casefold()))
        if dax_format:
            m["format_string"] = dax_format
            if dax_format.rstrip().endswith("%"):
                # A "0%"-style DAX format string ALSO auto-multiplies its
                # underlying value by 100 for display, the same convention
                # Qlik uses — so this is correct IF the measure's own DAX
                # expression already evaluates to a 0-1 fraction the way
                # Qlik's did. There's no way to know that from qNumFormat
                # alone (it describes the DISPLAY mask, not whether the
                # expression itself already contains a manual "* 100"),
                # and a Qlik author occasionally does both — get the wrong
                # magnitude by 100x either way (15% shown as 1500%, or 0.15%
                # shown for what should be 15%) and it's silent, since the
                # value still looks like a plausible number. Flag every
                # percent-formatted measure once, for a one-time manual
                # check against the actual displayed value.
                print(f"[build] WARNING: measure '{m['name']}' on '{table}' got a percent DAX format "
                      f"string ({dax_format!r}) from Qlik's own qNumFormat — verify the measure's DAX "
                      f"expression evaluates to a 0-1 fraction (not already pre-multiplied by 100), or "
                      f"the displayed percentage will be off by a factor of 100.")
        measures_by_table.setdefault(table, []).append(m)
        original_measure_table[m["name"].casefold()] = (table, m["name"])

    # A variable that's ALSO a what-if parameter or a scenario picker (see
    # _apply_what_if_parameters / _apply_variable_scenarios, both called
    # AFTER this function returns) gets its own dedicated table + measure
    # later, targeting exactly its own variable name as BOTH the table and
    # the measure name. Until then, that table genuinely doesn't exist yet
    # — so `v.get("table")` here (parameters_variables.skill.md's own
    # best-guess placeholder, commonly the variable's own name too) is
    # never a real table, and _real_table's generic orphan-fallback would
    # silently plant a measure of that SAME name on some unrelated table
    # (whichever happens to be first) instead. That's actively wrong, not
    # just imprecise: once the real table+measure are created later, the
    # model ends up with TWO measures sharing one name, and the model-wide
    # measure-name dedupe keeps this wrongly-placed EARLIER one (renaming
    # the correct, later-created one instead) — the earlier one's own DAX,
    # which forward-references the not-yet-real table/measure by name,
    # then resolves to ITSELF, a circular dependency Power BI refuses to
    # evaluate at all. Skip creating this placeholder measure entirely for
    # such a variable; the later pass creates the real, correctly-homed one.
    what_if_variable_names = {p["variable"] for p in detect_what_if_parameters(extracted_dir)}
    scenario_variable_names = {s["variable"] for s in detect_variable_scenarios(extracted_dir)}
    deferred_variable_names = what_if_variable_names | scenario_variable_names

    for v in _load_json(converted_dir, "variables.converted.json").get("variables", []):
        if v.get("name") in deferred_variable_names:
            continue
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
            expr_columns_per_file = table.get("expr_columns_per_file")
            date_formats_per_file = table.get("date_formats_per_file")
            # See the matching comment in _assemble_semantic_inputs: expr
            # column exclusion must stay PER FILE (handled internally by
            # generate_partition_m/generate_combined_partition_m via the
            # per-file expr_columns dicts below), never applied globally
            # here — a name can be a real column in one file and a
            # computed alias in another.
            base_columns = [c for c in table["columns"] if c["name"] not in join_agg_aliases]
            multi_source_files = table.get("multi_source_files")
            if multi_source_files:
                m_expression = generate_combined_partition_m(
                    tname, multi_source_files, base_columns,
                    source_ref=SOURCE_DATA_PARAM_NAME, sql=_SQL_PARTITION_REFS,
                    computed=table.get("computed"), renames=table.get("renames"),
                    expr_columns_per_file=expr_columns_per_file, date_formats_per_file=date_formats_per_file,
                    sheet_names=table.get("sheet_names"),
                )
            else:
                single_expr_columns = expr_columns_per_file[0] if expr_columns_per_file else None
                single_date_formats = date_formats_per_file[0] if date_formats_per_file else None
                m_expression = generate_partition_m(
                    tname, table.get("csv_filename") or f"{_safe(tname)}.csv", base_columns,
                    source_ref=SOURCE_DATA_PARAM_NAME, sql=_SQL_PARTITION_REFS,
                    computed=table.get("computed"), renames=table.get("renames"),
                    expr_columns=single_expr_columns, date_formats=single_date_formats,
                    sheet_name=table.get("sheet_name"),
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
            if measure_index[key].casefold() == name.casefold():
                # Never "fix" a measure's own phantom-table reference into
                # a bare reference to ITSELF — this happens whenever a
                # variable name doubles as both the DAX measure's own name
                # and the column name inside an invented, never-materialized
                # parameter-table reference (e.g. `'vCurrentSheet
                # Parameter'[vCurrentSheet]` on the `vCurrentSheet` measure
                # itself — a real, confirmed case). "Correcting" that to
                # `[vCurrentSheet]` makes the measure reference itself, a
                # genuine circular dependency Power BI Desktop rejects on
                # open. Leave the table-qualified form in place instead;
                # `_fix_phantom_table_refs` (run right after this) already
                # has its own matching self-reference guard and will
                # correctly fall through to BLANK() with a proper warning,
                # since the phantom parameter table genuinely doesn't exist.
                return match.group(0)
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
            if col.casefold() in measure_names and measure_names[col.casefold()].casefold() != name.casefold():
                # Guard against repointing a measure's phantom-table
                # reference to ITSELF: a Qlik variable name (e.g.
                # `vCurrentSheet`) very commonly becomes both the DAX
                # measure's own name AND the column name inside an
                # LLM-invented (never actually materialized) parameter
                # table reference like `'vCurrentSheet Parameter'
                # [vCurrentSheet]` — column name and measure name collide
                # by construction, not coincidence. Without this check,
                # `[vCurrentSheet]` "resolves" to the very measure being
                # defined, producing a real, confirmed circular-dependency
                # error in Power BI Desktop ("Measure: 'X'[vCurrentSheet],
                # Measure: 'X'[vCurrentSheet]") on open. Fall through to
                # the unresolved/BLANK() path instead — the phantom
                # parameter table genuinely doesn't exist, so there's
                # nothing else this expression can correctly resolve to.
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


def _apply_variable_scenarios(
    extracted_dir: str,
    tables: dict[str, dict],
    measures_by_table: dict[str, list[dict]],
    pages: list[dict],
    original_measure_table: dict[str, tuple[str, str]] | None = None,
) -> None:
    """A Qlik "scenario picker" (several action-buttons on one sheet, each
    setting the SAME variable to a different fixed value — e.g.
    "Standard"/"Enhanced"/"Aggressive" buttons all setting `vDunning`)
    converts to a disconnected table + SELECTEDVALUE() measure + a Slicer,
    replacing the button row — see scenario_buttons.py for why buttons
    themselves can't carry this over (Power BI has no "set this DAX value"
    button action, so the buttons alone would stay permanently inert).

    A scenario variable is deliberately EXCLUDED from the earlier
    variables.converted.json measure-creation pass in
    _assemble_semantic_inputs (see its own comment — creating a same-named
    placeholder measure on the wrong table before this table exists risks
    a circular self-reference once both end up sharing one name), so
    "replaced" below is normally never true in practice; the "not replaced"
    branch — creating the measure fresh, correctly homed on this scenario's
    own table from the start — is the expected path, not a rare fallback.
    """
    for scenario in detect_variable_scenarios(extracted_dir):
        var_name = scenario["variable"]
        values = scenario["values"]
        table_name = f"{var_name} Scenario"
        if table_name in tables:
            print(f"[build] WARNING: scenario table '{table_name}' collides with an existing table name — skipping")
            continue

        m_expression, data_type = generate_scenario_table_m(values)
        tables[table_name] = {
            "columns": [{"name": "Value", "data_type": data_type, "source_column": "Value"}],
            "m_expression": m_expression,
        }

        default = values[0]
        default_literal = default if data_type == "double" else f'"{default}"'
        new_expression = f"SELECTEDVALUE('{table_name}'[Value], {default_literal})"

        # Replace whichever table's existing measure for this variable
        # (added by the script/parameters conversion, as a constant) —
        # search by name rather than assuming which table, since that
        # measure's home table was that earlier pass's own judgment call.
        replaced = False
        for rows in measures_by_table.values():
            for row in rows:
                if row.get("name") == var_name:
                    row["expression"] = new_expression
                    replaced = True
        if not replaced:
            # The expected path (see docstring) — add it fresh on the new
            # table, and register it so any OTHER measure/visual that
            # references this variable by bracket name resolves correctly.
            measures_by_table.setdefault(table_name, []).append({"name": var_name, "expression": new_expression})
            if original_measure_table is not None:
                original_measure_table[var_name.casefold()] = (table_name, var_name)

        # Swap the button row for one Slicer covering the same bounds —
        # every button this variable's buttons occupy is removed and
        # replaced by a single visual, on whichever page they were on.
        button_ids = set(scenario["button_ids"])
        for page in pages:
            matched = [v for v in page.get("visuals", []) if v.get("name") in button_ids]
            if not matched:
                continue
            positions = [v.get("position") or {} for v in matched]
            xs = [p.get("x", 0) for p in positions]
            ys = [p.get("y", 0) for p in positions]
            rights = [p.get("x", 0) + p.get("width", 0) for p in positions]
            bottoms = [p.get("y", 0) + p.get("height", 0) for p in positions]
            slicer_visual = {
                "name": f"scenario-slicer-{_safe(var_name)}",
                "position": {
                    "x": min(xs), "y": min(ys),
                    "z": min(p.get("z", 0) for p in positions),
                    "width": max(rights) - min(xs), "height": max(bottoms) - min(ys),
                    "tabOrder": min(p.get("tabOrder", 0) for p in positions),
                },
                "visual": {
                    "visualType": "slicer",
                    "query": {"queryState": {"Values": {"projections": [
                        {"field": {"Column": {"Expression": {"SourceRef": {"Entity": table_name}}, "Property": "Value"}},
                         "queryRef": f"{table_name}.Value"}
                    ]}}},
                    "objects": {},
                },
                "confidence": "high",
                "notes": f"Replaces {len(matched)} Qlik 'setVariable' action-button(s) that set '{var_name}' — "
                         f"Power BI has no button action that sets a DAX value, so a slicer bound to a real, "
                         f"interactive scenario table is the working equivalent.",
            }
            page["visuals"] = [v for v in page["visuals"] if v.get("name") not in button_ids] + [slicer_visual]
            print(f"[build] '{var_name}': replaced {len(matched)} scenario button(s) "
                  f"({', '.join(str(v) for v in values)}) with a Slicer bound to '{table_name}[Value]'")


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


def _build_container_id_map(extracted_dir: str) -> dict[str, str]:
    """Map a container CHILD object's own id to the id of the Qlik
    container it came from (see extract/extractor.py's _expand_container,
    which now records this directly on each child it emits) — used to
    recover "these tiles were grouped together in Qlik" at build time,
    since the LLM's own converted visual JSON only carries each visual's
    own id, not which container it was a sibling within."""
    sheets_path = os.path.join(extracted_dir, "sheets.json")
    if not os.path.exists(sheets_path):
        return {}
    with open(sheets_path, encoding="utf-8") as f:
        sheets = json.load(f)

    out: dict[str, str] = {}

    def walk(node):
        if isinstance(node, dict):
            obj_id = node.get("id")
            container_id = node.get("container_id")
            if obj_id and container_id:
                out[obj_id] = container_id
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(sheets)
    return out


# A container-child object's own id is synthesized (see modules/extract's
# _expand_container) as "qlik-compound-context-<container_id>-link-<child_id>-qlik"
# — the container it came from is embedded directly in the name, so which
# KPI tiles were siblings inside the SAME Qlik container can be recovered
# from names alone, with no extra metadata needing to be threaded through
# extraction -> conversion -> build.
_CONTAINER_CHILD_NAME_RE = re.compile(r"^qlik-compound-context-([0-9a-fA-F-]+)-link-")

# Only merge visual types that are genuinely just "one value on display" —
# merging a chart or a table into a KPI strip wouldn't make sense, and isn't
# what a Qlik container of several KPI tiles maps to anyway.
_MERGEABLE_KPI_VISUAL_TYPES = {"card", "multiRowCard"}


def _merge_container_kpi_siblings(pages: list[dict], container_id_map: dict[str, str] | None = None) -> None:
    """Several separate Qlik KPI objects placed together inside the SAME
    container (not a single KPI's own primary+secondary pair — see the
    multiRowCard rule in sheets_convert.skill.md for that case, handled by
    the LLM itself) each convert to their own independent Card visual by
    default, one per object — scattered across the page as N unrelated
    tiles instead of the one grouped KPI strip Qlik itself shows. Merge
    every card-type sibling that shares a container into a single
    multiRowCard, in their original child order, covering all of their
    combined bounds — the same "one Qlik grouping, one Power BI visual"
    principle as the primary+secondary KPI fix, generalized to any number
    of siblings.

    Grouping is read from `container_id_map` (object id -> container id,
    built from extraction's own recorded data — reliable for every
    container shape) when available; a container child whose id follows
    the older "qlik-compound-context-<id>-link-" naming convention is
    still grouped correctly via that pattern as a fallback, for
    extracted/ data from before this map existed."""
    container_id_map = container_id_map or {}
    for page in pages:
        visuals = page.get("visuals", [])
        groups: dict[str, list[dict]] = {}
        for visual in visuals:
            name = visual.get("name") or ""
            container_id = container_id_map.get(name)
            if not container_id:
                m = _CONTAINER_CHILD_NAME_RE.match(name)
                container_id = m.group(1) if m else None
            visual_type = (visual.get("visual") or {}).get("visualType")
            if container_id and visual_type in _MERGEABLE_KPI_VISUAL_TYPES:
                groups.setdefault(container_id, []).append(visual)

        for container_id, members in groups.items():
            if len(members) < 2:
                continue  # a lone KPI in its container is already handled as a normal single card

            members.sort(key=lambda v: (v.get("position") or {}).get("tabOrder", 0))

            projections = []
            for member in members:
                qs = ((member.get("visual") or {}).get("query") or {}).get("queryState") or {}
                values = qs.get("Values") or {}
                projections.extend(values.get("projections") or [])
            if not projections:
                continue  # nothing resolvable to show — leave the individual (already-pruned) visuals as-is

            positions = [m.get("position") or {} for m in members]
            xs = [p.get("x", 0) for p in positions]
            ys = [p.get("y", 0) for p in positions]
            rights = [p.get("x", 0) + p.get("width", 0) for p in positions]
            bottoms = [p.get("y", 0) + p.get("height", 0) for p in positions]
            merged_position = {
                "x": min(xs), "y": min(ys),
                "z": min(p.get("z", 0) for p in positions),
                "width": max(rights) - min(xs), "height": max(bottoms) - min(ys),
                "tabOrder": min(p.get("tabOrder", 0) for p in positions),
            }

            merged_visual = {
                "name": f"kpi-group-{container_id}",
                "position": merged_position,
                "visual": {
                    "visualType": "multiRowCard",
                    "query": {"queryState": {"Values": {"projections": projections}}},
                    "objects": {},
                },
                "confidence": "medium",
                "notes": f"Merged {len(members)} KPI tiles that shared one Qlik container "
                         f"('{container_id}') into a single multiRowCard, in their original order.",
            }

            member_names = {m.get("name") for m in members}
            page["visuals"] = [v for v in page["visuals"] if v.get("name") not in member_names] + [merged_visual]


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


def _strip_nested_query_ref(node) -> None:
    """The LLM occasionally nests a `queryRef` key INSIDE the `field` object
    itself (e.g. `field: {Column: {...}, queryRef: "..."}` or even deeper,
    inside `Column`/`Measure`/`Aggregation.Expression.Column`) instead of
    where PBIR's schema actually requires it — as a SIBLING of `field` on
    the projection itself. Power BI Desktop rejects the whole report over
    this ("An additional property 'queryRef' was included in the .../field
    property"), even though `_fix_query_refs` below already writes the
    correct sibling `queryRef` — the stray nested copy has to be removed,
    not just overridden, or the correct and incorrect copies coexist and
    Desktop still rejects the file. Recurses through the field's known
    wrapper shapes so a `queryRef` nested at any depth is caught."""
    if not isinstance(node, dict):
        return
    node.pop("queryRef", None)
    for key in ("Column", "Measure", "Aggregation", "Expression"):
        _strip_nested_query_ref(node.get(key))


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
            query_state = ((visual.get("visual") or {}).get("query") or {}).get("queryState") or {}
            if not isinstance(query_state, dict):
                continue
            for value in query_state.values():
                if not isinstance(value, dict):
                    continue
                for proj in value.get("projections", []) or []:
                    if not isinstance(proj, dict):
                        continue
                    field = proj.get("field")
                    _strip_nested_query_ref(field)
                    entity, prop = _entity_and_property_of_field(field)
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
            query_state = ((visual.get("visual") or {}).get("query") or {}).get("queryState") or {}
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


_SYSTEM_FIELD_TOKEN_RE = re.compile(r"\$[A-Za-z]\w*")


def _visual_has_system_field(visual: dict) -> bool:
    """True if any projection in this visual's queryState binds a Qlik
    SYSTEM field — $Table, $Field, $Rows, $Info, $Occurrence, and the like
    (Qlik's own built-in introspection fields, exposed by things like its
    "Data Model Viewer" sheet). These describe the Qlik app's OWN metadata,
    not real data, so no equivalent table/column exists (or ever will) in
    the Power BI model to bind them to.

    Two different shapes have to be checked, not just one: report_visuals
    sometimes still points the binding at a FABRICATED table (seen:
    "Model") with a `"Property"` value that starts with `$` — the original
    case this scanned for. But when it instead correctly recognizes the
    field as unresolvable and sets `"field": null`, the ONLY place the
    system-field name survives is the projection's own `queryRef` string
    (e.g. `"$Table (unresolved)"`) — a `null` field short-circuits a scan
    that only ever looks inside dict/list nodes, so that case silently
    fell through to the generic unresolved-projection pruner instead of
    this dedicated, clearer textbox fallback, leaving an empty table/chart
    with no visible explanation. Scanning every STRING value (not just
    "Property") for a `$Word`-shaped token catches both."""
    query_state = ((visual.get("visual") or {}).get("query") or {}).get("queryState") or {}
    def scan(node):
        if isinstance(node, dict):
            return any(scan(v) for v in node.values())
        if isinstance(node, list):
            return any(scan(v) for v in node)
        if isinstance(node, str):
            return bool(_SYSTEM_FIELD_TOKEN_RE.search(node))
        return False
    return scan(query_state)


def _replace_system_field_visuals(pages: list[dict]) -> None:
    for page in pages:
        for visual in page.get("visuals", []):
            if not _visual_has_system_field(visual):
                continue
            old_type = (visual.get("visual") or {}).get("visualType", "visual")
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
            page = json.load(f)
        # Defense in depth: modules/sheet/visuals.py already filters a
        # malformed (non-dict) LLM "visuals" entry before writing this
        # file, but an OLDER converted/ file written before that fix
        # existed (or a page written some other way) could still have one
        # on disk — drop it here too rather than crash every visual on the
        # page over one bad entry ("'int' object has no attribute 'get'").
        visuals = page.get("visuals", [])
        good_visuals = [v for v in visuals if isinstance(v, dict)]
        if len(good_visuals) != len(visuals):
            print(f"[build] WARNING: {os.path.basename(path)} has "
                  f"{len(visuals) - len(good_visuals)} malformed (non-object) visual entr"
                  f"{'y' if len(visuals) - len(good_visuals) == 1 else 'ies'} on disk — dropping "
                  f"rather than crashing the build; re-run convert for this app to regenerate cleanly")
            page["visuals"] = good_visuals
        pages.append(page)
    _normalize_query_states(pages)
    return pages


# Deterministic Qlik object `type` -> Power BI `visualType` for every case
# that has exactly ONE right answer regardless of the object's own
# content — kept here as ground truth, applied as an OVERRIDE over
# whatever the LLM chose (see _apply_deterministic_visual_types below),
# the same "ground truth wins" pattern as _qlik_num_format_to_dax and
# type_overrides.json. This exists because prompt-only guidance in
# sheets_convert.skill.md was NOT reliably followed even when tested
# correctly in isolation — a real app's `combochart` with
# `orientation: "horizontal"` and only 1 measure (no real combo) was
# converted correctly to `clusteredBarChart` in an isolated single-object
# test call, but back to the wrong `lineClusteredColumnComboChart` when
# converted as part of a normal full-sheet batch — LLM output for a
# well-specified but easy-to-miss rule is not consistent enough to trust
# on its own for something this mechanical.
_QLIK_TO_PBI_SIMPLE_VISUAL_TYPE = {
    "linechart": "lineChart",
    "piechart": "pieChart",
    "treemap": "treemap",
    "scatterplot": "scatterChart",
    "table": "tableEx",
    "sn-table": "tableEx",
    "pivot-table": "pivotTable",
    "gauge": "gauge",
    "bulletchart": "gauge",
    "listbox": "slicer",
}


def _deterministic_visual_type(qlik_type: str, props: dict, measure_count: int) -> str | None:
    """Returns the ONE correct Power BI visualType for a Qlik object whose
    conversion doesn't require any judgment call — orientation, stacking,
    and combo-vs-plain-bar are all explicit, readable properties on the
    object itself, never something to infer/guess. Returns None for a
    Qlik type this function has no deterministic opinion about (the LLM's
    own choice is trusted as-is for those)."""
    qlik_type = (qlik_type or "").casefold()
    if qlik_type in _QLIK_TO_PBI_SIMPLE_VISUAL_TYPE:
        return _QLIK_TO_PBI_SIMPLE_VISUAL_TYPE[qlik_type]

    if qlik_type in ("barchart", "combochart"):
        # A `combochart` with fewer than 2 measures has no second series to
        # actually "combo" with — Qlik authors commonly pick the combo
        # object purely as a general-purpose bar/column chart even with
        # nothing to combine, so it degrades to the same orientation-aware
        # plain bar/column choice as a real `barchart`. With 2+ measures
        # it's a genuine bar+line combination, which Power BI only offers
        # in a COLUMN-based combo visual (no horizontal-bar combo exists
        # natively) — orientation is moot there, so no override is applied
        # and the LLM's own combo-visual choice is trusted.
        if qlik_type == "combochart" and measure_count >= 2:
            return None
        orientation = (props.get("orientation") or "vertical").casefold()
        if orientation == "horizontal":
            return "clusteredBarChart"
        return "columnChart" if props.get("stacked") else "clusteredColumnChart"

    return None


def _build_qlik_object_index(extracted_dir: str) -> dict[str, dict]:
    """{object_id: {"type": Qlik type, "properties": layout.properties,
    "measure_count": int}} for every object across every sheet — the
    ground truth _apply_deterministic_visual_types compares the LLM's
    chosen visualType against."""
    path = os.path.join(extracted_dir, "sheets.json")
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    sheets = data if isinstance(data, list) else data.get("sheets", [])
    index: dict[str, dict] = {}
    for sheet in sheets:
        for obj in sheet.get("objects", []):
            obj_id = obj.get("id")
            if not obj_id:
                continue
            props = obj.get("layout", {}).get("properties", {}) or {}
            hc = props.get("qHyperCubeDef", {}) or {}
            index[obj_id] = {
                "type": obj.get("type"),
                "properties": props,
                "measure_count": len(hc.get("qMeasures", []) or []),
            }
    return index


def _apply_deterministic_visual_types(pages: list[dict], extracted_dir: str) -> None:
    """Overrides visual.visualType with the deterministic answer (see
    _deterministic_visual_type) whenever one exists, regardless of what
    the LLM chose — applied AFTER conversion so it's a guaranteed
    correction rather than a hope that the skill's own guidance was
    followed."""
    qlik_objects = _build_qlik_object_index(extracted_dir)
    if not qlik_objects:
        return
    for page in pages:
        for visual in page.get("visuals", []):
            obj_id = visual.get("name")
            info = qlik_objects.get(obj_id)
            if not info:
                continue
            visual_obj = visual.get("visual")
            if not isinstance(visual_obj, dict):
                continue
            deterministic = _deterministic_visual_type(info["type"], info["properties"], info["measure_count"])
            if deterministic and visual_obj.get("visualType") != deterministic:
                print(f"[build] corrected visual '{obj_id}' visualType "
                      f"'{visual_obj.get('visualType')}' -> '{deterministic}' "
                      f"(deterministic, from Qlik's own '{info['type']}'/orientation/measure-count)")
                visual_obj["visualType"] = deterministic


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
            visual_obj = visual.get("visual") or {}
            if not isinstance(visual_obj, dict):
                continue
            visual_type = visual_obj.get("visualType")

            # A visual with no data role at all (textbox/shape/actionButton)
            # is correctly modeled with no "query" key — but the conversion
            # sometimes emits the key anyway with a literal `null` value.
            # pbip-compiler's own PBIR pass calls .get("queryState", {}) on
            # that value unconditionally and crashes on the None, so
            # normalize to a real (possibly empty) query object here.
            if "query" in visual_obj and visual_obj["query"] is None:
                visual_obj["query"] = {"queryState": {}}

            query_state = (visual_obj.get("query") or {}).get("queryState") or {}
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


_RELATED_CALL_RE = re.compile(r"\bRELATED\s*\(\s*'?([A-Za-z_][\w ]*?)'?\s*\[", re.IGNORECASE)


def _collect_related_orientations_from_measures(
    measures_by_table: dict[str, list[dict]], calc_cols_by_table: dict[str, list[dict]],
) -> list[tuple[str, str]]:
    """Scan every measure/calculated-column's final DAX for `RELATED(Table[
    ...])` calls and return (home_table, related_table) pairs — the home
    table (where the measure/column itself lives, i.e. the side DOING the
    iterating) MUST be the "many" side of its relationship to whichever
    table RELATED() reaches into, or Power BI Desktop errors with "doesn't
    have a relationship to any table available in the current context" even
    though the column genuinely exists (see the caller for why this can
    disagree with the data-model conversion's own relationship direction)."""
    orientations: list[tuple[str, str]] = []
    for home_table, rows in measures_by_table.items():
        for row in rows:
            for m in _RELATED_CALL_RE.finditer(row.get("expression") or ""):
                related_table = m.group(1).strip()
                if related_table and related_table != home_table:
                    orientations.append((home_table, related_table))
    for home_table, rows in calc_cols_by_table.items():
        for row in rows:
            for m in _RELATED_CALL_RE.finditer(row.get("expression") or ""):
                related_table = m.group(1).strip()
                if related_table and related_table != home_table:
                    orientations.append((home_table, related_table))
    return orientations


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


def _drop_relationships_with_non_unique_one_side(relationships: list[dict], tables: dict[str, dict]) -> list[dict]:
    """A relationship's `to_table`/`to_column` (the "one" side) MUST hold
    unique values, or Power BI's refresh-time validation rejects the whole
    table's load ("contains a duplicate value '...' and this is not
    allowed for columns on the one side of a many-to-one relationship") —
    and critically, marking the relationship `is_active: false` does NOT
    exempt it from this check. Confirmed in practice: data_model.skill.md
    correctly spotted a circular-reference risk (Fact_Orders -> Dim_Zones
    <- Fact_Zone_Performance -> Fact_Orders) and deactivated the
    Fact_Orders -> Fact_Zone_Performance link to break the cycle — the
    RIGHT instinct — but Fact_Zone_Performance is a `LEFT JOIN ... GROUP
    BY ZoneID, Month` aggregate (see _detect_join_resident_aggregations):
    its real grain is the FULL {ZoneID, Month} composite, so `ZoneID`
    alone repeats once per month and is never unique — deactivating that
    relationship wasn't enough, it needed to be DROPPED entirely, the same
    conclusion `_drop_relationships_into_related_calc_tables` already
    reaches for a DAX calculated table's still-nonexistent-at-validation-
    time columns, just for a different underlying reason.

    Detects this from each such table's own known GROUP BY key (recorded
    on `tables[name]["join_spec"]["group_by"]` when it was built) rather
    than sampling real data — the composite-vs-single-column mismatch is
    already known for certain, no uniqueness sampling required."""
    composite_keys: dict[str, set[str]] = {}
    for name, t in tables.items():
        join_spec = t.get("join_spec")
        if join_spec and len(join_spec.get("group_by", [])) > 1:
            composite_keys[name] = {g["alias"] for g in join_spec["group_by"]}

    if not composite_keys:
        return relationships

    kept = []
    for rel in relationships:
        one_side_table, one_side_column = rel["to_table"], rel["to_column"]
        full_key = composite_keys.get(one_side_table)
        if full_key and one_side_column in full_key and len(full_key) > 1:
            print(f"[build] NOTE: dropping relationship {rel['from_table']}[{rel['from_column']}] -> "
                  f"{one_side_table}[{one_side_column}] (even though it was marked inactive) — "
                  f"'{one_side_table}' is grouped by the composite key {sorted(full_key)}, so "
                  f"'{one_side_column}' alone repeats and Power BI's refresh-time validation rejects ANY "
                  f"relationship declaring it as the one/to side, active or not ('contains a duplicate "
                  f"value ... not allowed for columns on the one side'). {rel.get('notes', '')}")
            continue
        kept.append(rel)
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


def _warn_userelationship_rls_collision(relationships: list[dict], roles: list[dict]) -> None:
    """DAX's `USERELATIONSHIP()` — the function `_merge_relationships` above
    tells the user to reach for whenever it deactivates a relationship to
    break a cycle — cannot be used in any measure that touches a table with
    a Row-Level-Security role filter defined on it (Microsoft's own docs:
    "USERELATIONSHIP cannot be used when row level security is defined for
    the table"). This build already independently derives BOTH pieces —
    circular-reference-driven inactive relationships (`_merge_relationships`)
    and Section-Access-derived RLS roles (`rls.converted.json`) — but never
    cross-checks them against each other. If a table ends up in both sets,
    the `USERELATIONSHIP()` workaround this build's own printed guidance
    just told the user to add will actually error out in Power BI at query
    time. There is no automatic fix for this (redesigning around it is a
    real modeling decision, not something to silently pick for the user) —
    only surface it loudly enough to reach MANUAL_REVIEW.md."""
    rls_tables = {
        perm.get("table")
        for role in roles
        for perm in role.get("table_permissions", [])
        if perm.get("table")
    }
    if not rls_tables:
        return
    for rel in relationships:
        if rel.get("is_active") is False and (rel.get("from_table") in rls_tables or rel.get("to_table") in rls_tables):
            rls_table = rel["from_table"] if rel.get("from_table") in rls_tables else rel["to_table"]
            print(f"[build] WARNING: relationship {rel['from_table']}[{rel['from_column']}] -> "
                  f"{rel['to_table']}[{rel['to_column']}] was deactivated to break a circular reference, "
                  f"but '{rls_table}' also has a Row-Level Security role filter — USERELATIONSHIP() cannot "
                  f"be used in any measure on a table with RLS defined (Power BI will error at query time), "
                  f"so the usual 'activate it with USERELATIONSHIP() where needed' fix does not apply here. "
                  f"This needs a manual modeling decision (e.g. restructure the relationship instead of "
                  f"reactivating it per-measure).")


_QLIK_NUMFORMAT_CURRENCY_RE = re.compile(r"^[^\d#0.,\s]+")


def _qlik_num_format_to_dax(num_format: dict | None) -> str | None:
    """Translates a Qlik master measure's OWN configured display format —
    `qNumFormat` (see extractor.py's `_get_measures`: `{"qType": "M"|"R"|
    "F"|"I"|"U"|..., "qFmt": "<mask>", "qnDec": <int>, "qUseThou": 0|1}`) —
    into a Power BI/DAX `formatString` deterministically, the same
    "ground truth over LLM guess" philosophy as every other detector in
    this file. Before this, the DAX conversion had NO number-format
    information at all to work from (extraction never pulled qNumFormat
    until now) — every currency/percent/thousands-separator display was
    silently lost even though Qlik's OWN measure definition specified it
    (a real review: "AR at Risk -> no formatting (3.51M with title)",
    "Credit utilization% - 0.78 (no %)", "Total AR outstanding -> 16.22M
    (no $, comma separation, full digits combined)" — all three are this
    exact bug, on three different measures).

    Returns None when there's genuinely nothing to translate (no
    qNumFormat at all, or Qlik's own type is "U"/Unknown with no qFmt
    mask either — the measure's format was never configured in Qlik
    either, so there's nothing to reproduce)."""
    if not num_format or not isinstance(num_format, dict):
        return None
    q_type = (num_format.get("qType") or "U").upper()
    fmt_mask = num_format.get("qFmt") or ""
    n_dec = num_format.get("qnDec")
    use_thou = bool(num_format.get("qUseThou"))
    if q_type == "U" and not fmt_mask:
        return None

    decimals = int(n_dec) if isinstance(n_dec, int) else (fmt_mask.count("0", fmt_mask.find(".") + 1) if "." in fmt_mask else 0)
    decimal_part = ("." + "0" * decimals) if decimals > 0 else ""

    if "%" in fmt_mask or q_type == "P":
        return f"0{decimal_part}%"

    if q_type == "M":
        currency_match = _QLIK_NUMFORMAT_CURRENCY_RE.match(fmt_mask.strip())
        symbol = currency_match.group(0).strip() if currency_match else "$"
        return f'"{symbol}"#,##0{decimal_part}'

    if q_type in ("R", "F", "I", "U"):
        integer_part = "#,##0" if use_thou else "0"
        return f"{integer_part}{decimal_part}"

    return None  # date/time/interval qTypes — not a measure format string


def _load_json(directory: str, filename: str) -> dict:
    path = os.path.join(directory, filename)
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _safe(name: str) -> str:
    import re
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)
