"""Pull script, data model, master measures/dimensions, variables, sheets and
section access out of a Qlik Cloud app via the Engine API, saving each as a
JSON/text file under extracted/<app_name>/."""

from __future__ import annotations

import csv
import json
import os
from typing import Any

from .client import QlikCloudClient, EngineSession
from .section_access import parse_section_access

HYPERCUBE_MAX_CELLS_PER_PAGE = 8_000   # Qlik Cloud tenants cap qHeight*qWidth per fetch well below on-prem defaults
HYPERCUBE_MIN_PAGE_HEIGHT = 1
MAX_ROWS_PER_TABLE = 500_000  # safety cap so a runaway table can't hang extraction

_PAGE_TOO_LARGE_CODE = 6001

EXTRACTED_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "extracted")


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

            script = _get_script(session)
            _write_text(out_dir, "script.qvs", script)

            data_model = _get_data_model(session)
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

            data_dir = os.path.join(out_dir, "data")
            os.makedirs(data_dir, exist_ok=True)
            for table in data_model.get("tables", []):
                _export_table_data(session, table, data_dir)

            kpi_containers = _detect_kpi_metadata_tables(data_model, data_dir)
            _write_json(out_dir, "kpi_containers.json", kpi_containers)
            if kpi_containers:
                names = ", ".join(k["table"] for k in kpi_containers)
                print(f"[extract] detected KPI container config table(s): {names}")
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


def _get_data_model(session: EngineSession) -> dict:
    """Tables, fields and their associations (keys)."""
    result = session.doc_call("GetTablesAndKeys", [
        {"qcx": 0, "qcy": 0}, {"qcx": 0, "qcy": 0}, 0, True, False,
    ])
    return {
        "tables": result.get("qtr", []),
        "keys": result.get("qk", []),
    }


def _export_table_data(session: EngineSession, table: dict, data_dir: str) -> None:
    """Export one table's actual rows (not just schema) so the compiled .pbix
    loads real data instead of pointing at a source file/DB only the Qlik
    author's machine could reach.

    Uses a straight (non-aggregated) hypercube with every field of the table
    as a dimension PLUS a synthetic '=RecNo()' dimension. RecNo() is the
    table's own internal record number, so it's guaranteed unique per row —
    without it, a straight hypercube behaves like a GROUP BY over every
    listed field and silently collapses any two rows that happen to share
    identical values across all fields (very possible on a table built from
    Table.Combine()-ing several sparse/mostly-null sub-tables, or on
    low-cardinality sample data), quietly under-counting SUM()s downstream.
    """
    table_name = table.get("qName") or table.get("name")
    fields = table.get("qFields", table.get("fields", []))
    field_names = [f.get("qName") or f.get("name") for f in fields if f.get("qName") or f.get("name")]
    if not table_name or not field_names:
        return

    # Dates are exported as their raw Qlik serial-day number (not formatted
    # text) so M can reconstruct the calendar date deterministically instead
    # of guessing a locale for a formatted string like "01/02/2026".
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
        print(f"[extract] WARNING: could not export data for table '{table_name}': {exc}")
        return

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

    csv_path = os.path.join(data_dir, f"{_safe_filename(table_name)}.csv")
    short_rows = 0
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(field_names)
        for row in rows:
            data_cells = row[:-1]  # drop the trailing RecNo() distinctness column
            if len(data_cells) != len(field_names):
                # Qlik occasionally returns fewer cells than requested for a
                # row (seen with dual-valued derived fields like
                # MonthName()). Silently zipping a short row against
                # use_raw_number/field_names would shift every later column
                # by one — pad with nulls instead so column alignment stays
                # correct (only the missing field is blank for that row).
                short_rows += 1
                data_cells = data_cells + [{"qIsNull": True}] * (len(field_names) - len(data_cells))
            writer.writerow([_cell_value(cell, raw) for cell, raw in zip(data_cells, use_raw_number)])

    if short_rows:
        print(f"[extract] WARNING: {table_name}: {short_rows} row(s) returned fewer cells than "
              f"requested by the Engine API — padded with nulls to keep columns aligned; "
              f"check for a dual-valued/derived field (e.g. MonthName()-style) losing data")
    print(f"[extract] {table_name}: exported {len(rows)} row(s) -> data/{os.path.basename(csv_path)}")


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


def _detect_kpi_metadata_tables(data_model: dict, data_dir: str) -> list[dict]:
    """Detect a Qlik 'KPI container' driver table: a config table where each
    row describes one KPI tile (a title, a measure reference like
    '[Achievement %]', a background color, ...) rendered by a generic KPI
    container extension instead of native per-KPI chart objects. Since the
    container reads this table at runtime rather than exposing each KPI as
    its own object, the normal per-sheet-object extraction never sees these
    as separate KPIs — recovering them means reading the config table's own
    rows directly (already exported to CSV) and matching them back to real
    measures during conversion.

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

        csv_path = os.path.join(data_dir, f"{_safe_filename(table_name)}.csv")
        if not os.path.exists(csv_path):
            continue
        with open(csv_path, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))

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


def _safe_filename(name: str) -> str:
    return "".join(c if c.isalnum() or c in "_-." else "_" for c in name)


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
            if cell.get("type") == "sn-layout-container":
                # A native Qlik "container" object groups several real
                # visualization tiles (very often KPI cards) as children —
                # the container itself carries no chart data of its own, so
                # without expanding into its children they'd be invisible
                # (converted as a blank textbox instead of real KPIs).
                objects.extend(_expand_container(session, obj_id, bounds))
                continue
            obj_info = _get_object_layout(session, obj_id)
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


def _expand_container(session: EngineSession, container_id: str, container_bounds: dict) -> list[dict]:
    """Recurse into a native Qlik container object and return its real
    child tiles (each with its own type + qHyperCubeDef) as ordinary sheet
    objects, auto-laid-out in a grid within the container's own bounds
    (Engine API's GetChildInfos returns child id/type only, not position —
    there's no reliably documented per-child grid position to read instead)."""
    try:
        result = session.doc_call("GetObject", [container_id])
        handle = result["qReturn"]["qHandle"]
        children = session.object_call(handle, "GetChildInfos", []).get("qInfos", [])
    except Exception as exc:
        print(f"[extract] WARNING: could not expand container '{container_id}': {exc}")
        return []

    if not children:
        return []

    n = len(children)
    cols = min(n, 4) or 1
    rows = -(-n // cols)  # ceil division
    cell_w = container_bounds.get("width", cols) / cols
    cell_h = container_bounds.get("height", rows) / rows
    base_x = container_bounds.get("x", 0)
    base_y = container_bounds.get("y", 0)

    objects = []
    for i, child in enumerate(children):
        child_id = child.get("qId")
        if not child_id:
            continue
        col, row = i % cols, i // cols
        objects.append({
            "id": child_id,
            "type": child.get("qType"),
            "bounds": {
                "x": base_x + col * cell_w,
                "y": base_y + row * cell_h,
                "width": cell_w,
                "height": cell_h,
            },
            "layout": _get_object_layout(session, child_id),
        })
    return objects


# ── io helpers ───────────────────────────────────────────────────────────────

def _write_json(out_dir: str, filename: str, data: Any) -> None:
    with open(os.path.join(out_dir, filename), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _write_text(out_dir: str, filename: str, text: str) -> None:
    with open(os.path.join(out_dir, filename), "w", encoding="utf-8") as f:
        f.write(text)
