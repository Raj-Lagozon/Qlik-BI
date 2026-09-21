"""KPI-container config table -> individual KPI cards (sheets_convert.skill.md, task=kpi_container)."""

from __future__ import annotations

import json
import os

from app.utilities.llm import EXTRACTED_ROOT, collect_confidence, load_extracted, load_skill, run_skill, safe, write_converted

_SKILL_FILE = "sheets_convert.skill.md"


def convert_kpi_containers(app_name: str) -> list[str]:
    kpi_path = os.path.join(EXTRACTED_ROOT, app_name, "kpi_containers.json")
    if not os.path.exists(kpi_path):
        return []
    with open(kpi_path, encoding="utf-8") as f:
        containers = json.load(f)
    if not containers:
        return []

    skill = load_skill(_SKILL_FILE)
    measures = load_extracted(app_name, "measures.json")
    variables = load_extracted(app_name, "variables.json")

    paths = []
    for container in containers:
        payload = {
            "task": "kpi_container",
            "table": container["table"],
            "fields": container["fields"],
            "rows": container["rows"],
            "measures": measures,
            "variables": variables,
        }
        print(f"[convert] kpi_containers.json (table '{container['table']}') -> sheets_convert.skill.md (kpi_container)")
        result = run_skill(skill, payload, json_output=True)
        collect_confidence("kpi_container", result.get("kpis", []))
        paths.append(write_converted(app_name, f"kpi_container__{safe(container['table'])}.json", result))
    return paths
