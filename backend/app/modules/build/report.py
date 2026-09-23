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


_QLIK_ACTION_TYPE_TO_VISUALLINK_FIELD = {
    "PageNavigation": "navigationSection",
    "Bookmark": "bookmark",
    "WebUrl": "webUrl",
    "Drillthrough": "drillthroughSection",
    "QnA": "qna",
}


def _dax_literal(value) -> dict:
    if isinstance(value, str):
        escaped = value.replace("'", "''")
        return {"expr": {"Literal": {"Value": f"'{escaped}'"}}}
    if isinstance(value, bool):
        return {"expr": {"Literal": {"Value": "true" if value else "false"}}}
    return {"expr": {"Literal": {"Value": str(value)}}}


def _relocate_button_action(inner_visual: dict) -> None:
    """A button's navigation (page navigation / bookmark / drillthrough /
    web URL) is a CONTAINER-level property in real PBIR — but there is no
    `action` property anywhere in the schema. Confirmed against the real
    Fabric visualConfiguration schema (definition/visualConfiguration/2.2.0
    /schema-embedded.json): the actual container object group is
    `visualLink` (a sibling of `general`/`title`/`subTitle` under
    `visualContainerObjects`, itself nested INSIDE `visual`), whose `VisualLink`
    properties are `show`, `type`, `bookmark`, `webUrl`, `navigationSection`,
    `drillthroughSection`, `qna`, `tooltip`, etc — never a generic `action`/
    `destination` pair. `{"action": {"type": "PageNavigation", "destination":
    "<x>"}}` (this skill's own working shape for the LLM to emit, kept
    because it's simple and unambiguous for the model) is translated here
    into the real `visualLink` shape: `type` becomes the DAX-literal-quoted
    action type, and `destination` is written into whichever field that
    type implies (PageNavigation -> navigationSection, Bookmark -> bookmark,
    WebUrl -> webUrl, Drillthrough -> drillthroughSection, QnA -> qna).
    `action` was previously (wrongly) emitted verbatim as a made-up
    `general[0].properties.action` property, which Power BI Desktop's schema
    rejected outright ("An additional property 'action' was included in the
    /visual/visualContainerObjects/general/0/properties property"),
    corrupting the whole report on open. In practice the LLM has also
    nested this raw `{"type": ..., "destination": ...}` dict directly under
    `visualContainerObjects.general[0].properties.action` (skipping the
    top-level `inner_visual["action"]` shape entirely) alongside a made-up
    `buttonText` property (also not a real schema property anywhere) — so
    this function checks both locations. `buttonText`/`text`/`title` found
    next to `action` (whichever key name the LLM used) is relocated to
    `visual.objects.text[0].properties.text` — actionButton's own
    visual-TYPE-SPECIFIC "Text" formatting card, not a container-level
    property, per Power BI Desktop's real action-button formatting pane
    (Style/Icon/Text/Outline/Fill) — this placement is a best-effort mapping
    from that observed formatting-pane structure, not independently
    schema-verified the way `visualLink` is, so treat it as the primary
    suspect first if a button's label still doesn't render.
    Never trust every future LLM call to get this exactly right, the same
    defensive pattern as `_fix_query_refs` in modules/build/project.py."""
    if not isinstance(inner_visual, dict):
        return
    action = inner_visual.pop("action", None)
    button_text = None
    for key in ("buttonText", "text", "title"):
        if key in inner_visual:
            button_text = inner_visual.pop(key)
            break

    container_objects = inner_visual.get("visualContainerObjects")
    general = container_objects.get("general") if isinstance(container_objects, dict) else None
    if isinstance(general, list) and general and isinstance(general[0], dict):
        props = general[0].get("properties")
        if isinstance(props, dict):
            if action is None:
                action = props.pop("action", None)
            if button_text is None:
                for key in ("buttonText", "text", "title"):
                    if key in props:
                        button_text = props.pop(key)
                        break
            if not props:
                general.pop(0)
            if not general:
                container_objects.pop("general", None)

    if isinstance(action, dict):
        action_type = action.get("type")
        destination = action.get("destination")
        field = _QLIK_ACTION_TYPE_TO_VISUALLINK_FIELD.get(action_type)
        container_objects = inner_visual.setdefault("visualContainerObjects", {})
        visual_link = container_objects.setdefault("visualLink", [{"properties": {}}])
        props = visual_link[0].setdefault("properties", {})
        props["show"] = _dax_literal(True)
        if action_type:
            props["type"] = _dax_literal(action_type)
        if field and destination:
            props[field] = _dax_literal(destination)

    if button_text is not None:
        objects = inner_visual.setdefault("objects", {})
        text_group = objects.setdefault("text", [{"properties": {}}])
        text_group[0].setdefault("properties", {})["text"] = (
            button_text if isinstance(button_text, dict) else _dax_literal(button_text)
        )


def _dax_literal_text(node) -> str | None:
    """Best-effort extraction of the plain text a `_dax_literal(str)` call
    produced (single-quoted DAX string literal, with '' as the escaped
    quote), for re-merging two such literals. Returns None if `node` isn't
    that shape."""
    if not isinstance(node, dict):
        return None
    value = node.get("expr", {}).get("Literal", {}).get("Value")
    if not isinstance(value, str) or len(value) < 2 or not value.startswith("'") or not value.endswith("'"):
        return None
    return value[1:-1].replace("''", "'")


def _relocate_footer_to_subtitle(inner_visual: dict) -> None:
    """The LLM has, in practice, invented a `footer` property (for a Qlik
    KPI's footnote/subtext line) under `visualContainerObjects` — this is
    NOT a real PBIR property anywhere (confirmed against the real Fabric
    visualConfiguration schema: the only text-bearing container groups are
    `title` and `subTitle`), and Power BI Desktop rejects the whole report
    over it ("An additional property 'footer' was included in the
    /visual/visualContainerObjects property"). Power BI's card/KPI-style
    visuals have no third text zone to put a footnote in, so the closest
    honest equivalent is folding the footer text into `subTitle` — appended
    after any real subtitle text if one is already present, or used as the
    subtitle outright if not. Same defensive philosophy as
    `_relocate_button_action` — never trust every LLM response to only use
    documented property names."""
    if not isinstance(inner_visual, dict):
        return
    container_objects = inner_visual.get("visualContainerObjects")
    if not isinstance(container_objects, dict) or "footer" not in container_objects:
        return
    footer = container_objects.pop("footer")
    if not isinstance(footer, list) or not footer or not isinstance(footer[0], dict):
        return
    footer_props = footer[0].get("properties", {})
    footer_text = _dax_literal_text(footer_props.get("text"))
    if footer_text is None:
        return

    sub_title = container_objects.setdefault("subTitle", [{"properties": {}}])
    props = sub_title[0].setdefault("properties", {})
    existing_text = _dax_literal_text(props.get("text"))
    merged_text = f"{existing_text} · {footer_text}" if existing_text else footer_text
    props["text"] = _dax_literal(merged_text)
    props.setdefault("show", _dax_literal(True))


def _sanitize_visual_extra_keys(visual: dict, inner_visual: dict) -> None:
    """Three shape mistakes seen in practice, all rejecting the WHOLE
    report the same way `action`-inside-`visual` does ("An additional
    property '<x>' was included in the /visual[/query] property"):

    1. `confidence`/`notes` — this task's own review metadata (see
       sheets_convert.skill.md's Task C) — belong as top-level siblings of
       `visual` on the SAME visual entry, and the skill says outright "the
       builder strips it before writing the real PBIR file". Strip them
       from wherever they land: the correct top-level spot (`visual`, the
       whole dict passed in here, not `inner_visual`), AND defensively
       from `inner_visual` itself, since real output has shown the LLM
       nesting them a level too deep despite the documented shape.
    2. `objects` — visual-level formatting/style overrides — belongs as a
       sibling of `query` inside `visual`, never nested one level deeper
       INSIDE `query` itself (PBIR's `query` only ever has `queryState`).
       Relocate it up a level if found there, without clobbering an
       `objects` that's already correctly placed at the `visual` level.
    3. `title`/`subTitle` — a CONTAINER-level property (skill.md's own
       documented shape puts these under a `visualContainerObjects` key
       NESTED INSIDE `visual`, the same category as `action`) — but real
       output has shown the LLM instead nesting them under
       `visual.objects.title`/`subTitle` (a visual-TYPE-SPECIFIC properties
       bag, which doesn't recognize these keys at all for most visual
       types). Relocate them into `visual.visualContainerObjects` if found
       there, without clobbering a title/subTitle that's already correctly
       placed.

    Same defensive philosophy as `_relocate_button_action` right above —
    never trust every LLM response to get this exactly right."""
    visual.pop("confidence", None)
    visual.pop("notes", None)
    if not isinstance(inner_visual, dict):
        return
    inner_visual.pop("confidence", None)
    inner_visual.pop("notes", None)
    query = inner_visual.get("query")
    if isinstance(query, dict) and "objects" in query:
        misplaced_objects = query.pop("objects")
        inner_visual.setdefault("objects", misplaced_objects)

    inner_objects = inner_visual.get("objects")
    if isinstance(inner_objects, dict):
        for key in ("title", "subTitle"):
            if key in inner_objects:
                misplaced = inner_objects.pop(key)
                container_objects = inner_visual.setdefault("visualContainerObjects", {})
                container_objects.setdefault(key, misplaced)


def write_report(report_dir: str, pages: list[dict], *, app_name: str, custom_theme: dict | None = None) -> None:
    """`pages` is a list of {"page": {...}, "visuals": [...]}` dicts, one per
    Qlik sheet, as produced by llm_convert._convert_report.

    `custom_theme` (see build/theme.py) is a Power BI Report Theme JSON
    built from the source Qlik app's own extracted color palette — when
    given, it's written into the project as a REGISTERED (user-added)
    theme and set as the report's active theme, alongside the default base
    theme (kept as the underlying foundation every theme builds on top
    of — Power BI always needs one, custom or not, see below). When None
    (no usable color was extracted from this app), the report uses the
    plain default base theme only, exactly as before this parameter
    existed."""
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
    resource_packages = [
        {
            "name": "SharedResources",
            "type": "SharedResources",
            "items": [
                {"name": default_theme_name, "path": f"BaseThemes/{default_theme_name}.json", "type": "BaseTheme"}
            ],
        }
    ]
    theme_collection = {
        "baseTheme": {
            "name": default_theme_name,
            "reportVersionAtImport": {"visual": "1.8.50", "report": "2.0.50", "page": "1.3.50"},
            "type": "SharedResources",
        }
    }
    if custom_theme:
        # A REGISTERED (user-added) theme, as opposed to one of Power BI's
        # own built-in named themes (the baseTheme above, which Desktop
        # already knows by name with no file needed) — this one's actual
        # JSON content has to physically exist in the project for Desktop
        # (and pbip-compiler) to find it, under StaticResources/
        # RegisteredResources/, confirmed against how Power BI Desktop
        # itself lays out a theme added via View > Themes > Browse for
        # themes. baseTheme is deliberately kept alongside customTheme
        # (not replaced) — it's the foundation Power BI still falls back
        # to for anything the custom theme's own (deliberately minimal —
        # just name + dataColors) JSON doesn't specify itself.
        theme_name = custom_theme["name"]
        theme_filename = f"{_safe(theme_name)}.json"
        registered_dir = os.path.join(report_dir, "StaticResources", "RegisteredResources")
        os.makedirs(registered_dir, exist_ok=True)
        with open(os.path.join(registered_dir, theme_filename), "w", encoding="utf-8") as f:
            json.dump(custom_theme, f, indent=2)
        theme_collection["customTheme"] = {
            "name": theme_name,
            "reportVersionAtImport": {"visual": "1.8.50", "report": "2.0.50", "page": "1.3.50"},
            "type": "RegisteredResources",
        }
        resource_packages.append({
            "name": "RegisteredResources",
            "type": "RegisteredResources",
            "items": [{"name": theme_name, "path": theme_filename, "type": "CustomTheme"}],
        })

    report_json = {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/report/3.1.0/schema.json",
        "themeCollection": theme_collection,
        "resourcePackages": resource_packages,
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
            inner_visual = visual.get("visual", {})
            visual_json = {
                "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.5.0/schema.json",
                "name": visual_id,
                "position": visual.get("position", {"x": 0, "y": 0, "width": 300, "height": 200, "tabOrder": v_ordinal}),
                "visual": inner_visual,
            }
            if "filterConfig" in visual:
                visual_json["filterConfig"] = visual["filterConfig"]
            # A container-level property the LLM emitted correctly (title/
            # subTitle, or a button's action before _relocate_button_action
            # even runs) was previously DROPPED entirely — the dict literal
            # above never copied this key from the source `visual` at all,
            # so even a perfectly-shaped visualContainerObjects from the
            # LLM never made it into the real file. Copy it in first, before
            # the two sanitizers below (which both expect to merge INTO
            # whatever's already here, not create it from nothing).
            # NOTE: `visualContainerObjects` belongs NESTED INSIDE `visual`
            # (a sibling of `objects`/`query`), never as a top-level key of
            # visual.json itself — Power BI Desktop's schema rejects the
            # latter as an "additional property... in the root property",
            # corrupting the whole report on open. `inner_visual` here is
            # the exact same dict object as `visual_json["visual"]`, so
            # writing into it writes into the right place.
            if isinstance(visual.get("visualContainerObjects"), dict) and isinstance(inner_visual, dict):
                inner_visual["visualContainerObjects"] = visual["visualContainerObjects"]
            _relocate_button_action(inner_visual)
            _relocate_footer_to_subtitle(inner_visual)
            _sanitize_visual_extra_keys(visual, inner_visual)
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
