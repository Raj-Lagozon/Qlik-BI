"""Runs each extracted artifact through its matching skill.md via Azure OpenAI
and writes the converted (M / DAX / TMDL-fragment / PBIR JSON / RLS) output
under converted/<app_name>/."""

from __future__ import annotations

import json
import os
import re

from .azure_client import run_skill

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILLS_DIR = os.path.join(ROOT, "skills")
EXTRACTED_ROOT = os.path.join(ROOT, "extracted")
CONVERTED_ROOT = os.path.join(ROOT, "converted")


def _load_skill(filename: str) -> str:
    with open(os.path.join(SKILLS_DIR, filename), encoding="utf-8") as f:
        return f.read()


def _load_extracted(app_name: str, filename: str):
    path = os.path.join(EXTRACTED_ROOT, app_name, filename)
    if filename.endswith(".json"):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    with open(path, encoding="utf-8") as f:
        return f.read()


def _write_converted(app_name: str, filename: str, data) -> str:
    out_dir = os.path.join(CONVERTED_ROOT, app_name)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, filename)
    with open(path, "w", encoding="utf-8") as f:
        if filename.endswith(".json"):
            json.dump(data, f, indent=2, ensure_ascii=False)
        else:
            f.write(data)
    return path


# Every skill's output items carry a "confidence": "high"|"medium"|"low"
# field (see each skill.md's Output section) — the LLM's own judgment of how
# certain it is about that one translation, separate from and in addition to
# the code-level checks in pbip_build (hallucinated entity names, unresolved
# measures, etc). Collected here across every conversion call so convert_app
# can print one batch summary — a low-confidence item might still build and
# render fine, so this is a review flag, not a build blocker.
_confidence_log: list[dict] = []


def _collect_confidence(source: str, items: list[dict]) -> None:
    for item in items:
        confidence = item.get("confidence")
        if confidence in ("medium", "low"):
            _confidence_log.append({
                "source": source,
                "name": item.get("name") or item.get("title") or "<unnamed>",
                "confidence": confidence,
                "notes": item.get("notes") or item.get("description") or "",
            })


def convert_app(app_name: str) -> dict:
    """Run every domain converter for one extracted app. Returns the paths of
    everything written under converted/<app_name>/."""
    _confidence_log.clear()
    written = {}

    written["m_queries"] = _convert_m_queries(app_name)
    written["data_model"] = _convert_data_model(app_name)
    written["measures"] = _convert_measures(app_name)
    written["dimensions"] = _convert_dimensions(app_name)
    written["parameters"] = _convert_parameters(app_name)
    written["report"] = _convert_report(app_name)
    written["rls"] = _convert_rls(app_name)
    written["kpi_containers"] = _convert_kpi_containers(app_name)
    written["adhoc_expressions"] = _convert_adhoc_expressions(app_name)

    _print_confidence_summary()
    return written


def _print_confidence_summary() -> None:
    if not _confidence_log:
        print("[convert] all converted items reported high confidence")
        return
    low = [x for x in _confidence_log if x["confidence"] == "low"]
    medium = [x for x in _confidence_log if x["confidence"] == "medium"]
    print(f"[convert] confidence flags: {len(medium)} medium, {len(low)} low — review before trusting the build")
    for item in low + medium:
        note = f" — {item['notes']}" if item["notes"] else ""
        print(f"[convert]   [{item['confidence'].upper()}] {item['source']}: '{item['name']}'{note}")


def _convert_m_queries(app_name: str) -> list[str]:
    skill = _load_skill("m_query.skill.md")
    script = _load_extracted(app_name, "script.qvs")
    data_model = _load_extracted(app_name, "data_model.json")

    paths = []
    for table in data_model.get("tables", []):
        table_name = table.get("qName") or table.get("name")
        if not table_name:
            continue
        payload = {"table_name": table_name, "script": script, "data_model_table": table}
        m_code = run_skill(skill, payload, json_output=False)
        m_code = _strip_code_fence(m_code)
        m_code = _extract_m_confidence_comment(table_name, m_code)
        paths.append(_write_converted(app_name, f"m_query__{_safe(table_name)}.m", m_code))
    return paths


_M_CONFIDENCE_RE = re.compile(r"^\s*//\s*CONFIDENCE:\s*(low|medium)\s*-\s*(.+?)\s*\n", re.IGNORECASE)


def _extract_m_confidence_comment(table_name: str, m_code: str) -> str:
    """m_query.skill.md's output is raw M text (no JSON wrapper — the
    contract is 'just the M code, ready to paste'), so it can't carry a
    structured confidence field the way every other skill's JSON output
    does. Instead the skill is asked to prepend a `// CONFIDENCE: low -
    reason` comment line only when uncertain; parse that out here (feeding
    it into the same batch summary as everything else) and strip it before
    writing the .m file, since a leading comment there would otherwise
    become part of the TMDL partition source."""
    match = _M_CONFIDENCE_RE.match(m_code)
    if not match:
        return m_code
    confidence, reason = match.group(1).lower(), match.group(2)
    _collect_confidence("m_query", [{"name": table_name, "confidence": confidence, "notes": reason}])
    return m_code[match.end():]


def _convert_data_model(app_name: str) -> str:
    skill = _load_skill("data_model.skill.md")
    data_model = _load_extracted(app_name, "data_model.json")
    result = run_skill(skill, data_model, json_output=True)
    _collect_confidence("data_model", result.get("relationships", []))
    return _write_converted(app_name, "data_model.converted.json", result)


def _convert_measures(app_name: str) -> str | None:
    measures = _load_extracted(app_name, "measures.json")
    if not measures:
        print("[convert] no master measures in this app — skipping dax_measures LLM call")
        return None
    skill = _load_skill("dax_measures.skill.md")
    data_model = _load_extracted(app_name, "data_model.json")
    result = run_skill(skill, {"measures": measures, "data_model": data_model}, json_output=True)
    _collect_confidence("dax_measures", result.get("measures", []))
    return _write_converted(app_name, "measures.converted.json", result)


def _convert_dimensions(app_name: str) -> str | None:
    dimensions = _load_extracted(app_name, "dimensions.json")
    if not dimensions:
        print("[convert] no master dimensions in this app — skipping dax_columns_hierarchies LLM call")
        return None
    skill = _load_skill("dax_columns_hierarchies.skill.md")
    data_model = _load_extracted(app_name, "data_model.json")
    result = run_skill(skill, {"dimensions": dimensions, "data_model": data_model}, json_output=True)
    _collect_confidence("dax_columns_hierarchies", result.get("items", []))
    return _write_converted(app_name, "dimensions.converted.json", result)


def _convert_parameters(app_name: str) -> str | None:
    variables = _load_extracted(app_name, "variables.json")
    if not variables:
        print("[convert] no variables in this app — skipping parameters_variables LLM call")
        return None
    skill = _load_skill("parameters_variables.skill.md")
    script = _load_extracted(app_name, "script.qvs")
    result = run_skill(skill, {"variables": variables, "script": script}, json_output=True)
    _collect_confidence("parameters_variables", result.get("variables", []))
    return _write_converted(app_name, "variables.converted.json", result)


def _convert_report(app_name: str) -> list[str]:
    skill = _load_skill("report_visuals.skill.md")
    sheets = _load_extracted(app_name, "sheets.json")
    measures = _load_extracted(app_name, "measures.json")
    dimensions = _load_extracted(app_name, "dimensions.json")

    paths = []
    for sheet in sheets:
        payload = {"sheet": sheet, "measures": measures, "dimensions": dimensions}
        result = run_skill(skill, payload, json_output=True)
        _collect_confidence("report_visuals", result.get("visuals", []))
        sheet_id = sheet.get("id") or _safe(sheet.get("title", "sheet"))
        paths.append(_write_converted(app_name, f"page__{_safe(sheet_id)}.json", result))
    return paths


def _convert_rls(app_name: str) -> str | None:
    section_access = _load_extracted(app_name, "section_access.json")
    if not section_access.get("present"):
        print("[convert] no Section Access in source script — skipping rls_section_access LLM call")
        return _write_converted(app_name, "rls.converted.json",
                                 {"roles": [], "notes": ["No Section Access in source script — no RLS to migrate."]})
    skill = _load_skill("rls_section_access.skill.md")
    data_model = _load_extracted(app_name, "data_model.json")
    result = run_skill(skill, {"section_access": section_access, "data_model": data_model}, json_output=True)
    _collect_confidence("rls_section_access", result.get("roles", []))
    return _write_converted(app_name, "rls.converted.json", result)


def _convert_kpi_containers(app_name: str) -> list[str]:
    kpi_path = os.path.join(EXTRACTED_ROOT, app_name, "kpi_containers.json")
    if not os.path.exists(kpi_path):
        return []
    with open(kpi_path, encoding="utf-8") as f:
        containers = json.load(f)
    if not containers:
        return []

    skill = _load_skill("kpi_container.skill.md")
    measures = _load_extracted(app_name, "measures.json")
    variables = _load_extracted(app_name, "variables.json")

    paths = []
    for container in containers:
        payload = {
            "table": container["table"],
            "fields": container["fields"],
            "rows": container["rows"],
            "measures": measures,
            "variables": variables,
        }
        result = run_skill(skill, payload, json_output=True)
        _collect_confidence("kpi_container", result.get("kpis", []))
        paths.append(_write_converted(app_name, f"kpi_container__{_safe(container['table'])}.json", result))
    return paths


def _collect_adhoc_expressions(app_name: str) -> tuple[dict[str, str], dict[str, str]]:
    """Find measure/dimension expressions used directly inside a chart's own
    qHyperCubeDef that aren't backed by any real master measure/dimension —
    a chart author can type a Sum(...)/If(...) expression straight into an
    object instead of picking a library item. The report_visuals conversion
    only sees that object's qLabel/qFieldLabels text (e.g. "MeasureValue"),
    which isn't a real field or measure name, so binding to it directly
    fails ("fields that need to be fixed") — these need their own DAX
    conversion first, the same way an actual master measure/dimension does."""
    sheets = _load_extracted(app_name, "sheets.json")
    measures = _load_extracted(app_name, "measures.json")
    dimensions = _load_extracted(app_name, "dimensions.json")
    known_measure_titles = {m["title"].casefold() for m in measures if m.get("title")}
    known_dim_titles = {d["title"].casefold() for d in dimensions if d.get("title")}

    adhoc_measures: dict[str, str] = {}
    adhoc_dims: dict[str, str] = {}

    def walk(node):
        if isinstance(node, dict):
            hc = node.get("qHyperCubeDef")
            if isinstance(hc, dict):
                for m in hc.get("qMeasures") or []:
                    qdef = (m or {}).get("qDef") or {}
                    label, expr = qdef.get("qLabel"), qdef.get("qDef")
                    if label and expr and label.casefold() not in known_measure_titles:
                        adhoc_measures.setdefault(label, expr)
                for d in hc.get("qDimensions") or []:
                    qdef = (d or {}).get("qDef") or {}
                    field_defs = qdef.get("qFieldDefs") or []
                    labels = qdef.get("qFieldLabels") or []
                    if field_defs and str(field_defs[0]).startswith("="):
                        label = labels[0] if labels else None
                        if label and label.casefold() not in known_dim_titles:
                            adhoc_dims.setdefault(label, field_defs[0])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(sheets)
    return adhoc_measures, adhoc_dims


def _convert_adhoc_expressions(app_name: str) -> str | None:
    adhoc_measures, adhoc_dims = _collect_adhoc_expressions(app_name)
    if not adhoc_measures and not adhoc_dims:
        return None

    data_model = _load_extracted(app_name, "data_model.json")
    result: dict = {"measures": [], "items": []}

    if adhoc_measures:
        skill = _load_skill("dax_measures.skill.md")
        payload = [
            {"title": label, "expression": expr, "label_expression": None, "tags": []}
            for label, expr in adhoc_measures.items()
        ]
        r = run_skill(skill, {"measures": payload, "data_model": data_model}, json_output=True)
        result["measures"] = r.get("measures", [])
        _collect_confidence("adhoc_expressions (measure)", result["measures"])

    if adhoc_dims:
        skill = _load_skill("dax_columns_hierarchies.skill.md")
        payload = [
            {"title": label, "grouping": "N", "field_defs": [expr], "field_labels": [label]}
            for label, expr in adhoc_dims.items()
        ]
        r = run_skill(skill, {"dimensions": payload, "data_model": data_model}, json_output=True)
        result["items"] = r.get("items", [])
        _collect_confidence("adhoc_expressions (dimension)", result["items"])

    return _write_converted(app_name, "adhoc_expressions.converted.json", result)


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    return text.strip()
