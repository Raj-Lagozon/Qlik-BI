"""Detect Qlik 'variable input' slider objects (qlik-variable-input, a
common Qlik Sense extension for what-if simulation) and turn each into a
real Power BI what-if parameter: a small table holding the numeric range
plus a SELECTEDVALUE() measure named after the slider's own label, so any
existing measure that already referenced that label as a bracketed measure
name (Qlik lets any expression reference $(vVar) by variable substitution,
which the report/measure conversion renders as a bracket reference to the
slider's display label) resolves automatically with no rewriting needed."""

from __future__ import annotations

import json
import os


def detect_what_if_parameters(extracted_dir: str) -> list[dict]:
    sheets_path = os.path.join(extracted_dir, "sheets.json")
    if not os.path.exists(sheets_path):
        return []
    with open(sheets_path, encoding="utf-8") as f:
        sheets = json.load(f)

    variables_path = os.path.join(extracted_dir, "variables.json")
    var_defaults: dict[str, str] = {}
    if os.path.exists(variables_path):
        with open(variables_path, encoding="utf-8") as f:
            for v in json.load(f):
                if v.get("name"):
                    var_defaults[v["name"]] = v.get("definition")

    found: dict[str, dict] = {}

    def walk(node):
        if isinstance(node, dict):
            is_slider = (
                node.get("visualization") == "qlik-variable-input"
                or node.get("qInfo", {}).get("qType") == "qlik-variable-input"
            )
            if is_slider:
                var_name = node.get("variableName")
                if var_name and var_name not in found:
                    label = (node.get("subtitle") or var_name).strip()
                    min_v = node.get("min", 0)
                    max_v = node.get("max", 100)
                    step = node.get("step", 1) or 1
                    default = _to_number(var_defaults.get(var_name), fallback=min_v)
                    found[var_name] = {
                        "variable": var_name, "label": label,
                        "min": min_v, "max": max_v, "step": step, "default": default,
                    }
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(sheets)
    return list(found.values())


def _to_number(raw, fallback):
    if raw is None:
        return fallback
    try:
        return float(str(raw).strip().strip("'\""))
    except ValueError:
        return fallback


def generate_range_m(min_v, max_v, step) -> str:
    count = int((max_v - min_v) / step) + 1
    return (
        "let\n"
        f"    Source = List.Numbers({min_v}, {count}, {step}),\n"
        "    #\"Converted to Table\" = Table.FromList(Source, Splitter.SplitByNothing(), {\"Value\"}, null, ExtraValues.Error),\n"
        "    #\"Changed Type\" = Table.TransformColumnTypes(#\"Converted to Table\", {{\"Value\", type number}})\n"
        "in\n"
        "    #\"Changed Type\""
    )
