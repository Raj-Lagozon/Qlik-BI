"""Converts extracted Qlik bookmarks (extracted/<app>/bookmarks.json) into
real Power BI PBIR bookmark files under <Report>/definition/bookmarks/, and
builds a lookup so a button whose action is "Apply bookmark" can be wired
to a bookmark that actually exists in the project.

Before this module existed, `report.py`'s `_relocate_button_action` wrote
a `visualLink.bookmark` value straight from whatever destination string the
LLM supplied (the raw Qlik bookmark id or title) — but nothing anywhere
ever created a matching `*.bookmark.json` file, so the button silently
pointed at a bookmark that didn't exist. This closes that gap.

Schema note: the individual bookmark file's shape below (`$schema`, `name`,
`displayName`, `explorationState.{version,activeSection,sections}`) was
independently verified against the real Fabric bookmark schema
(`definition/bookmark/1.0.0/schema.json`) this session — required/optional
top-level keys and the `sections.<page>.visualContainers` nesting are
confirmed. The OPTIONAL `explorationState.filters` field (which would
restore the bookmark's captured Qlik selections as real Power BI slicer/
filter state) is deliberately NOT populated — its nested `FilterDefinition`
shape could not be independently verified in this pass (the referenced
`semanticQuery` schema wasn't reachable), and given this project's repeated
history of a guessed PBIR shape corrupting the WHOLE report, guessing a
new, unverified, deeply-nested field is a worse outcome than shipping a
verified-safe bookmark that captures the target PAGE correctly but not yet
the filter state. See PARTIALLY SUPPORTED note in `write_bookmarks`.

Also unverified: whether Power BI Desktop requires a `bookmarks.json`
index file (mirroring `pages/pages.json`) for bookmarks to appear at all —
**confirmed by a real user report**: Power BI Desktop refuses to open the
whole project without it ("Cannot find file 'bookmarks/bookmarks.json'").
`write_bookmarks` writes this index (schema verified against the real
Fabric `bookmarksMetadata` schema) alongside the individual bookmark files.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil

_MAX_BOOKMARK_NAME = 50  # mirrors report.py's _MAX_VISUAL_NAME — same PBIR name-length convention


def _bounded_bookmark_name(raw: str, taken: set[str]) -> str:
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", raw or "bookmark")
    if len(name) > _MAX_BOOKMARK_NAME:
        digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]
        name = f"{name[:_MAX_BOOKMARK_NAME - 11]}-{digest}"
    if name in taken:
        base = name[:_MAX_BOOKMARK_NAME - 4]
        i = 2
        while f"{base}-{i}" in taken:
            i += 1
        name = f"{base}-{i}"
    taken.add(name)
    return name


def write_bookmarks(report_dir: str, bookmarks: list[dict], page_order: list[str]) -> dict[str, str]:
    """Writes one `<name>.bookmark.json` per Qlik bookmark under
    `<report_dir>/definition/bookmarks/`. Returns a lookup — both the Qlik
    bookmark's own `id` and its casefolded `title` map to the generated PBI
    bookmark's `name` — for `report.py`'s button-action relocator to
    resolve a `Bookmark` action's `destination` against.

    `page_order`: the real, already-resolved list of PBIR page names (Qlik
    sheet ids are used verbatim as page names elsewhere in this build, see
    `report.py`/`_load_pages`) — a bookmark's own `sheet_id` is matched
    against this list directly, no separate id-translation table needed.
    Falls back to `page_order[0]` when a bookmark's sheet_id is missing or
    doesn't match any real page (e.g. the sheet was skipped/renamed)."""
    bookmarks_dir = os.path.join(report_dir, "definition", "bookmarks")
    # Wipe first, same reasoning as report.py's pages/ regeneration: a
    # rebuild that renames/drops a bookmark would otherwise leave the OLD
    # file sitting alongside the new ones, and stale bookmarks accumulating
    # across repeated builds is exactly the kind of drift this project has
    # already hit once with stale visual folders.
    if os.path.isdir(bookmarks_dir):
        shutil.rmtree(bookmarks_dir)
    if not bookmarks or not page_order:
        return {}
    os.makedirs(bookmarks_dir, exist_ok=True)

    lookup: dict[str, str] = {}
    taken: set[str] = set()
    written = 0
    index_names: list[str] = []
    for bm in bookmarks:
        title = bm.get("title") or bm.get("id") or "Bookmark"
        raw_id = bm.get("id") or title
        pbi_name = _bounded_bookmark_name(raw_id, taken)

        active_page = bm.get("sheet_id") if bm.get("sheet_id") in page_order else page_order[0]

        # Per-visual hide state (verified against the real Fabric bookmark
        # schema this session — SectionState.visualContainers.<name>.
        # singleVisual.display.mode, with "hidden" as one of exactly 4
        # allowed values: maximize/spotlight/elevation/hidden). Used for
        # container-tab emulation (see project.py's
        # _generate_container_tab_bookmarks): a tab's bookmark hides every
        # OTHER child in the same Qlik container, leaving only its own
        # child visible — the closest faithful reproduction of Qlik's
        # single-active-tab container behavior Power BI's bookmark
        # mechanism supports. A visual not listed here is left at its
        # normal (visible) state — there is no explicit "visible" mode,
        # only "hidden", so simply omitting a visual is how it stays shown.
        visual_containers = {
            name: {"singleVisual": {"display": {"mode": "hidden"}}}
            for name in bm.get("hidden_visuals", [])
        }

        bookmark_json = {
            "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/bookmark/1.0.0/schema.json",
            "name": pbi_name,
            "displayName": title,
            "explorationState": {
                "version": "1.0",
                "activeSection": active_page,
                "sections": {
                    active_page: {"visualContainers": visual_containers},
                },
            },
        }
        with open(os.path.join(bookmarks_dir, f"{pbi_name}.bookmark.json"), "w", encoding="utf-8") as f:
            json.dump(bookmark_json, f, indent=2)
        written += 1
        index_names.append(pbi_name)

        if bm.get("id"):
            lookup[bm["id"].casefold()] = pbi_name
        lookup[title.casefold()] = pbi_name

        if bm.get("selections"):
            fields = ", ".join(f"{s['field']}={s['values']}" for s in bm["selections"])
            print(f"[build] NOTE: bookmark '{title}' navigates to the correct page but its captured "
                  f"Qlik selections ({fields}) are NOT yet applied as Power BI filter/slicer state — "
                  f"PARTIALLY SUPPORTED: set the matching slicer values manually in Power BI Desktop "
                  f"and re-save this bookmark if exact filter-state parity is needed.")

    if written:
        # Confirmed real bug (user report): Power BI Desktop refuses to
        # open the project at all without this index file ("Cannot find
        # file 'bookmarks/bookmarks.json'") — it is NOT optional the way
        # this module's docstring originally (incorrectly) speculated.
        # Schema verified against the real Fabric bookmarksMetadata schema:
        # {"$schema": ..., "items": [{"name": "<bookmark name>"}, ...]} —
        # a flat list is sufficient (grouping via BookmarkGroupMetadata is
        # optional and not needed here).
        index_json = {
            "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/bookmarksMetadata/1.0.0/schema.json",
            "items": [{"name": name} for name in index_names],
        }
        with open(os.path.join(bookmarks_dir, "bookmarks.json"), "w", encoding="utf-8") as f:
            json.dump(index_json, f, indent=2)
        print(f"[build] wrote {written} bookmark(s) + bookmarks.json index -> {bookmarks_dir}")
    return lookup
