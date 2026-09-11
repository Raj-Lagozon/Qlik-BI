"""Write a *.Report/ folder (PBIR) from the converted per-sheet
page/visual JSON produced by the report_visuals skill."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid

# Power BI (PBIR) hard limit on a visual's `name`: 1..50 characters. Qlik
# object ids for container-generated pseudo-objects blow well past this
# (e.g. "qlik-compound-context-<guid>-link-<guid>-qlik" ~= 90 chars).
_MAX_VISUAL_NAME = 50


def _bounded_visual_name(raw: str, taken: set[str]) -> str:
    """A <=50-char, page-unique, build-stable visual name. Short names pass
    through untouched; an over-length name keeps a readable prefix and gets a
    deterministic hash suffix so it never changes between builds and never
    collides with a sibling on the same page."""
    name = raw or "visual"
    if len(name) > _MAX_VISUAL_NAME:
        digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]
        name = f"{name[:_MAX_VISUAL_NAME - 11]}-{digest}"
    if name in taken:
        base = name[:_MAX_VISUAL_NAME - 4]
        i = 2
        while f"{base}-{i}" in taken:
            i += 1
        name = f"{base}-{i}"
    taken.add(name)
    return name


def _rewrite_name_refs(node, rename: dict[str, str]):
    """Replace any string value anywhere in `node` that equals a renamed
    visual's old name with its new name — covers cross-references like a
    child visual's `parentGroupName` or a filter/bookmark `visualName`."""
    if isinstance(node, dict):
        return {k: _rewrite_name_refs(v, rename) for k, v in node.items()}
    if isinstance(node, list):
        return [_rewrite_name_refs(v, rename) for v in node]
    if isinstance(node, str):
        return rename.get(node, node)
    return node


def write_report(report_dir: str, pages: list[dict], *, app_name: str) -> None:
    """`pages` is a list of {"page": {...}, "visuals": [...]}` dicts, one per
    Qlik sheet, as produced by llm_convert._convert_report."""
    defn_dir = os.path.join(report_dir, "definition")
    pages_dir = os.path.join(defn_dir, "pages")
    # Regenerate from a CLEAN slate. Visual/page folder names are derived
    # from (sometimes renamed) Qlik object ids, so a rebuild that shortens or
    # renames a folder would otherwise leave the OLD folder sitting next to
    # the new one — Power BI reads every folder under pages/*/visuals/ and
    # would still hit the stale one ("visual name exceeds 50 characters" on a
    # name the current build no longer emits). Wiping pages/ each time keeps
    # what's on disk exactly equal to what this build produced.
    if os.path.isdir(pages_dir):
        shutil.rmtree(pages_dir)
    os.makedirs(pages_dir, exist_ok=True)

    # pbip-compiler doesn't need these two files (it reads definition/
    # directly), but Power BI Desktop refuses to open the .pbip at all
    # without them — "Required artifact is missing in .../definition.pbir".
    write_platform_file(report_dir, item_type="Report", display_name=app_name)
    # $schema confirmed against a real Desktop-authored definition.pbir
    # (found bundled as a pbix-mcp test fixture) — our earlier version
    # omitted it entirely, very likely part of why Power BI Desktop has
    # been refusing to open the raw .pbip project directly.
    with open(os.path.join(report_dir, "definition.pbir"), "w", encoding="utf-8") as f:
        json.dump({
            "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definitionProperties/2.0.0/schema.json",
            "version": "4.0",
            "datasetReference": {
                "byPath": {"path": f"../{app_name}.SemanticModel"}
            },
        }, f, indent=2)

    # A report with no theme reference at all crashes Power BI Desktop's
    # ribbon on load ("Cannot read properties of undefined (reading
    # 'customTheme')") — always ship a default base theme. Shape confirmed
    # against a real Desktop-authored report.json (the same pbix-mcp test
    # fixture used for version.json/relationships.tmdl/model.tmdl above) —
    # our earlier version had no $schema at all, and reportVersionAtImport
    # is a {visual, report, page} object, not a bare version string.
    default_theme_name = "CY24SU08"
    report_json = {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/report/3.1.0/schema.json",
        "themeCollection": {
            "baseTheme": {
                "name": default_theme_name,
                "reportVersionAtImport": {"visual": "1.8.50", "report": "2.0.50", "page": "1.3.50"},
                "type": "SharedResources",
            }
        },
        "resourcePackages": [
            {
                "name": "SharedResources",
                "type": "SharedResources",
                "items": [
                    {"name": default_theme_name, "path": f"BaseThemes/{default_theme_name}.json", "type": "BaseTheme"}
                ],
            }
        ],
        "settings": {},
    }
    with open(os.path.join(defn_dir, "report.json"), "w", encoding="utf-8") as f:
        json.dump(report_json, f, indent=2)

    # Power BI Desktop refuses to open a .pbip project directly without this
    # ("Cannot find file 'version.json'") — confirmed against a real,
    # Desktop-authored reference .Report/definition/ folder (found bundled
    # as a pbix-mcp test fixture); pbip-compiler doesn't need it at all
    # (it never reads this file), same as .platform/definition.pbir above.
    with open(os.path.join(defn_dir, "version.json"), "w", encoding="utf-8") as f:
        json.dump({
            "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/versionMetadata/1.0.0/schema.json",
            "version": "2.0.0",
        }, f, indent=2)

    page_order = []
    for ordinal, page_data in enumerate(pages):
        page = page_data.get("page", {})
        page_id = _safe(page.get("name") or f"Page{ordinal + 1}")
        page_order.append(page_id)

        page_dir = os.path.join(pages_dir, page_id)
        visuals_dir = os.path.join(page_dir, "visuals")
        os.makedirs(visuals_dir, exist_ok=True)

        # $schema/displayOption confirmed against a real Desktop-authored
        # page.json; note there's no "ordinal" field there at all — page
        # order comes entirely from pages.json's pageOrder array below, so
        # a stray ordinal field here (present in our earlier version) is
        # extra content the real schema doesn't have.
        page_json = {
            "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/page/2.0.0/schema.json",
            "name": page_id,
            "displayName": page.get("displayName", page_id),
            "displayOption": "FitToPage",
            "height": page.get("height", 720),
            "width": page.get("width", 1280),
        }
        with open(os.path.join(page_dir, "page.json"), "w", encoding="utf-8") as f:
            json.dump(page_json, f, indent=2)

        # Resolve every visual's name first (bounded to PBIR's 50-char limit,
        # unique within the page), building an old->new map so any
        # cross-reference between visuals on this page can be rewritten too.
        visuals = page_data.get("visuals", [])
        taken: set[str] = set()
        name_map: dict[str, str] = {}
        resolved_ids: list[str] = []
        for v_ordinal, visual in enumerate(visuals):
            raw_id = _safe(visual.get("name") or f"visual_{v_ordinal}")
            visual_id = _bounded_visual_name(raw_id, taken)
            resolved_ids.append(visual_id)
            if visual_id != raw_id:
                name_map[raw_id] = visual_id

        for v_ordinal, (visual, visual_id) in enumerate(zip(visuals, resolved_ids)):
            visual_dir = os.path.join(visuals_dir, visual_id)
            os.makedirs(visual_dir, exist_ok=True)
            visual_json = {
                "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.5.0/schema.json",
                "name": visual_id,
                "position": visual.get("position", {"x": 0, "y": 0, "width": 300, "height": 200, "tabOrder": v_ordinal}),
                "visual": visual.get("visual", {}),
            }
            if "filterConfig" in visual:
                visual_json["filterConfig"] = visual["filterConfig"]
            if name_map:
                visual_json = _rewrite_name_refs(visual_json, name_map)
                visual_json["name"] = visual_id  # never let a ref-rewrite touch our own key
            with open(os.path.join(visual_dir, "visual.json"), "w", encoding="utf-8") as f:
                json.dump(visual_json, f, indent=2)

    # $schema, and "activePageName" (not "activePage" — a real, confirmed
    # property-name mismatch in our earlier version, not just a missing
    # $schema) — both confirmed against a real Desktop-authored pages.json.
    with open(os.path.join(pages_dir, "pages.json"), "w", encoding="utf-8") as f:
        json.dump({
            "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/pagesMetadata/1.0.0/schema.json",
            "pageOrder": page_order,
            "activePageName": page_order[0] if page_order else None,
        }, f, indent=2)


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
