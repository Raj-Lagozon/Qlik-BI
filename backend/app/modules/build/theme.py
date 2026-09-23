"""Converts the Qlik color palette extracted into theme.json (see
extract/extractor.py's _get_theme) into a Power BI custom Report Theme
JSON — report.py wires the result into the PBIR project.
"""

from __future__ import annotations

import json
import os

# Power BI's own theme editor shows 8-10 "data colors" as the primary
# palette slots; more than that just cycles back to the start, so there's
# no benefit keeping every hex code an app happened to define as a
# variable (some apps define a couple dozen one-off colors for specific
# KPI backgrounds/borders that were never meant as a chart-series palette).
_MAX_DATA_COLORS = 10

# Power BI's own default "Classic" theme accents — used only to PAD a real
# but too-short Qlik palette (one or two accent colors alone isn't a full
# chart palette) with visually distinct, professionally-chosen colors,
# never used standalone with zero real Qlik colors (see build_custom_theme
# — an app with no discoverable color at all gets no custom theme, not a
# generic Power BI palette pretending to be "its" theme).
_FALLBACK_PALETTE = [
    "#118DFF", "#12239E", "#E66C37", "#6B007B", "#E044A7",
    "#744EC2", "#D9B300", "#D64550",
]


def load_theme(extracted_dir: str) -> dict:
    """{"app_theme_name": str | None, "colors": [...]} as written by
    extraction, or the empty/default shape for an app extracted before
    theme.json existed (older extracted/ folders) — never an error, since
    a report is always still buildable with no custom theme at all."""
    path = os.path.join(extracted_dir, "theme.json")
    if not os.path.exists(path):
        return {"app_theme_name": None, "colors": []}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def build_custom_theme(app_name: str, colors: list[str]) -> dict | None:
    """Returns a Power BI custom Report Theme JSON built from the app's own
    extracted color palette, or None when there's no real color to build
    one from at all — callers fall back to the plain default base theme in
    that case (see report.py), rather than shipping a theme that isn't
    actually the source app's.

    A palette with only one or two real colors gets padded with Power BI's
    own default accent colors (see _FALLBACK_PALETTE) so charts with more
    series than the app had distinct brand colors still render with
    visually distinct series — the app's own color(s) always come first/
    most prominent."""
    if not colors:
        return None
    palette = list(dict.fromkeys(colors))[:_MAX_DATA_COLORS]
    if len(palette) < 2:
        for c in _FALLBACK_PALETTE:
            if c not in palette:
                palette.append(c)
            if len(palette) >= 6:
                break
    return {
        "name": f"{app_name} Theme",
        "dataColors": palette,
    }
