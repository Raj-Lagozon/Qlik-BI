"""Pull script, data model, master measures/dimensions, variables, sheets and
section access out of a Qlik Cloud app via the Engine API, saving each as a
JSON/text file under extracted/<app_name>/."""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any

from app.setting import settings

from .client import QlikCloudClient, EngineSession
from .section_access import parse_section_access

HYPERCUBE_MAX_CELLS_PER_PAGE = 8_000   # Qlik Cloud tenants cap qHeight*qWidth per fetch well below on-prem defaults
HYPERCUBE_MIN_PAGE_HEIGHT = 1
MAX_ROWS_PER_TABLE = 500_000  # safety cap so a runaway table can't hang extraction

# _reload_doc() is what actually populates GetTablesAndKeys for a freshly
# imported app (see its docstring) — this retry is just a backstop for the
# rarer case where DoReload reports success but the engine takes a moment
# to reflect it, so a real problem still fails loudly instead of silently
# writing an empty data_model.json that later collapses the whole build
# down to just synthetic (e.g. what-if slider) tables.
_DATA_MODEL_RETRY_ATTEMPTS = 5
_DATA_MODEL_RETRY_DELAY_SECONDS = 3

_PAGE_TOO_LARGE_CODE = 6001

EXTRACTED_ROOT = str(settings.project_root / "extracted")


def extract_app(
    qvf_path: str,
    app_name: str | None = None,
    space_id: str | None = None,
    keep_app: bool = False,
) -> str:
    """Import a local .qvf into Qlik Cloud, extract everything the conversion
    pipeline needs, and write it to extracted/<app_name>/. Returns app_name.

    The .qvf is only imported so it can be read back through the Engine API —
    by default it's deleted from the tenant again once extraction finishes
    (or fails), so re-running extract repeatedly doesn't pile up apps in
    Qlik Cloud. Pass keep_app=True to leave it there for manual inspection.
    """
    client = QlikCloudClient(space_id=space_id)
    app_name = app_name or os.path.splitext(os.path.basename(qvf_path))[0]

    print(f"[extract] importing {qvf_path} into Qlik Cloud as '{app_name}'...")
    app_id = client.import_app(qvf_path, name=app_name)
    print(f"[extract] app id: {app_id}")

    out_dir = os.path.join(EXTRACTED_ROOT, app_name)
    os.makedirs(out_dir, exist_ok=True)

    try:
        with client.open_engine_session(app_id) as session:
            session.open_doc(app_id)
            _reload_doc(session)

            script = _get_script(session)
            _write_text(out_dir, "script.qvs", script)

            data_model = _get_data_model_with_retry(session, script)
            _write_json(out_dir, "data_model.json", data_model)

            measures = _get_measures(session)
            _write_json(out_dir, "measures.json", measures)

            dimensions = _get_dimensions(session)
            _write_json(out_dir, "dimensions.json", dimensions)

            variables = _get_variables(session)
            _write_json(out_dir, "variables.json", variables)

            sheets = _get_sheets(session)
            _write_json(out_dir, "sheets.json", sheets)

            section_access = parse_section_access(script)
            _write_json(out_dir, "section_access.json", section_access)

            # NOTE: .qvf row/record data is intentionally NOT extracted and no
            # data/*.csv files are written here (see _fetch_table_rows below
            # for the one narrow exception: a KPI-container config table's
            # own small set of rows, which are object/sheet metadata, not
            # business data, and are kept in memory only).
            kpi_containers = _detect_kpi_metadata_tables(session, data_model)
            _write_json(out_dir, "kpi_containers.json", kpi_containers)
            if kpi_containers:
                names = ", ".join(k["table"] for k in kpi_containers)
                print(f"[extract] detected KPI container config table(s): {names}")

            theme = _get_theme(session, variables, sheets)
            _write_json(out_dir, "theme.json", theme)
            if theme["colors"]:
                print(f"[extract] detected {len(theme['colors'])} theme color(s) "
                      f"({'app theme: ' + theme['app_theme_name'] if theme['app_theme_name'] else 'harvested from variables/visuals'})")
    finally:
        if keep_app:
            print(f"[extract] keeping app {app_id} in Qlik Cloud (--keep-app)")
        else:
            print(f"[extract] deleting temporary app {app_id} from Qlik Cloud...")
            try:
                client.delete_app(app_id)
            except Exception as exc:
                print(f"[extract] WARNING: could not delete app {app_id}: {exc}")

    _write_json(out_dir, "meta.json", {"app_name": app_name, "app_id": app_id if keep_app else None})
    print(f"[extract] done -> {out_dir}")
    return app_name


# ── individual pulls ────────────────────────────────────────────────────────

def _get_script(session: EngineSession) -> str:
    result = session.doc_call("GetScript", [])
    return result.get("qScript", "")


def _reload_doc(session: EngineSession) -> None:
    """A .qvf imported via the REST /apps/import endpoint arrives with its
    script and layout, but Qlik Cloud does NOT pre-populate the associative
    engine's table/field data from the file's own embedded state — that
    only happens once this session actually reloads it (confirmed directly:
    GetTablesAndKeys returns 0 tables before DoReload and every real table
    immediately after, even for a script that never touches data.qvf's own
    prior computed state). This runs unconditionally for every app, not
    just ones known to need it, since there's no reliable way to tell
    upfront whether an imported app already has a queryable model.

    A reload failing here (e.g. a lib:// connection this tenant hasn't
    registered) is not fatal on its own — GetScript/sheets/measures don't
    need it — so this only warns; _get_data_model_with_retry is what turns
    "reload didn't produce any tables" into a hard, actionable failure.
    """
    print("[extract] reloading app in Qlik Cloud so its data model is queryable...")
    try:
        result = session.doc_call("DoReload", [0, False, False])
    except Exception as exc:
        print(f"[extract] WARNING: reload raised an error: {exc}")
        return
    if result.get("qReturn") is not True:
        print(
            "[extract] WARNING: reload did not report success — the script's data "
            "connections may not resolve in this tenant. Continuing anyway; the "
            "data model may end up empty or partial."
        )


def _get_data_model(session: EngineSession) -> dict:
    """Tables, fields and their associations (keys)."""
    result = session.doc_call("GetTablesAndKeys", [
        {"qcx": 0, "qcy": 0}, {"qcx": 0, "qcy": 0}, 0, True, False,
    ])
    return {
        "tables": result.get("qtr", []),
        "keys": result.get("qk", []),
    }


def _get_data_model_with_retry(session: EngineSession, script: str) -> dict:
    data_model = _get_data_model(session)
    if data_model.get("tables"):
        return data_model

    if not re.search(r"\bLOAD\b", script, re.IGNORECASE):
        return data_model  # the script genuinely has no LOAD statements - 0 tables is correct

    for attempt in range(1, _DATA_MODEL_RETRY_ATTEMPTS + 1):
        print(
            f"[extract] WARNING: GetTablesAndKeys returned 0 tables after reload, "
            f"but the script clearly has LOAD statements - retrying "
            f"({attempt}/{_DATA_MODEL_RETRY_ATTEMPTS})..."
        )
        time.sleep(_DATA_MODEL_RETRY_DELAY_SECONDS)
        data_model = _get_data_model(session)
        if data_model.get("tables"):
            return data_model

    raise RuntimeError(
        "Qlik Engine API's GetTablesAndKeys kept returning 0 tables even "
        "after reloading the app and the app's own load script defines "
        f"tables, after {_DATA_MODEL_RETRY_ATTEMPTS} retries. The reload "
        "likely failed (check the WARNING printed right after 'reloading "
        "app in Qlik Cloud...' above) — most often because a lib:// data "
        "connection the script references isn't registered in this Qlik "
        "Cloud tenant. Fix/register that connection (or point the script "
        "at one that exists) and re-run extract."
    )


_HEX_COLOR_RE = re.compile(r"#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b")


def _get_theme(session: EngineSession, variables: list[dict], sheets: list[dict]) -> dict:
    """Qlik's own branding/palette, recovered two ways (neither requires
    pulling any business row data):

    1. The app's actually-configured Qlik theme NAME, via GetAppProperties
       — informational only (Qlik and Power BI theme systems are
       unrelated/non-transferable formats, so this can't be converted
       directly into a Power BI theme file), but worth recording so a
       person picking a matching Power BI theme by hand knows what to
       look for.
    2. A real, usable COLOR PALETTE, harvested from the two places a Qlik
       app's actual brand colors consistently show up: variables whose own
       definition is a literal hex color (this app's earlier `vTeal`,
       `vAmber`, `vRed`, `vGreen`, `vNavyBlue`, etc. — a script author
       naming a handful of variables after colors and defining them as hex
       is the standard Qlik idiom for "the app's palette", used throughout
       object color-expression overrides instead of Qlik's own generic
       theme system), and any hex code embedded LITERALLY in an object's
       own layout (a color picked directly in the object's Appearance
       panel, never promoted to a variable). Order is preserved
       (first-seen = most emphasized, typically a KPI/header accent) and
       duplicates removed — this becomes the build's actual Power BI
       `dataColors` palette (see project.py/report.py).

    Returns {"app_theme_name": str | None, "colors": [<hex>, ...]}."""
    app_theme_name = None
    try:
        props = session.doc_call("GetAppProperties", [])
        app_theme_name = (props.get("theme") or "").strip() or None
    except Exception as exc:
        print(f"[extract] WARNING: could not read app theme via GetAppProperties: {exc}")

    colors: list[str] = []
    seen: set[str] = set()

    def _add(hex_code: str) -> None:
        normalized = hex_code.upper()
        if normalized not in seen:
            seen.add(normalized)
            colors.append(normalized)

    for v in variables:
        definition = (v.get("definition") or "").strip().strip("'\"")
        if _HEX_COLOR_RE.fullmatch(definition):
            _add(definition)

    sheets_text = json.dumps(sheets)
    for m in _HEX_COLOR_RE.finditer(sheets_text):
        _add(m.group(0))

    return {"app_theme_name": app_theme_name, "colors": colors}


def _fetch_table_rows(session: EngineSession, table: dict) -> list[dict]:
    """Fetch one table's rows directly via the Engine, kept in MEMORY ONLY —
    never written to a CSV file or any other file on disk. This is used for
    exactly one purpose (see _detect_kpi_metadata_tables below): reading a
    small KPI-container config table's own rows (a title/measure-ref/color
    per KPI tile), which is object/sheet metadata recovery, not business-data
    extraction. It is NOT used for ordinary fact/dimension tables — no
    .qvf row/record data is extracted or persisted for those.

    Uses a straight (non-aggregated) hypercube with every field of the table
    as a dimension PLUS a synthetic '=RecNo()' dimension. RecNo() is the
    table's own internal record number, so it's guaranteed unique per row —
    without it, a straight hypercube behaves like a GROUP BY over every
    listed field and silently collapses any two rows that happen to share
    identical values across all fields.
    """
    table_name = table.get("qName") or table.get("name")
    fields = table.get("qFields", table.get("fields", []))
    field_names = [f.get("qName") or f.get("name") for f in fields if f.get("qName") or f.get("name")]
    if not table_name or not field_names:
        return []

    # Dates are read as their raw Qlik serial-day number (not formatted text)
    # so downstream code can reconstruct the calendar date deterministically
    # instead of guessing a locale for a formatted string like "01/02/2026".
    use_raw_number = [_field_is_numeric(f) or _field_is_date(f) for f in fields if f.get("qName") or f.get("name")]

    width = len(field_names) + 1  # +1 for the trailing RecNo() distinctness column
    height = max(HYPERCUBE_MIN_PAGE_HEIGHT, HYPERCUBE_MAX_CELLS_PER_PAGE // width)

    obj_def = {
        "qInfo": {"qType": "TableDataExport"},
        "qHyperCubeDef": {
            "qDimensions": (
                [{"qDef": {"qFieldDefs": [name]}} for name in field_names]
                + [{"qDef": {"qFieldDefs": ["=RecNo()"]}}]
            ),
            "qMeasures": [],
            "qInitialDataFetch": [{"qTop": 0, "qLeft": 0, "qHeight": height, "qWidth": width}],
            "qSuppressZero": False,
            "qSuppressMissing": False,
            "qMode": "S",
        },
    }
    try:
        handle, layout, height = _create_with_shrink(session, obj_def, height, width)
    except Exception as exc:
        print(f"[extract] WARNING: could not read rows for table '{table_name}': {exc}")
        return []

    hc = layout.get("qHyperCube", {})
    total_rows = hc.get("qSize", {}).get("qcy", 0)
    pages = hc.get("qDataPages", [])
    matrix = pages[0]["qMatrix"] if pages else []

    rows = list(matrix)
    top = len(rows)
    while len(rows) < total_rows and len(rows) < MAX_ROWS_PER_TABLE:
        page_rows, height = _fetch_page_with_shrink(session, handle, top, height, width)
        if not page_rows:
            break
        rows.extend(page_rows)
        top += len(page_rows)

    n_fields = len(field_names)
    # The `=RecNo()` distinctness dimension is only actually present in the
    # result on engines that accept it — Qlik Cloud silently DROPS an inline
    # `=RecNo()` dimension from a qMode "S" hypercube, so every row comes back
    # with exactly n_fields cells and no distinctness column. Blindly doing
    # `row[:-1]` in that case would chop off the real LAST field of every
    # row. Decide from the actual returned cell count, not from what was
    # requested.
    returned_width = len(rows[0]) if rows else hc.get("qSize", {}).get("qcx", width)
    has_distinctness_col = returned_width > n_fields

    out = []
    for row in rows:
        data_cells = list(row[:-1]) if has_distinctness_col else list(row)
        if len(data_cells) < n_fields:
            data_cells = data_cells + [{"qIsNull": True}] * (n_fields - len(data_cells))
        elif len(data_cells) > n_fields:
            data_cells = data_cells[:n_fields]
        out.append({name: _cell_value(cell, raw) for name, cell, raw in zip(field_names, data_cells, use_raw_number)})
    return out


def _is_page_too_large(exc: Exception) -> bool:
    return getattr(exc, "code", None) == _PAGE_TOO_LARGE_CODE


def _create_with_shrink(session: EngineSession, obj_def: dict, height: int, width: int):
    """Create the session object, halving qHeight on a 'Page(s) too large'
    error until it fits (Qlik Cloud's per-request cell cap varies by tenant
    and isn't discoverable up front)."""
    while True:
        obj_def["qHyperCubeDef"]["qInitialDataFetch"][0]["qHeight"] = height
        try:
            handle, layout = session.create_session_object(obj_def)
            return handle, layout, height
        except Exception as exc:
            if not _is_page_too_large(exc) or height <= HYPERCUBE_MIN_PAGE_HEIGHT:
                raise
            height = max(HYPERCUBE_MIN_PAGE_HEIGHT, height // 2)
            print(f"[extract] page too large, retrying with qHeight={height}")


def _fetch_page_with_shrink(session: EngineSession, handle: int, top: int, height: int, width: int):
    """Fetch one hypercube data page, halving qHeight on a 'Page(s) too
    large' error until it succeeds."""
    while True:
        page_def = [{"qTop": top, "qLeft": 0, "qHeight": height, "qWidth": width}]
        try:
            result = session.object_call(handle, "GetHyperCubeData", ["/qHyperCubeDef", page_def])
            return result.get("qDataPages", [{}])[0].get("qMatrix", []), height
        except Exception as exc:
            if not _is_page_too_large(exc) or height <= HYPERCUBE_MIN_PAGE_HEIGHT:
                raise
            height = max(HYPERCUBE_MIN_PAGE_HEIGHT, height // 2)
            print(f"[extract] page too large, retrying with qHeight={height}")


def _detect_kpi_metadata_tables(session: EngineSession, data_model: dict) -> list[dict]:
    """Detect a Qlik 'KPI container' driver table: a config table where each
    row describes one KPI tile (a title, a measure reference like
    '[Achievement %]', a background color, ...) rendered by a generic KPI
    container extension instead of native per-KPI chart objects. Since the
    container reads this table at runtime rather than exposing each KPI as
    its own object, the normal per-sheet-object extraction never sees these
    as separate KPIs — recovering them means reading the config table's own
    rows directly (via _fetch_table_rows, in memory only — never written to
    a CSV file) and matching them back to real measures during conversion.
    This is object/sheet metadata recovery (which KPI tiles exist), not
    business-data extraction.

    Heuristic: a table with at least one field whose name contains "title"
    and at least one whose name contains "measure" (case-insensitive) is
    almost certainly this pattern — a plain fact/dimension table wouldn't
    have both kinds of columns together.
    """
    detected = []
    for table in data_model.get("tables", []):
        table_name = table.get("qName") or table.get("name")
        fields = [f.get("qName") or f.get("name") for f in table.get("qFields", table.get("fields", []))
                  if f.get("qName") or f.get("name")]
        if not table_name or not fields:
            continue
        lower_fields = [f.lower() for f in fields]
        has_title = any("title" in f for f in lower_fields)
        has_measure = any("measure" in f for f in lower_fields)
        if not (has_title and has_measure):
            continue

        rows = _fetch_table_rows(session, table)
        detected.append({"table": table_name, "fields": fields, "rows": rows})
    return detected


def _field_is_numeric(field: dict) -> bool:
    tags = field.get("qTags", field.get("tags", []))
    return any(t in ("$numeric", "$integer") for t in tags) and not _field_is_date(field)


def _field_is_date(field: dict) -> bool:
    tags = field.get("qTags", field.get("tags", []))
    if "$date" not in tags and "$timestamp" not in tags:
        return False
    # Qlik idioms like MonthName()/WeekDayName() produce a dual value —
    # numerically a date, but the field is named/used for its TEXT label
    # ("Aug 2026"), not the underlying serial number. A field literally
    # named "...Name" tagged as a date is that pattern, not a real date
    # column — export its display text, not a date Power BI would need to
    # reconstruct (and that nobody actually wants to see as a raw date).
    name = (field.get("qName") or field.get("name") or "").lower()
    if "name" in name:
        return False
    return True


def _cell_value(cell: dict, raw_number: bool):
    if cell.get("qIsNull"):
        return ""
    if raw_number and "qNum" in cell and isinstance(cell["qNum"], (int, float)):
        return cell["qNum"]
    return cell.get("qText", "")




def _get_measures(session: EngineSession) -> list[dict]:
    # qMeasureListDef only returns qInfo/qMeta natively — the actual measure
    # definition (qDef, the DAX-bound expression) has to be pulled in through
    # a qData path pointing at "/qMeasure", landing under qData.measure in
    # each item. Without this every item comes back with expression=null.
    obj_def = {
        "qInfo": {"qType": "MeasureList"},
        "qMeasureListDef": {
            "qType": "measure",
            "qData": {"measure": "/qMeasure"},
        },
    }
    _, layout = session.create_session_object(obj_def)
    items = layout.get("qMeasureList", {}).get("qItems", [])
    measures = []
    for item in items:
        info = item.get("qInfo", {})
        meta = item.get("qMeta", {})
        measure_def = item.get("qData", {}).get("measure", {})
        measures.append({
            "id": info.get("qId"),
            "title": meta.get("title"),
            "description": meta.get("description"),
            "expression": measure_def.get("qDef"),
            "label_expression": measure_def.get("qLabelExpression"),
            "tags": meta.get("tags", []),
        })
    return measures


def _get_dimensions(session: EngineSession) -> list[dict]:
    # Same issue as measures: the dimension definition (qGrouping,
    # qFieldDefs) must be requested via a qData path ("/qDim") — it is not
    # returned as a native top-level qDim property on each item.
    obj_def = {
        "qInfo": {"qType": "DimensionList"},
        "qDimensionListDef": {
            "qType": "dimension",
            "qData": {"dim": "/qDim"},
        },
    }
    _, layout = session.create_session_object(obj_def)
    items = layout.get("qDimensionList", {}).get("qItems", [])
    dimensions = []
    for item in items:
        info = item.get("qInfo", {})
        meta = item.get("qMeta", {})
        dim = item.get("qData", {}).get("dim", {})
        dimensions.append({
            "id": info.get("qId"),
            "title": meta.get("title"),
            "description": meta.get("description"),
            "grouping": dim.get("qGrouping"),          # "N" single field, "H" drill-down hierarchy
            "field_defs": dim.get("qFieldDefs", []),
            "field_labels": dim.get("qFieldLabels", []),
            "tags": meta.get("tags", []),
        })
    return dimensions


def _get_variables(session: EngineSession) -> list[dict]:
    obj_def = {
        "qInfo": {"qType": "VariableList"},
        "qVariableListDef": {
            "qType": "variable",
            "qShowReserved": False,
            "qShowConfig": False,
            "qData": {"tags": "/tags"},
        },
    }
    _, layout = session.create_session_object(obj_def)
    items = layout.get("qVariableList", {}).get("qItems", [])
    variables = []
    for item in items:
        variables.append({
            "name": item.get("qName"),
            "definition": item.get("qDefinition"),
            "comment": item.get("qComment"),
            "is_script_created": item.get("qIsScriptCreated", False),
        })
    return variables


def _get_sheets(session: EngineSession) -> list[dict]:
    """Sheets with their child visualization objects, including layout (x/y/w/h)."""
    app_layout = session.doc_call("GetAppLayout", [])
    obj_def = {
        "qInfo": {"qType": "SheetList"},
        "qAppObjectListDef": {
            "qType": "sheet",
            "qData": {
                "title": "/qMetaDef/title",
                "description": "/qMetaDef/description",
                "cells": "/cells",
                "rank": "/rank",
            },
        },
    }
    _, layout = session.create_session_object(obj_def)
    sheet_items = layout.get("qAppObjectList", {}).get("qItems", [])

    sheets = []
    for sheet_item in sheet_items:
        sheet_id = sheet_item["qInfo"]["qId"]
        data = sheet_item.get("qData", {})
        objects = []
        for cell in data.get("cells", []):
            obj_id = cell.get("name")
            if not obj_id:
                continue
            bounds = {
                "x": cell.get("bounds", {}).get("x", cell.get("col", 0)),
                "y": cell.get("bounds", {}).get("y", cell.get("row", 0)),
                "width": cell.get("bounds", {}).get("width", cell.get("colspan", 1)),
                "height": cell.get("bounds", {}).get("height", cell.get("rowspan", 1)),
            }
            obj_info = _get_object_layout(session, obj_id)
            # A Qlik "container" of any flavour (sn-layout-container, the
            # classic tab `container`, sn-container, …) groups several real
            # visualization tiles — very often KPI cards — as children the
            # sheet's own `cells` array never lists. Without expanding it,
            # every one of those child KPIs is invisible in Power BI (the
            # container itself has no hypercube, so it converts to a blank
            # textbox). Detect it by type OR by the presence of a child
            # list in its resolved layout/properties, then recurse.
            if _is_container(cell.get("type"), obj_info):
                objects.extend(_expand_container(session, obj_id, bounds, obj_info, depth=0))
                continue
            # Per explicit instruction: extract every object as-is, with no
            # decorative/placeholder/nav-button filtering — the conversion
            # stage decides what to do with a decorative shape or a
            # hard-coded constant KPI, extraction itself no longer drops
            # anything. (_is_decorative_or_placeholder/_sheet_nav_button_reason
            # are kept, unused, in case per-object filtering is wanted again
            # later — e.g. surfaced as a user-facing option rather than a
            # silent default.)
            objects.append({
                "id": obj_id,
                "type": cell.get("type"),
                "bounds": bounds,
                "layout": obj_info,
            })
        sheets.append({
            "id": sheet_id,
            "title": data.get("title"),
            "description": data.get("description"),
            "rank": data.get("rank"),
            "objects": objects,
        })
    return sheets


# Matches a bare numeric literal ("1", "2.5") or a single-argument
# aggregation wrapped directly around one ("=Sum(2)", "Sum(-1.5)") — Qlik
# authors commonly write a KPI's "value" as `=Sum(2)` instead of a bare `2`
# even though it's still just a hard-coded constant, not a real aggregation
# over any field.
_CONSTANT_EXPR_RE = re.compile(
    r"^=?\s*(?:sum|count|avg|min|max)?\s*\(?\s*-?\d+(\.\d+)?\s*\)?$", re.IGNORECASE
)


def _extract_text_leaves(node) -> list[str]:
    """Walk a Qlik sn-text object's Lexical-editor JSON tree (props["text"],
    already a parsed dict here) and collect every literal "text" leaf."""
    out: list[str] = []
    if isinstance(node, dict):
        val = node.get("text")
        if isinstance(val, str):
            out.append(val)
        for v in node.values():
            out.extend(_extract_text_leaves(v))
    elif isinstance(node, list):
        for v in node:
            out.extend(_extract_text_leaves(v))
    return out


def _is_decorative_or_placeholder(obj_type: str | None, obj_info: dict) -> str | None:
    """Detect Qlik objects that carry no real business content and would
    otherwise convert into visual clutter — pure divider/line shapes, a
    completely blank text object, or a "KPI" whose every measure is a bare
    numeric literal (e.g. `=1`, `Sum(2)`) rather than a real aggregation
    over actual data. Returns a one-line reason to log, or None if the
    object should be kept.

    Confirmed against real extracted objects (not guessed): a Qlik `Line`
    extension object (qType `sn-shape`) never has a qHyperCubeDef at all —
    it is 100% decorative, so it's always skipped. A `kpi` object whose
    qMeasures all resolve to a plain number literal is a step/ordinal badge
    or a placeholder, not a real KPI — the pipeline's own KPI-container and
    ad-hoc-measure handling already treats hard-coded-constant expressions
    as needing a placeholder measure; when EVERY measure on a whole object
    is like that, the object itself isn't reportable content."""
    if not isinstance(obj_info, dict):
        return None
    props = obj_info.get("properties") or {}
    if not isinstance(props, dict):
        return None

    if obj_type == "sn-shape":
        return "purely decorative divider/line shape (Qlik 'Line' extension), no data"

    if obj_type == "sn-text":
        raw_text = props.get("text")
        if isinstance(raw_text, str):
            try:
                parsed = json.loads(raw_text)
            except (ValueError, TypeError):
                parsed = None
            leaves = _extract_text_leaves(parsed) if parsed is not None else []
            if not any(leaf.strip() for leaf in leaves):
                return "blank text object (no visible text in any paragraph)"

    hc = props.get("qHyperCubeDef")
    if obj_type == "kpi" and isinstance(hc, dict):
        measures = hc.get("qMeasures") or []
        defs = [((m or {}).get("qDef") or {}).get("qDef") for m in measures]
        defs = [d for d in defs if isinstance(d, str)]
        if defs and all(_CONSTANT_EXPR_RE.match(d.strip()) for d in defs):
            return f"KPI's measure(s) are hard-coded constant(s) ({', '.join(defs)}), not a real aggregation"

    return None


def _sheet_nav_button_reason(obj_type: str | None, obj_info: dict) -> str | None:
    """A container-child `action-button` whose action is `goToSheet` is a
    Qlik sheet-tab button — confirmed via its real `navigation` property
    (`{"action": "goToSheet", "sheet": "<id>", ...}`). Skip it rather than
    reproducing it as a PBIR button, for two compounding reasons: (1) Power
    BI's own report already has a native page-tab strip providing exactly
    this navigation, with zero extra visual and zero overlap risk; (2) this
    button's only available position here is _expand_container's synthetic
    grid cell (the Engine API gives no real per-child position), which does
    not match Qlik's actual irregular tab-strip layout and is exactly what
    was overlapping real page content. A button whose action is something
    OTHER than goToSheet (open a URL, trigger a variable, etc.) is kept —
    those have no native Power BI equivalent to fall back on."""
    if obj_type != "action-button" or not isinstance(obj_info, dict):
        return None
    props = obj_info.get("properties") or {}
    nav = props.get("navigation") if isinstance(props, dict) else None
    if isinstance(nav, dict) and nav.get("action") == "goToSheet":
        return "sheet-navigation button — Power BI's own page tabs already provide this, with a real (non-overlapping) position"
    return None


def _get_object_layout(session: EngineSession, obj_id: str) -> dict:
    try:
        result = session.doc_call("GetObject", [obj_id])
        handle = result["qReturn"]["qHandle"]
        props = session.object_call(handle, "GetEffectiveProperties", [])
        layout = session.object_call(handle, "GetLayout", [])
        return {
            "properties": props.get("qProp", {}),
            "layout": layout.get("qLayout", {}),
        }
    except Exception as exc:  # object may be embedded/master-linked differently across versions
        return {"error": str(exc)}


_CONTAINER_TYPE_HINTS = ("container",)  # substring match, case-insensitive


def _is_container(cell_type: str | None, obj_info: dict) -> bool:
    """A cell is a container if its type name says so, or if its resolved
    layout/properties expose a child list (`qChildList` / `qChildListDef`)
    — some container extensions don't use an obvious type name."""
    if isinstance(cell_type, str) and any(h in cell_type.lower() for h in _CONTAINER_TYPE_HINTS):
        return True
    if not isinstance(obj_info, dict):
        return False
    layout = obj_info.get("layout") or {}
    props = obj_info.get("properties") or {}
    if isinstance(layout, dict) and (layout.get("qChildList") or {}).get("qItems"):
        return True
    if isinstance(props, dict) and props.get("qChildListDef"):
        return True
    return False


def _container_child_ids(session: EngineSession, container_id: str, obj_info: dict) -> list[dict]:
    """Child {id, type} list for a container — prefer GetChildInfos, fall
    back to the qChildList already present in the container's own layout."""
    try:
        result = session.doc_call("GetObject", [container_id])
        handle = result["qReturn"]["qHandle"]
        infos = session.object_call(handle, "GetChildInfos", []).get("qInfos", [])
        if infos:
            return [{"id": c.get("qId"), "type": c.get("qType")} for c in infos if c.get("qId")]
    except Exception as exc:
        print(f"[extract] WARNING: GetChildInfos failed for container '{container_id}': {exc}")
    items = ((obj_info.get("layout") or {}).get("qChildList") or {}).get("qItems") or []
    out = []
    for it in items:
        cid = (it.get("qInfo") or {}).get("qId")
        ctype = (it.get("qInfo") or {}).get("qType") or (it.get("qData") or {}).get("visualization")
        if cid:
            out.append({"id": cid, "type": ctype})
    return out


def _expand_container(session: EngineSession, container_id: str, container_bounds: dict,
                      obj_info: dict, depth: int = 0) -> list[dict]:
    """Recurse into a Qlik container object and return its real child tiles
    (each with its own type + qHyperCubeDef) as ordinary sheet objects,
    auto-laid-out in a grid within the container's own bounds (Engine API
    gives child id/type only, not per-child position). Recurses through
    nested containers, up to a sane depth limit."""
    if depth > 4:
        print(f"[extract] WARNING: container nesting past depth 4 at '{container_id}' — not recursing further")
        return []

    children = _container_child_ids(session, container_id, obj_info)
    if not children:
        return []

    n = len(children)
    cols = min(n, 4) or 1
    rows = -(-n // cols)  # ceil division
    cell_w = container_bounds.get("width", cols) / cols
    cell_h = container_bounds.get("height", rows) / rows
    base_x = container_bounds.get("x", 0)
    base_y = container_bounds.get("y", 0)

    objects: list[dict] = []
    for i, child in enumerate(children):
        child_id = child["id"]
        col, row = i % cols, i // cols
        child_bounds = {
            "x": base_x + col * cell_w, "y": base_y + row * cell_h,
            "width": cell_w, "height": cell_h,
        }
        child_info = _get_object_layout(session, child_id)
        if _is_container(child.get("type"), child_info):
            objects.extend(_expand_container(session, child_id, child_bounds, child_info, depth + 1))
            continue
        # Per explicit instruction: every container child is extracted,
        # including decorative shapes, hard-coded constant KPI badges, and
        # sheet-navigation buttons — no filtering at extraction time.
        objects.append({
            "id": child_id,
            "type": child.get("type"),
            "bounds": child_bounds,
            "layout": child_info,
            # Which Qlik container this tile came from — not every
            # container's children happen to have that id embedded in
            # their own name (only the "qlik-compound-context-<id>-link-"
            # pattern does), so this is recorded explicitly rather than
            # left for a build-time regex to (sometimes wrongly) infer.
            # Used by the builder to merge several sibling KPI tiles from
            # the SAME container back into one grouped visual instead of
            # leaving them as scattered, independent cards.
            "container_id": container_id,
        })
    return objects


# ── io helpers ───────────────────────────────────────────────────────────────

def _write_json(out_dir: str, filename: str, data: Any) -> None:
    with open(os.path.join(out_dir, filename), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _write_text(out_dir: str, filename: str, text: str) -> None:
    with open(os.path.join(out_dir, filename), "w", encoding="utf-8") as f:
        f.write(text)
