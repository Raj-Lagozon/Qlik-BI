"""Sheets -> PBIR pages/visuals, plus ad-hoc (not-library-backed) chart
expressions (sheets_convert.skill.md, task=report / measures / dimensions)."""

from __future__ import annotations

import re

from app.utilities.llm import collect_confidence, load_extracted, load_skill, run_skill, safe, write_converted

_SKILL_FILE = "sheets_convert.skill.md"

# A sheet with a lot of objects (very common now that a Qlik "KPI
# container" gets expanded into one discrete object per child tile —
# 30-40+ objects on one sheet is normal) puts a payload in front of the LLM
# that's too big for it to reliably enumerate: it silently converts only
# the first handful of objects and drops the rest, with no error — a page
# that should have ~40 real visuals comes back with 2. Splitting a large
# sheet into several smaller calls (each getting the sheet's own metadata
# but only a slice of its objects) and merging the results keeps every
# individual call small enough to actually finish converting what it was
# given.
_MAX_OBJECTS_PER_REPORT_CALL = 12


def convert_report(app_name: str) -> list[str]:
    skill = load_skill(_SKILL_FILE)
    sheets = load_extracted(app_name, "sheets.json")
    measures = load_extracted(app_name, "measures.json")
    dimensions = load_extracted(app_name, "dimensions.json")
    # A chart/table object frequently binds a plain FIELD directly (never
    # promoted to a master measure/dimension) — e.g. a straight table's
    # "CustomerName"/"Open Dispute Value" columns. Without the real table/
    # field list, the LLM has no way to resolve those bindings and has to
    # leave the projection empty ("Data-model tables not provided") — which
    # is exactly what silently produced a totally empty tableEx and a
    # Category-less, single-bar chart in practice. Always give it the real
    # data model alongside measures/dimensions.
    data_model = load_extracted(app_name, "data_model.json")

    paths = []
    for sheet in sheets:
        sheet_id = sheet.get("id") or safe(sheet.get("title", "sheet"))
        title = sheet.get("title", sheet_id)
        objects = sheet.get("objects", [])
        if len(objects) > _MAX_OBJECTS_PER_REPORT_CALL:
            batches = [objects[i:i + _MAX_OBJECTS_PER_REPORT_CALL]
                       for i in range(0, len(objects), _MAX_OBJECTS_PER_REPORT_CALL)]
            print(f"[convert] sheet '{title}' has {len(objects)} objects — splitting into {len(batches)} "
                  f"batches of <= {_MAX_OBJECTS_PER_REPORT_CALL} so the LLM can't silently truncate/drop "
                  f"objects on one oversized call")
        else:
            batches = [objects]

        merged_page, merged_visuals = None, []
        for i, batch in enumerate(batches):
            payload = {
                "task": "report",
                "sheet": {**sheet, "objects": batch},
                "measures": measures,
                "dimensions": dimensions,
                "data_model": data_model,
            }
            label = f"sheet '{title}'" + (f" (batch {i + 1}/{len(batches)})" if len(batches) > 1 else "")
            print(f"[convert] {label} (sheets.json) -> sheets_convert.skill.md (report)")
            result = run_skill(skill, payload, json_output=True)
            if merged_page is None:
                merged_page = result.get("page", {})
            merged_visuals.extend(result.get("visuals", []))

        result = {"page": merged_page or {}, "visuals": merged_visuals}
        collect_confidence("report_visuals", result["visuals"])
        paths.append(write_converted(app_name, f"page__{safe(sheet_id)}.json", result))
    return paths


def _collect_adhoc_expressions(app_name: str) -> tuple[dict[str, str], dict[str, str]]:
    """Find measure/dimension expressions used directly inside a chart's own
    qHyperCubeDef that aren't backed by any real master measure/dimension —
    a chart author can type a Sum(...)/If(...) expression straight into an
    object instead of picking a library item. The report conversion
    only sees that object's qLabel/qFieldLabels text (e.g. "MeasureValue"),
    which isn't a real field or measure name, so binding to it directly
    fails ("fields that need to be fixed") — these need their own DAX
    conversion first, the same way an actual master measure/dimension does."""
    sheets = load_extracted(app_name, "sheets.json")
    measures = load_extracted(app_name, "measures.json")
    dimensions = load_extracted(app_name, "dimensions.json")
    known_measure_titles = {m["title"].casefold() for m in measures if m.get("title")}
    known_dim_titles = {d["title"].casefold() for d in dimensions if d.get("title")}

    adhoc_measures: dict[str, str] = {}
    adhoc_dims: dict[str, str] = {}

    def blank(s) -> bool:
        return not (isinstance(s, str) and s.strip() and s.strip() != "\n")

    # Only fall back to "use the raw expression as its own label" for an
    # expression that's actually an AGGREGATING measure the LLM couldn't
    # name — not a caption/label expression (=chr(10), ='Off'), not a bare
    # passthrough to an existing measure (=[DSO]), not a string literal.
    _agg_call = re.compile(
        r"\b(sum|count|avg|average|min|max|aggr|only|median|mode|"
        r"rangesum|rangeavg|firstsortedvalue|nummin|nummax)\s*\(", re.I)

    def is_real_measure_expr(expr: str) -> bool:
        body = expr.strip().lstrip("=").strip()
        if not body or re.fullmatch(r"\[[^\[\]]+\]", body):  # just "[Existing Measure]"
            return False
        if re.fullmatch(r"""['"].*['"]""", body):  # just a string literal
            return False
        return bool(_agg_call.search(body))

    def looks_like_expr(s: str) -> bool:
        """A qLabel that is really the raw Qlik expression text (Qlik
        defaults it to that when the author sets no label) — it must not be
        used as the measure name: its {}/</>/$/newline characters land in
        TMDL as `measure '<name>'` and break the parser."""
        if not isinstance(s, str):
            return False
        if any(ch in s for ch in "{}<>$\n\r"):
            return True
        return "(" in s and bool(_agg_call.search(s))

    _name_skip = {w.lower() for w in (
        "sum", "count", "avg", "average", "min", "max", "aggr", "only", "median",
        "mode", "rangesum", "rangeavg", "firstsortedvalue", "nummin", "nummax",
        "distinct", "total", "if", "match", "pick", "num", "chr", "date", "floor")}

    def derive_measure_name(expr: str) -> str:
        """A readable, deterministic name for a chart measure with no
        usable label — "Sum of PrimarySalesValue", "Count of InvoiceID".
        The raw expression itself must NOT be used as the name (it lands in
        TMDL as `measure '<name>'` and its {}/</>/$ characters break the
        parser); the raw text is still carried as `qlik_source` so the
        visual binding resolves regardless of what we name the measure."""
        body = expr.strip().lstrip("=").strip()
        agg_m = _agg_call.search(body)
        agg = agg_m.group(1).title() if agg_m else "Value"
        idents = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", body)
        field = next((t for t in reversed(idents) if t.lower() not in _name_skip), None)
        return f"{agg} of {field}" if field else f"{agg} value"

    def derive_dimension_name(expr: str) -> str:
        """A readable, deterministic name for a chart DIMENSION with no
        usable label. Unlike a measure, a blank-labeled dimension is very
        often a `&`-concatenation of two or more real fields (a display
        label built from several columns, e.g.
        `CustomerID & ' - ' & CustomerName`) rather than an aggregation —
        `derive_measure_name`'s "Sum of X" phrasing doesn't fit that shape,
        so join the referenced field names instead ("CustomerID -
        CustomerName Label"); falls back to derive_measure_name's own
        phrasing for a non-concatenation expression (an `If()`/bucketing
        dimension with no `&` in it)."""
        body = expr.strip().lstrip("=").strip()
        if "&" in body:
            idents = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", body)
            fields = [t for t in idents if t.lower() not in _name_skip]
            if fields:
                return f"{' - '.join(dict.fromkeys(fields))} Label"
        return derive_measure_name(expr)

    def process_object(props: dict, eval_layout: dict) -> None:
        hc = props.get("qHyperCubeDef")
        if not isinstance(hc, dict):
            return
        qmeasures = hc.get("qMeasures") or []
        # The modern "kpi" extension (visualization: "kpi", version 2.x)
        # never sets a static qLabel on its measure at all: the display
        # name comes only from the KPI object's OWN "title" property, or —
        # when even THAT is blank — from GetLayout's resolved
        # qFallbackTitle (a live-evaluated snapshot, e.g. a dynamic
        # qLabelExpression like "='Projected Qtr Pool (' & Count(...) &
        # ' reps)'" baked to literal text at extraction time). Recovering
        # either is what the report task actually bound the visual to (it
        # has no other name to work with), so without registering an
        # ad-hoc measure under that exact text, nothing ever resolves it
        # and the KPI renders as "fields that need to be fixed". Scoped
        # strictly to visualization=="kpi" using its OWN title/fallback
        # (never any ancestor/sheet title) — a regular chart's blank
        # qLabel means something else entirely (its real fallback is the
        # expression text itself, resolved by Qlik at render time) and
        # must not be guessed at here.
        is_kpi = props.get("visualization") == "kpi"
        own_title = props.get("title") if isinstance(props.get("title"), str) else None
        eval_measure_info = (((eval_layout or {}).get("qHyperCube") or {}).get("qMeasureInfo")) or []
        # A KPI can carry TWO measures under one shared title (a primary +
        # a secondary value in the same tile) — but the report task only
        # ever projects ONE field reference for the object, using the bare
        # title with no disambiguating suffix (it documents this itself:
        # "only primary value projected here"). Matching that exactly,
        # register the FIRST blank-label measure under the bare title
        # (setdefault — first wins, i.e. the primary value) rather than
        # suffixing every one of them, since a suffixed name would never
        # match what the report task actually bound.
        def usable(s) -> str | None:
            # A label candidate is only usable if it's non-blank AND isn't
            # really the raw Qlik expression text (Qlik defaults qLabel /
            # qFallbackTitle to that when the author set no label). Strip a
            # symmetric pair of surrounding quotes too — Qlik wraps a
            # label-expression's literal in quotes ('FILTERS'), and those
            # quotes must not become part of the measure name.
            if blank(s) or looks_like_expr(s):
                return None
            s = s.strip()
            if len(s) >= 2 and s[0] == s[-1] and s[0] in "'\"":
                s = s[1:-1].strip() or s
            return s

        for i, m in enumerate(qmeasures):
            qdef = (m or {}).get("qDef") or {}
            expr = qdef.get("qDef")
            label = usable(qdef.get("qLabel"))
            if not label and is_kpi:
                label = usable(own_title)
            if not label and is_kpi and i < len(eval_measure_info):
                label = usable((eval_measure_info[i] or {}).get("qFallbackTitle"))
            # Still no usable label: the report task falls back to binding
            # the visual to the RAW expression text itself (its skill.md
            # tells it to, so a later pass like this one can pick it up).
            # Give the measure a readable DERIVED name — never the raw
            # expression text, whose {}/</>/$ chars land in TMDL as
            # `measure '<name>'` and break the parser. _attach_qlik_source
            # carries the raw text so the visual binding resolves
            # regardless of what we name it. Only for a genuine aggregating
            # expression — not a caption/label expr or a [Measure] passthrough.
            if not label and not blank(expr) and is_real_measure_expr(expr):
                label = derive_measure_name(expr)
            if label and expr and label.casefold() not in known_measure_titles:
                adhoc_measures.setdefault(label, expr)
        for d in hc.get("qDimensions") or []:
            qdef = (d or {}).get("qDef") or {}
            field_defs = qdef.get("qFieldDefs") or []
            labels = qdef.get("qFieldLabels") or []
            if field_defs and str(field_defs[0]).startswith("="):
                expr = field_defs[0]
                label = usable(labels[0]) if labels else None
                # A blank/empty qFieldLabels entry (`['']`, not missing —
                # seen on a scatter chart's own dimension, real Qlik data)
                # used to silently drop this ad-hoc dimension entirely: an
                # empty string is falsy, so it never got registered here,
                # never got a real calculated column from Task B/the
                # ad-hoc pipeline, and the report conversion was left to
                # invent a plausible-sounding column name on its own with
                # nothing backing it (observed: "CustomerDim[Customer
                # Label]", claimed created "in Task B" but never actually
                # created anywhere — Power BI's "fields that need to be
                # fixed" on that exact name). Derive a readable name from
                # the expression the same way a blank-labeled ad-hoc
                # MEASURE already does, instead of dropping it.
                if not label and not blank(expr):
                    label = derive_dimension_name(expr)
                if label and label.casefold() not in known_dim_titles:
                    adhoc_dims.setdefault(label, expr)

    def walk(node):
        if isinstance(node, dict):
            layout = node.get("layout")
            if isinstance(layout, dict) and isinstance(layout.get("properties"), dict):
                # This dict is a sheet object entry (see
                # modules.extract.extractor._get_object_layout) —
                # properties and the EVALUATED layout (qFallbackTitle etc.)
                # are siblings here, not reachable from each other once the
                # walk descends past this point, so both have to be read
                # together right now.
                process_object(layout["properties"], layout.get("layout", {}))
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(sheets)
    return adhoc_measures, adhoc_dims


def convert_adhoc_expressions(app_name: str) -> str | None:
    adhoc_measures, adhoc_dims = _collect_adhoc_expressions(app_name)
    if not adhoc_measures and not adhoc_dims:
        return None

    skill = load_skill(_SKILL_FILE)
    data_model = load_extracted(app_name, "data_model.json")
    result: dict = {"measures": [], "items": []}

    if adhoc_measures:
        items = list(adhoc_measures.items())
        payload = [
            {"title": label, "expression": expr, "label_expression": None, "tags": []}
            for label, expr in items
        ]
        print(f"[convert] {len(payload)} ad-hoc chart measure expression(s) (sheets.json) -> sheets_convert.skill.md (measures)")
        r = run_skill(skill, {"task": "measures", "measures": payload, "data_model": data_model}, json_output=True)
        result["measures"] = _attach_qlik_source(r.get("measures", []), items)
        collect_confidence("adhoc_expressions (measure)", result["measures"])

    if adhoc_dims:
        items = list(adhoc_dims.items())
        payload = [
            {"title": label, "grouping": "N", "field_defs": [expr], "field_labels": [label]}
            for label, expr in items
        ]
        print(f"[convert] {len(payload)} ad-hoc chart dimension expression(s) (sheets.json) -> sheets_convert.skill.md (dimensions)")
        r = run_skill(skill, {"task": "dimensions", "dimensions": payload, "data_model": data_model}, json_output=True)
        result["items"] = _attach_qlik_source(r.get("items", []), items)
        collect_confidence("adhoc_expressions (dimension)", result["items"])

    return write_converted(app_name, "adhoc_expressions.converted.json", result)


def _attach_qlik_source(converted: list[dict], sent: list[tuple[str, str]]) -> list[dict]:
    """Record the ORIGINAL raw Qlik expression on each converted ad-hoc
    item as `qlik_source`. The report task and this ad-hoc pass are separate
    LLM calls over the same chart object and don't always agree on which
    text to use as the field's identifier — the report task often binds the
    visual to the raw Qlik expression text, while this pass names the
    converted measure/column after the qLabel. Carrying the raw source
    through lets the builder register it as an alias, so a visual still
    pointing at "Sum(OutstandingAmount)" resolves to the real measure that
    was synthesized from exactly that expression."""
    by_name = {label: raw for label, raw in sent}
    for idx, item in enumerate(converted):
        src = by_name.get(item.get("name"))
        if src is None and idx < len(sent):
            src = sent[idx][1]  # positional fallback — skill preserves order
        if src is not None:
            item["qlik_source"] = src
    return converted
