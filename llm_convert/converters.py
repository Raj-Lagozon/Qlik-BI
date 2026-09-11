"""Runs each extracted artifact through its matching skill.md via Azure OpenAI
and writes the converted (M / DAX / TMDL-fragment / PBIR JSON / RLS) output
under converted/<app_name>/."""

from __future__ import annotations

import json
import os
import re

from .azure_client import run_skill

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILLS_DIR = os.path.join(ROOT, "skills")
EXTRACTED_ROOT = os.path.join(ROOT, "extracted")
CONVERTED_ROOT = os.path.join(ROOT, "converted")


def _load_skill(filename: str) -> str:
    with open(os.path.join(SKILLS_DIR, filename), encoding="utf-8") as f:
        return f.read()


def _load_extracted(app_name: str, filename: str):
    path = os.path.join(EXTRACTED_ROOT, app_name, filename)
    if filename.endswith(".json"):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    with open(path, encoding="utf-8") as f:
        return f.read()


def _write_converted(app_name: str, filename: str, data) -> str:
    out_dir = os.path.join(CONVERTED_ROOT, app_name)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, filename)
    with open(path, "w", encoding="utf-8") as f:
        if filename.endswith(".json"):
            json.dump(data, f, indent=2, ensure_ascii=False)
        else:
            f.write(data)
    print(f"[convert]   -> wrote converted/{app_name}/{filename}")
    return path


# Every skill's output items carry a "confidence": "high"|"medium"|"low"
# field (see each skill.md's Output section) — the LLM's own judgment of how
# certain it is about that one translation, separate from and in addition to
# the code-level checks in pbip_build (hallucinated entity names, unresolved
# measures, etc). Collected here across every conversion call so convert_app
# can print one batch summary — a low-confidence item might still build and
# render fine, so this is a review flag, not a build blocker.
_confidence_log: list[dict] = []


def _collect_confidence(source: str, items: list[dict]) -> None:
    for item in items:
        confidence = item.get("confidence")
        if confidence in ("medium", "low"):
            _confidence_log.append({
                "source": source,
                "name": item.get("name") or item.get("title") or "<unnamed>",
                "confidence": confidence,
                "notes": item.get("notes") or item.get("description") or "",
            })


def convert_app(app_name: str) -> dict:
    """Run every domain converter for one extracted app. Returns the paths of
    everything written under converted/<app_name>/."""
    _confidence_log.clear()
    written = {}

    print(f"[convert] === {app_name}: m-queries (per table) ===")
    written["m_queries"] = _convert_m_queries(app_name)
    print(f"[convert] === {app_name}: data model / relationships ===")
    written["data_model"] = _convert_data_model(app_name)
    print(f"[convert] === {app_name}: master measures ===")
    written["measures"] = _convert_measures(app_name)
    print(f"[convert] === {app_name}: master dimensions ===")
    written["dimensions"] = _convert_dimensions(app_name)
    print(f"[convert] === {app_name}: variables / parameters ===")
    written["parameters"] = _convert_parameters(app_name)
    print(f"[convert] === {app_name}: report sheets/visuals ===")
    written["report"] = _convert_report(app_name)
    print(f"[convert] === {app_name}: section access / RLS ===")
    written["rls"] = _convert_rls(app_name)
    print(f"[convert] === {app_name}: KPI containers ===")
    written["kpi_containers"] = _convert_kpi_containers(app_name)
    print(f"[convert] === {app_name}: ad-hoc chart expressions ===")
    written["adhoc_expressions"] = _convert_adhoc_expressions(app_name)

    _print_confidence_summary()
    return written


def _print_confidence_summary() -> None:
    if not _confidence_log:
        print("[convert] all converted items reported high confidence")
        return
    low = [x for x in _confidence_log if x["confidence"] == "low"]
    medium = [x for x in _confidence_log if x["confidence"] == "medium"]
    print(f"[convert] confidence flags: {len(medium)} medium, {len(low)} low — review before trusting the build")
    for item in low + medium:
        note = f" — {item['notes']}" if item["notes"] else ""
        print(f"[convert]   [{item['confidence'].upper()}] {item['source']}: '{item['name']}'{note}")


def _convert_m_queries(app_name: str) -> list[str]:
    skill = _load_skill("m_query.skill.md")
    script = _load_extracted(app_name, "script.qvs")
    data_model = _load_extracted(app_name, "data_model.json")

    paths = []
    for table in data_model.get("tables", []):
        table_name = table.get("qName") or table.get("name")
        if not table_name:
            continue
        payload = {"table_name": table_name, "script": script, "data_model_table": table}
        print(f"[convert] table '{table_name}' (script.qvs, data_model.json) -> m_query.skill.md")
        m_code = run_skill(skill, payload, json_output=False)
        m_code = _strip_code_fence(m_code)
        m_code = _extract_m_confidence_comment(table_name, m_code)
        paths.append(_write_converted(app_name, f"m_query__{_safe(table_name)}.m", m_code))
    return paths


_M_CONFIDENCE_RE = re.compile(r"^\s*//\s*CONFIDENCE:\s*(low|medium)\s*-\s*(.+?)\s*\n", re.IGNORECASE)


def _extract_m_confidence_comment(table_name: str, m_code: str) -> str:
    """m_query.skill.md's output is raw M text (no JSON wrapper — the
    contract is 'just the M code, ready to paste'), so it can't carry a
    structured confidence field the way every other skill's JSON output
    does. Instead the skill is asked to prepend a `// CONFIDENCE: low -
    reason` comment line only when uncertain; parse that out here (feeding
    it into the same batch summary as everything else) and strip it before
    writing the .m file, since a leading comment there would otherwise
    become part of the TMDL partition source."""
    match = _M_CONFIDENCE_RE.match(m_code)
    if not match:
        return m_code
    confidence, reason = match.group(1).lower(), match.group(2)
    _collect_confidence("m_query", [{"name": table_name, "confidence": confidence, "notes": reason}])
    return m_code[match.end():]


def _convert_data_model(app_name: str) -> str:
    skill = _load_skill("data_model.skill.md")
    data_model = _load_extracted(app_name, "data_model.json")
    print("[convert] data_model.json -> data_model.skill.md")
    result = run_skill(skill, data_model, json_output=True)
    _collect_confidence("data_model", result.get("relationships", []))
    return _write_converted(app_name, "data_model.converted.json", result)


def _convert_measures(app_name: str) -> str | None:
    measures = _load_extracted(app_name, "measures.json")
    if not measures:
        print("[convert] no master measures in this app — skipping dax_measures LLM call")
        return None
    skill = _load_skill("dax_measures.skill.md")
    data_model = _load_extracted(app_name, "data_model.json")
    print(f"[convert] measures.json ({len(measures)} master measures) -> dax_measures.skill.md")
    result = run_skill(skill, {"measures": measures, "data_model": data_model}, json_output=True)
    _collect_confidence("dax_measures", result.get("measures", []))
    return _write_converted(app_name, "measures.converted.json", result)


def _convert_dimensions(app_name: str) -> str | None:
    dimensions = _load_extracted(app_name, "dimensions.json")
    if not dimensions:
        print("[convert] no master dimensions in this app — skipping dax_columns_hierarchies LLM call")
        return None
    skill = _load_skill("dax_columns_hierarchies.skill.md")
    data_model = _load_extracted(app_name, "data_model.json")
    print(f"[convert] dimensions.json ({len(dimensions)} master dimensions) -> dax_columns_hierarchies.skill.md")
    result = run_skill(skill, {"dimensions": dimensions, "data_model": data_model}, json_output=True)
    _collect_confidence("dax_columns_hierarchies", result.get("items", []))
    return _write_converted(app_name, "dimensions.converted.json", result)


def _convert_parameters(app_name: str) -> str | None:
    variables = _load_extracted(app_name, "variables.json")
    if not variables:
        print("[convert] no variables in this app — skipping parameters_variables LLM call")
        return None
    skill = _load_skill("parameters_variables.skill.md")
    script = _load_extracted(app_name, "script.qvs")
    print(f"[convert] variables.json ({len(variables)} variables), script.qvs -> parameters_variables.skill.md")
    result = run_skill(skill, {"variables": variables, "script": script}, json_output=True)
    _collect_confidence("parameters_variables", result.get("variables", []))
    return _write_converted(app_name, "variables.converted.json", result)


def _convert_report(app_name: str) -> list[str]:
    skill = _load_skill("report_visuals.skill.md")
    sheets = _load_extracted(app_name, "sheets.json")
    measures = _load_extracted(app_name, "measures.json")
    dimensions = _load_extracted(app_name, "dimensions.json")

    paths = []
    for sheet in sheets:
        payload = {"sheet": sheet, "measures": measures, "dimensions": dimensions}
        sheet_id = sheet.get("id") or _safe(sheet.get("title", "sheet"))
        print(f"[convert] sheet '{sheet.get('title', sheet_id)}' (sheets.json) -> report_visuals.skill.md")
        result = run_skill(skill, payload, json_output=True)
        _collect_confidence("report_visuals", result.get("visuals", []))
        paths.append(_write_converted(app_name, f"page__{_safe(sheet_id)}.json", result))
    return paths


def _convert_rls(app_name: str) -> str | None:
    section_access = _load_extracted(app_name, "section_access.json")
    if not section_access.get("present"):
        print("[convert] no Section Access in source script — skipping rls_section_access LLM call")
        return _write_converted(app_name, "rls.converted.json",
                                 {"roles": [], "notes": ["No Section Access in source script — no RLS to migrate."]})
    skill = _load_skill("rls_section_access.skill.md")
    data_model = _load_extracted(app_name, "data_model.json")
    print("[convert] section_access.json -> rls_section_access.skill.md")
    result = run_skill(skill, {"section_access": section_access, "data_model": data_model}, json_output=True)
    _collect_confidence("rls_section_access", result.get("roles", []))
    return _write_converted(app_name, "rls.converted.json", result)


def _convert_kpi_containers(app_name: str) -> list[str]:
    kpi_path = os.path.join(EXTRACTED_ROOT, app_name, "kpi_containers.json")
    if not os.path.exists(kpi_path):
        return []
    with open(kpi_path, encoding="utf-8") as f:
        containers = json.load(f)
    if not containers:
        return []

    skill = _load_skill("kpi_container.skill.md")
    measures = _load_extracted(app_name, "measures.json")
    variables = _load_extracted(app_name, "variables.json")

    paths = []
    for container in containers:
        payload = {
            "table": container["table"],
            "fields": container["fields"],
            "rows": container["rows"],
            "measures": measures,
            "variables": variables,
        }
        print(f"[convert] kpi_containers.json (table '{container['table']}') -> kpi_container.skill.md")
        result = run_skill(skill, payload, json_output=True)
        _collect_confidence("kpi_container", result.get("kpis", []))
        paths.append(_write_converted(app_name, f"kpi_container__{_safe(container['table'])}.json", result))
    return paths


def _collect_adhoc_expressions(app_name: str) -> tuple[dict[str, str], dict[str, str]]:
    """Find measure/dimension expressions used directly inside a chart's own
    qHyperCubeDef that aren't backed by any real master measure/dimension —
    a chart author can type a Sum(...)/If(...) expression straight into an
    object instead of picking a library item. The report_visuals conversion
    only sees that object's qLabel/qFieldLabels text (e.g. "MeasureValue"),
    which isn't a real field or measure name, so binding to it directly
    fails ("fields that need to be fixed") — these need their own DAX
    conversion first, the same way an actual master measure/dimension does."""
    sheets = _load_extracted(app_name, "sheets.json")
    measures = _load_extracted(app_name, "measures.json")
    dimensions = _load_extracted(app_name, "dimensions.json")
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
        # either is what report_visuals actually bound the visual to (it
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
        # a secondary value in the same tile) — but report_visuals only
        # ever projects ONE field reference for the object, using the bare
        # title with no disambiguating suffix (it documents this itself:
        # "only primary value projected here"). Matching that exactly,
        # register the FIRST blank-label measure under the bare title
        # (setdefault — first wins, i.e. the primary value) rather than
        # suffixing every one of them, since a suffixed name would never
        # match what report_visuals actually bound.
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
            # Still no usable label: report_visuals falls back to binding
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
                label = labels[0] if labels else None
                if label and label.casefold() not in known_dim_titles:
                    adhoc_dims.setdefault(label, field_defs[0])

    def walk(node):
        if isinstance(node, dict):
            layout = node.get("layout")
            if isinstance(layout, dict) and isinstance(layout.get("properties"), dict):
                # This dict is a sheet object entry (see qlik_extract.
                # extractor._get_object_layout) — properties and the
                # EVALUATED layout (qFallbackTitle etc.) are siblings here,
                # not reachable from each other once the walk descends past
                # this point, so both have to be read together right now.
                process_object(layout["properties"], layout.get("layout", {}))
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(sheets)
    return adhoc_measures, adhoc_dims


def _convert_adhoc_expressions(app_name: str) -> str | None:
    adhoc_measures, adhoc_dims = _collect_adhoc_expressions(app_name)
    if not adhoc_measures and not adhoc_dims:
        return None

    data_model = _load_extracted(app_name, "data_model.json")
    result: dict = {"measures": [], "items": []}

    if adhoc_measures:
        skill = _load_skill("dax_measures.skill.md")
        items = list(adhoc_measures.items())
        payload = [
            {"title": label, "expression": expr, "label_expression": None, "tags": []}
            for label, expr in items
        ]
        print(f"[convert] {len(payload)} ad-hoc chart measure expression(s) (sheets.json) -> dax_measures.skill.md")
        r = run_skill(skill, {"measures": payload, "data_model": data_model}, json_output=True)
        result["measures"] = _attach_qlik_source(r.get("measures", []), items)
        _collect_confidence("adhoc_expressions (measure)", result["measures"])

    if adhoc_dims:
        skill = _load_skill("dax_columns_hierarchies.skill.md")
        items = list(adhoc_dims.items())
        payload = [
            {"title": label, "grouping": "N", "field_defs": [expr], "field_labels": [label]}
            for label, expr in items
        ]
        print(f"[convert] {len(payload)} ad-hoc chart dimension expression(s) (sheets.json) -> dax_columns_hierarchies.skill.md")
        r = run_skill(skill, {"dimensions": payload, "data_model": data_model}, json_output=True)
        result["items"] = _attach_qlik_source(r.get("items", []), items)
        _collect_confidence("adhoc_expressions (dimension)", result["items"])

    return _write_converted(app_name, "adhoc_expressions.converted.json", result)


def _attach_qlik_source(converted: list[dict], sent: list[tuple[str, str]]) -> list[dict]:
    """Record the ORIGINAL raw Qlik expression on each converted ad-hoc
    item as `qlik_source`. report_visuals and this ad-hoc pass are separate
    LLM calls over the same chart object and don't always agree on which
    text to use as the field's identifier — report_visuals often binds the
    visual to the raw Qlik expression text, while this pass names the
    converted measure/column after the qLabel. Carrying the raw source
    through lets pbip_build register it as an alias, so a visual still
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


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    return text.strip()
