"""Phase 1 visual QA: a structural (not pixel-level) sanity check comparing
each Qlik sheet's own objects against what actually landed in the built
Power BI report — writes output/<app_name>/VISUAL_QA.md.

This does NOT render or screenshot anything (that's the Phase 2 pixel-diff
idea, which needs a Power BI Service workspace this project doesn't have
credentials for yet) — it only compares STRUCTURE already sitting on disk:
object counts, visual-type coverage, and how much of each page's canvas
area is actually occupied. That's enough to catch the exact class of bug
this session spent most of its time on by hand (a whole object silently
dropped, a real chart falling back to an unmapped placeholder, a page
that's mostly empty because several bindings failed) — automatically,
right after every build, instead of only when someone happens to open
Power BI Desktop and notice something looks wrong.
"""

from __future__ import annotations

import datetime
import glob
import json
import os

EXTRACTED_ROOT_NAME = "extracted"
OUTPUT_ROOT_NAME = "output"

# A Qlik object type that legitimately has NO data-bearing visual
# equivalent — a shape/decoration, a text block, a filter/nav control not
# expected to occupy meaningful "chart" area — including Qlik's native
# `sn-`-prefixed variants of the same things (sn-shape, sn-text), and
# `childObject`, a generic container-child wrapper whose real type is
# ambiguous at this level (could be anything) and so is excluded rather
# than risk it being wrongly counted as "should have become a real
# chart". Excluded from the "real visual" counts/ratios below so a sheet
# full of decorative shapes/labels doesn't look like a conversion failure.
_NON_DATA_QLIK_TYPES = {
    "shape", "sn-shape", "text-image", "sn-text", "textbox",
    "action-button", "button", "filterpane", "listbox", "childObject",
}

# The Power BI visualType this pipeline's own placeholder fallbacks always
# use for "no reasonable Power BI equivalent" (see report.py/project.py) —
# seeing one of these where the ORIGINAL Qlik object was a real chart/table
# (not already one of _NON_DATA_QLIK_TYPES) is the single strongest signal
# of a silently lost visual.
_PLACEHOLDER_VISUAL_TYPES = {"textbox"}


def _load_sheets(extracted_dir: str) -> dict[str, dict]:
    """{sheet_id: {"title": str, "objects": [{"id","type","area"}, ...],
    "total_area": float}} — "area" is width*height in the Qlik sheet's OWN
    units (percentage-of-sheet or grid cells, whichever this app uses);
    only ever compared as a RATIO against the sheet's own total, never
    against Power BI's pixel units directly, so the unit itself doesn't
    matter."""
    path = os.path.join(extracted_dir, "sheets.json")
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    raw_sheets = data if isinstance(data, list) else data.get("sheets", [])

    out: dict[str, dict] = {}
    for sheet in raw_sheets:
        sheet_id = sheet.get("id")
        if not sheet_id:
            continue
        objects = []
        for obj in sheet.get("objects", []):
            bounds = obj.get("bounds") or {}
            area = float(bounds.get("width", 0)) * float(bounds.get("height", 0))
            objects.append({"id": obj.get("id"), "type": obj.get("type"), "area": area})
        out[sheet_id] = {
            "title": sheet.get("title") or sheet_id,
            "objects": objects,
            "total_area": sum(o["area"] for o in objects) or 1.0,
        }
    return out


def _load_built_pages(project_dir: str, app_name: str) -> dict[str, dict]:
    """{page_id: {"visuals": [{"visualType","area"}, ...], "page_area":
    float}} read straight from the compiled PBIR output on disk — page_id
    matches a Qlik sheet's own id 1:1 (report.py names each page after the
    Qlik sheet id it came from)."""
    report_glob = os.path.join(project_dir, f"{app_name}.Report", "definition", "pages", "*")
    out: dict[str, dict] = {}
    for page_dir in glob.glob(report_glob):
        if not os.path.isdir(page_dir):
            continue  # the pages/ folder also holds the sibling pages.json manifest file
        page_id = os.path.basename(page_dir)
        page_json_path = os.path.join(page_dir, "page.json")
        page_width, page_height = 1280.0, 720.0
        if os.path.exists(page_json_path):
            with open(page_json_path, encoding="utf-8") as f:
                page_json = json.load(f)
            page_width = float(page_json.get("width", page_width))
            page_height = float(page_json.get("height", page_height))

        visuals = []
        for visual_path in glob.glob(os.path.join(page_dir, "visuals", "*", "visual.json")):
            with open(visual_path, encoding="utf-8") as f:
                visual_json = json.load(f)
            visual = visual_json.get("visual", {})
            position = visual_json.get("position", {})
            area = float(position.get("width", 0)) * float(position.get("height", 0))
            visuals.append({"visualType": visual.get("visualType"), "area": area})
        out[page_id] = {"visuals": visuals, "page_area": page_width * page_height}
    return out


def write_visual_qa(app_name: str, extracted_root: str, output_root: str) -> str:
    """Writes output/<app_name>/VISUAL_QA.md and returns its path. Always
    written (even with nothing to flag), same convention as
    project.py's MANUAL_REVIEW.md — one consistent place to check."""
    extracted_dir = os.path.join(extracted_root, app_name)
    project_dir = os.path.join(output_root, app_name)

    sheets = _load_sheets(extracted_dir)
    built_pages = _load_built_pages(project_dir, app_name)

    lines = [
        f"# Visual QA — {app_name}",
        "",
        f"Generated {datetime.datetime.now().isoformat(timespec='seconds')} by the QVF -> PBIX build.",
        "",
        "A STRUCTURAL comparison only — object counts, visual types, and how much of each ",
        "page's canvas is occupied — not a pixel-level visual match. Use this to catch a ",
        "silently dropped object or an unexpectedly empty page before opening Power BI Desktop.",
        "",
    ]

    findings: list[str] = []
    for sheet_id, sheet in sheets.items():
        title = sheet["title"]
        page = built_pages.get(sheet_id)
        if page is None:
            findings.append(f"**{title}** (`{sheet_id}`): sheet has {len(sheet['objects'])} object(s) "
                             f"in Qlik, but NO corresponding page exists in the built report at all.")
            continue

        qlik_data_objects = [o for o in sheet["objects"] if o["type"] not in _NON_DATA_QLIK_TYPES]
        built_visual_count = len(page["visuals"])
        qlik_object_count = len(sheet["objects"])

        if qlik_data_objects and built_visual_count == 0:
            findings.append(f"**{title}** (`{sheet_id}`): {len(qlik_data_objects)} real chart/table/KPI "
                             f"object(s) in Qlik, but the built page has NO visuals at all.")
            continue

        if qlik_object_count > 0 and built_visual_count < qlik_object_count * 0.6:
            findings.append(f"**{title}** (`{sheet_id}`): only {built_visual_count} visual(s) in the built "
                             f"page vs {qlik_object_count} object(s) in the Qlik sheet — "
                             f"{qlik_object_count - built_visual_count} may have been dropped.")

        # A textbox in the OUTPUT is only suspicious if it's not already
        # explained by a Qlik object that was legitimately text/textbox-like
        # to begin with (a Qlik "text-image"/"sn-text" object SHOULD become
        # a plain textbox — that's a correct 1:1 conversion, not a fallback).
        # Only the textbox count BEYOND that expected baseline signals a
        # real chart/table/KPI silently falling back to a placeholder.
        expected_textbox_ish = sum(1 for o in sheet["objects"] if o["type"] in ("text-image", "sn-text", "textbox"))
        placeholder_count = sum(1 for v in page["visuals"] if v["visualType"] in _PLACEHOLDER_VISUAL_TYPES)
        unexplained_placeholders = max(0, placeholder_count - expected_textbox_ish)
        if unexplained_placeholders > 0 and len(qlik_data_objects) > 0:
            ratio = unexplained_placeholders / max(len(qlik_data_objects), 1)
            if ratio > 0.3:
                findings.append(f"**{title}** (`{sheet_id}`): {unexplained_placeholders} visual(s) beyond "
                                 f"what Qlik's own text/textbox objects account for ended up as plain "
                                 f"textbox placeholders, out of {len(qlik_data_objects)} real chart/table/KPI "
                                 f"object(s) — check whether real charts fell back to an unmapped-type "
                                 f"placeholder.")

        qlik_coverage = sum(o["area"] for o in qlik_data_objects) / sheet["total_area"] if sheet["total_area"] else 0
        built_coverage = sum(v["area"] for v in page["visuals"]) / page["page_area"] if page["page_area"] else 0
        if qlik_coverage > 0.15 and built_coverage < qlik_coverage * 0.5:
            findings.append(f"**{title}** (`{sheet_id}`): Qlik objects covered ~{qlik_coverage:.0%} of the "
                             f"sheet, but the built page's visuals only cover ~{built_coverage:.0%} of the "
                             f"page — likely several visuals ended up very small or empty.")

    for sheet_id, page in built_pages.items():
        if sheet_id not in sheets:
            findings.append(f"Built page `{sheet_id}` has no matching Qlik sheet in extraction — "
                             f"orphaned page from a stale/renamed build.")

    if not findings:
        lines.append("No structural issues detected — every Qlik sheet has a matching page, visual "
                      "counts and canvas coverage are in the expected range.")
    else:
        lines.extend(f"- {finding}" for finding in findings)

    os.makedirs(project_dir, exist_ok=True)
    path = os.path.join(project_dir, "VISUAL_QA.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"[build] wrote visual QA report -> {path} ({len(findings)} finding(s))")
    return path
