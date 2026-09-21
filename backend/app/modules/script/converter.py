"""Converts the extracted Qlik load SCRIPT (script.qvs) into Power BI
artifacts: one Power Query M partition per table, and the script's own
vVariables into M parameters / DAX measures. Both tasks are driven by the
single consolidated `script_conversion.skill.md`."""

from __future__ import annotations

import re

from app.utilities.llm import (
    collect_confidence,
    load_extracted,
    load_skill,
    run_skill,
    safe,
    strip_code_fence,
    write_converted,
)

_SKILL_FILE = "script_conversion.skill.md"


def convert_m_queries(app_name: str) -> list[str]:
    skill = load_skill(_SKILL_FILE)
    script = load_extracted(app_name, "script.qvs")
    data_model = load_extracted(app_name, "data_model.json")

    paths = []
    for table in data_model.get("tables", []):
        table_name = table.get("qName") or table.get("name")
        if not table_name:
            continue
        payload = {"task": "m_query", "table_name": table_name, "script": script, "data_model_table": table}
        print(f"[convert] table '{table_name}' (script.qvs, data_model.json) -> script_conversion.skill.md (m_query)")
        m_code = run_skill(skill, payload, json_output=False)
        m_code = strip_code_fence(m_code)
        m_code = _extract_m_confidence_comment(table_name, m_code)
        paths.append(write_converted(app_name, f"m_query__{safe(table_name)}.m", m_code))
    return paths


_M_CONFIDENCE_RE = re.compile(r"^\s*//\s*CONFIDENCE:\s*(low|medium)\s*-\s*(.+?)\s*\n", re.IGNORECASE)


def _extract_m_confidence_comment(table_name: str, m_code: str) -> str:
    """The m_query task's output is raw M text (no JSON wrapper — the
    contract is 'just the M code, ready to paste'), so it can't carry a
    structured confidence field the way the JSON-output tasks do. Instead
    the skill is asked to prepend a `// CONFIDENCE: low - reason` comment
    line only when uncertain; parse that out here (feeding it into the same
    batch summary as everything else) and strip it before writing the .m
    file, since a leading comment there would otherwise become part of the
    TMDL partition source."""
    match = _M_CONFIDENCE_RE.match(m_code)
    if not match:
        return m_code
    confidence, reason = match.group(1).lower(), match.group(2)
    collect_confidence("m_query", [{"name": table_name, "confidence": confidence, "notes": reason}])
    return m_code[match.end():]


def convert_parameters(app_name: str) -> str | None:
    variables = load_extracted(app_name, "variables.json")
    if not variables:
        print("[convert] no variables in this app — skipping script_conversion.skill.md (variables) call")
        return None
    skill = load_skill(_SKILL_FILE)
    script = load_extracted(app_name, "script.qvs")
    print(f"[convert] variables.json ({len(variables)} variables), script.qvs -> script_conversion.skill.md (variables)")
    result = run_skill(skill, {"task": "variables", "variables": variables, "script": script}, json_output=True)
    collect_confidence("parameters_variables", result.get("variables", []))
    return write_converted(app_name, "variables.converted.json", result)
