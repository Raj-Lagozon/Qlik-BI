"""Master dimensions -> DAX columns/hierarchies (sheets_convert.skill.md, task=dimensions)."""

from __future__ import annotations

from app.utilities.llm import collect_confidence, load_extracted, load_skill, run_skill, write_converted

_SKILL_FILE = "sheets_convert.skill.md"


def convert_dimensions(app_name: str) -> str | None:
    dimensions = load_extracted(app_name, "dimensions.json")
    if not dimensions:
        print("[convert] no master dimensions in this app — skipping sheets_convert.skill.md (dimensions) call")
        return None
    skill = load_skill(_SKILL_FILE)
    data_model = load_extracted(app_name, "data_model.json")
    print(f"[convert] dimensions.json ({len(dimensions)} master dimensions) -> sheets_convert.skill.md (dimensions)")
    result = run_skill(skill, {"task": "dimensions", "dimensions": dimensions, "data_model": data_model}, json_output=True)
    collect_confidence("dax_columns_hierarchies", result.get("items", []))
    return write_converted(app_name, "dimensions.converted.json", result)
