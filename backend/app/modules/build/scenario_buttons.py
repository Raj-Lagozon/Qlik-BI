"""Detect Qlik action-buttons whose action is `setVariable` (a "scenario
picker" pattern: several buttons on one sheet, each setting the SAME
variable to a DIFFERENT fixed literal value — e.g. "Standard"/"Enhanced"/
"Aggressive" buttons all setting `vDunning`) and turn each such variable
into a real, interactive Power BI equivalent: a small disconnected table
holding the distinct values plus a `SELECTEDVALUE()` measure, driven by a
Slicer instead of buttons (Power BI has no button action that sets an
arbitrary DAX value, so a slicer is the only way this stays genuinely
interactive rather than a cosmetic, do-nothing button).

Fully deterministic — Qlik's own extracted action data
(`actionType`/`variable`/`value`) is a clean, structured field on the
button's own properties, not something that needs an LLM guess."""

from __future__ import annotations

import json
import os


def detect_variable_scenarios(extracted_dir: str) -> list[dict]:
    """Returns one entry per distinct variable driven by `setVariable`
    buttons: `{"variable": str, "values": [str, ...] (first-seen order,
    de-duplicated), "button_ids": [str, ...], "page_id": str}`.
    `button_ids` lets the caller find and replace those exact visuals;
    `values` in first-seen order means the FIRST button's value is a
    natural, safe default (matches whatever the source app's own default
    button/state was)."""
    sheets_path = os.path.join(extracted_dir, "sheets.json")
    if not os.path.exists(sheets_path):
        return []
    with open(sheets_path, encoding="utf-8") as f:
        sheets = json.load(f)

    by_variable: dict[str, dict] = {}
    for sheet in sheets:
        sheet_id = sheet.get("id")
        for obj in sheet.get("objects", []):
            if obj.get("type") != "action-button":
                continue
            props = (obj.get("layout") or {}).get("properties") or {}
            for action in props.get("actions") or []:
                if action.get("actionType") != "setVariable":
                    continue
                var_name = action.get("variable")
                value = action.get("value")
                if not var_name or value is None:
                    continue
                entry = by_variable.setdefault(var_name, {
                    "variable": var_name, "values": [], "button_ids": [], "page_id": sheet_id,
                })
                if value not in entry["values"]:
                    entry["values"].append(value)
                if obj.get("id") not in entry["button_ids"]:
                    entry["button_ids"].append(obj.get("id"))

    # A variable driven by only ONE distinct value across every button
    # isn't really a "scenario picker" (nothing to pick between) — leave
    # it alone rather than building a one-row slicer that can't do anything
    # a plain constant measure doesn't already do.
    return [e for e in by_variable.values() if len(e["values"]) > 1]


def _is_numeric(values: list[str]) -> bool:
    try:
        for v in values:
            float(str(v).strip())
        return True
    except (TypeError, ValueError):
        return False


def generate_scenario_table_m(values: list[str]) -> tuple[str, str]:
    """Returns (m_expression, column_data_type) for a disconnected table
    holding these distinct scenario values, one row each. Numeric-typed
    when every value parses as a number (e.g. "0"/"1"/"2"), text otherwise
    (e.g. "Standard"/"Enhanced"/"Aggressive") — `SELECTEDVALUE()` then
    naturally returns the same type the rest of the app's DAX expects."""
    if _is_numeric(values):
        rows = ", ".join(f"{{{float(v):g}}}" for v in values)
        m = f"let\n    Source = #table(type table [Value = number], {{{rows}}})\nin\n    Source"
        return m, "double"
    escaped = [str(v).replace('"', '""') for v in values]
    rows = ", ".join(f'{{"{v}"}}' for v in escaped)
    m = f"let\n    Source = #table(type table [Value = text], {{{rows}}})\nin\n    Source"
    return m, "string"
