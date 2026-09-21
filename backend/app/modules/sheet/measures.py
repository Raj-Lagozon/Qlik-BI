"""Master measures -> DAX measures (sheets_convert.skill.md, task=measures)."""

from __future__ import annotations

from app.utilities.llm import collect_confidence, load_extracted, load_skill, run_skill, write_converted

_SKILL_FILE = "sheets_convert.skill.md"


def convert_measures(app_name: str) -> str | None:
    measures = load_extracted(app_name, "measures.json")
    if not measures:
        print("[convert] no master measures in this app — skipping sheets_convert.skill.md (measures) call")
        return None
    skill = load_skill(_SKILL_FILE)
    data_model = load_extracted(app_name, "data_model.json")
    print(f"[convert] measures.json ({len(measures)} master measures) -> sheets_convert.skill.md (measures)")
    result = run_skill(skill, {"task": "measures", "measures": measures, "data_model": data_model}, json_output=True)
    collect_confidence("dax_measures", result.get("measures", []))
    return write_converted(app_name, "measures.converted.json", result)
