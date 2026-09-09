"""Write a *.Report/ folder (PBIR) from the converted per-sheet
page/visual JSON produced by the report_visuals skill."""

from __future__ import annotations

import json
import os
import uuid


def write_report(report_dir: str, pages: list[dict], *, app_name: str) -> None:
    """`pages` is a list of {"page": {...}, "visuals": [...]}` dicts, one per
    Qlik sheet, as produced by llm_convert._convert_report."""
    defn_dir = os.path.join(report_dir, "definition")
    pages_dir = os.path.join(defn_dir, "pages")
    os.makedirs(pages_dir, exist_ok=True)

    # pbip-compiler doesn't need these two files (it reads definition/
    # directly), but Power BI Desktop refuses to open the .pbip at all
    # without them — "Required artifact is missing in .../definition.pbir".
    write_platform_file(report_dir, item_type="Report", display_name=app_name)
    with open(os.path.join(report_dir, "definition.pbir"), "w", encoding="utf-8") as f:
        json.dump({
            "version": "4.0",
            "datasetReference": {
                "byPath": {"path": f"../{app_name}.SemanticModel"}
            },
        }, f, indent=2)

    # A report with no theme reference at all crashes Power BI Desktop's
    # ribbon on load ("Cannot read properties of undefined (reading
    # 'customTheme')") — always ship a default base theme.
    default_theme_name = "CY24SU08"
    report_json = {
        "resourcePackages": [
            {
                "name": "SharedResources",
                "type": "SharedResources",
                "items": [
                    {"type": "BaseTheme", "path": f"BaseThemes/{default_theme_name}.json", "name": default_theme_name}
                ],
            }
        ],
        "themeCollection": {
            "baseTheme": {
                "name": default_theme_name,
                "reportVersionAtImport": "5.55",
                "type": "SharedResources",
            }
        },
        "settings": {},
    }
    with open(os.path.join(defn_dir, "report.json"), "w", encoding="utf-8") as f:
        json.dump(report_json, f, indent=2)

    page_order = []
    for ordinal, page_data in enumerate(pages):
        page = page_data.get("page", {})
        page_id = _safe(page.get("name") or f"Page{ordinal + 1}")
        page_order.append(page_id)

        page_dir = os.path.join(pages_dir, page_id)
        visuals_dir = os.path.join(page_dir, "visuals")
        os.makedirs(visuals_dir, exist_ok=True)

        page_json = {
            "name": page_id,
            "displayName": page.get("displayName", page_id),
            "width": page.get("width", 1280),
            "height": page.get("height", 720),
            "ordinal": page.get("ordinal", ordinal),
        }
        with open(os.path.join(page_dir, "page.json"), "w", encoding="utf-8") as f:
            json.dump(page_json, f, indent=2)

        for v_ordinal, visual in enumerate(page_data.get("visuals", [])):
            visual_id = _safe(visual.get("name") or f"visual_{v_ordinal}")
            visual_dir = os.path.join(visuals_dir, visual_id)
            os.makedirs(visual_dir, exist_ok=True)
            visual_json = {
                "name": visual_id,
                "position": visual.get("position", {"x": 0, "y": 0, "width": 300, "height": 200, "tabOrder": v_ordinal}),
                "visual": visual.get("visual", {}),
            }
            if "filterConfig" in visual:
                visual_json["filterConfig"] = visual["filterConfig"]
            with open(os.path.join(visual_dir, "visual.json"), "w", encoding="utf-8") as f:
                json.dump(visual_json, f, indent=2)

    with open(os.path.join(pages_dir, "pages.json"), "w", encoding="utf-8") as f:
        json.dump({"pageOrder": page_order, "activePage": page_order[0] if page_order else None}, f, indent=2)


def write_platform_file(item_dir: str, *, item_type: str, display_name: str) -> None:
    """The `.platform` sidecar every top-level PBIP item folder (both
    *.Report and *.SemanticModel) needs — Power BI Desktop's Fabric git
    integration metadata, not read by pbip-compiler."""
    platform = {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",
        "metadata": {"type": item_type, "displayName": display_name},
        "config": {"version": "2.0", "logicalId": str(uuid.uuid4())},
    }
    with open(os.path.join(item_dir, ".platform"), "w", encoding="utf-8") as f:
        json.dump(platform, f, indent=2)


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "_-" else "_" for c in name)
