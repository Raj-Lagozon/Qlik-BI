"""Converts the extracted Qlik associative data model into Power BI
semantic-model artifacts: table relationships, and Section Access -> RLS/OLS.
Both tasks are driven by the single consolidated `data_model.skill.md`."""

from __future__ import annotations

from app.utilities.llm import collect_confidence, load_extracted, load_skill, run_skill, write_converted

_SKILL_FILE = "data_model.skill.md"


def convert_data_model(app_name: str) -> str:
    skill = load_skill(_SKILL_FILE)
    data_model = load_extracted(app_name, "data_model.json")
    print("[convert] data_model.json -> data_model.skill.md (relationships)")
    result = run_skill(skill, {"task": "relationships", **data_model}, json_output=True)
    collect_confidence("data_model", result.get("relationships", []))
    return write_converted(app_name, "data_model.converted.json", result)


def convert_rls(app_name: str) -> str | None:
    section_access = load_extracted(app_name, "section_access.json")
    if not section_access.get("present"):
        print("[convert] no Section Access in source script — skipping data_model.skill.md (rls) call")
        return write_converted(app_name, "rls.converted.json",
                                {"roles": [], "notes": ["No Section Access in source script — no RLS to migrate."]})
    skill = load_skill(_SKILL_FILE)
    data_model = load_extracted(app_name, "data_model.json")
    print("[convert] section_access.json -> data_model.skill.md (rls)")
    result = run_skill(skill, {"task": "rls", "section_access": section_access, "data_model": data_model}, json_output=True)
    collect_confidence("rls_section_access", result.get("roles", []))
    return write_converted(app_name, "rls.converted.json", result)
