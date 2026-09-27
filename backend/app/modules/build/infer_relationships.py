"""Derive table relationships deterministically — never trusting the LLM's
own re-derivation of Qlik's associative model as the primary source (see
project.py's `_merge_relationships`, which already prefers this module's
output over the LLM's `data_model.converted.json` for any exact table-pair
collision).

PRIMARY source: Qlik's own extracted field/key metadata in
`data_model.json` (`tables[].qFields[].qKeyType`/`qnTotalDistinctValues`/
`qnRows`, and the top-level `keys` list — the Engine API's own
`GetTablesAndKeys` output). This is Qlik's OWN associative engine telling
us, per field per table, whether that field is a real key there
(`qKeyType: "PERFECT_KEY"`/`"PRIMARY_KEY"`) — the most authoritative
signal available, and critically, ALWAYS extracted (part of the core
`_get_data_model` call), unlike raw CSV row data (`extracted/<app>/data/`),
which a real app has been found NOT to have at all (extraction stopped
pulling .qvf row data — see extractor.py) — meaning the OLDER, CSV-only
version of this module silently returned ZERO relationships for such an
app, leaving 100% of its relationships to the LLM's own (confirmed
run-to-run INCONSISTENT) guessing. A real, confirmed case: on one run the
LLM correctly related `CreditDim`/`DisputeCount90d` directly to
`CustomerDim` (their true shared-key peer, matching Qlik's own `keys`
grouping); on a later run it instead routed them through `ARFact` (the
fact table) — a materially different, less correct topology that broke
cross-filtering between the customer dimension and its related tables in
the actual generated report ("not getting charts and visual properly
because of relationship").

FALLBACK source: the original CSV-based column-name/uniqueness scan, kept
for any (table, field) pair Qlik's own metadata doesn't cover (an older
extraction predating data_model.json's current shape, or a field genuinely
missing from it) — deterministic and testable offline, same as before."""

from __future__ import annotations

import csv
import json
import os

MIN_UNIQUE_RATIO = 0.9  # a field must be "mostly unique" in at least one table to count as a key

# Qlik's own NxKeyType classification (Engine API enum) for a field WITHIN
# one specific table — PERFECT_KEY/PRIMARY_KEY both mean "this table's own
# copy of this field is effectively unique here", which is exactly the
# question MIN_UNIQUE_RATIO otherwise has to approximate from raw counts.
_QLIK_KEY_TYPES = {"PERFECT_KEY", "PRIMARY_KEY"}


def infer_relationships(tables: dict[str, dict], extracted_dir: str) -> list[dict]:
    """tables: {table_name: {"columns": [{"name": ..., ...}, ...], ...}}."""
    data_model = _load_data_model_json(extracted_dir)
    qlik_field_info = _index_qlik_fields(data_model)
    real_table_by_cf = {t.casefold(): t for t in tables}

    # Field-owner-table groups: PREFER Qlik's own "keys" list (the
    # associative engine's own grouping — more authoritative than
    # re-deriving it from column names, and immune to picking up a
    # coincidentally-shared, non-associated column name Qlik itself never
    # treated as a real key). Composite keys (>1 field) are out of scope
    # for this single-column relationship builder — those are already
    # handled separately via the AutoNumberHash/ConcatKey computed-column
    # detectors in project.py/csv_m.py.
    field_owner_tables: dict[str, list[str]] = {}
    covered_fields: set[str] = set()
    for key_group in data_model.get("keys", []):
        key_fields = key_group.get("qKeyFields", [])
        if len(key_fields) != 1:
            continue
        field_cf = key_fields[0].casefold()
        owners = [real_table_by_cf[t.casefold()] for t in key_group.get("qTables", []) if t.casefold() in real_table_by_cf]
        # De-dupe while preserving Qlik's own qTables order — that order
        # matters: it's used below as the tie-break when multiple tables
        # are equally unique on this field (see one_side selection).
        seen_owner: set[str] = set()
        owners = [t for t in owners if not (t in seen_owner or seen_owner.add(t))]
        if len(owners) >= 2:
            field_owner_tables[field_cf] = owners
            covered_fields.add(field_cf)

    # Fallback grouping (legacy behavior) for any field Qlik's own "keys"
    # list didn't cover — e.g. data_model.json predates the "keys" field,
    # or a genuinely shared column Qlik's engine didn't key-group.
    for table_name, table in tables.items():
        for col in table.get("columns", []):
            fcf = col["name"].casefold()
            if fcf in covered_fields:
                continue
            field_owner_tables.setdefault(fcf, [])
            if table_name not in field_owner_tables[fcf]:
                field_owner_tables[fcf].append(table_name)
    field_owner_tables = {f: ts for f, ts in field_owner_tables.items() if len(ts) >= 2}

    csv_stats_cache: dict[tuple[str, str], tuple[int, int]] = {}

    def stats(table_name: str, field_cf: str) -> tuple[int, int] | None:
        info = qlik_field_info.get((table_name.casefold(), field_cf))
        if info is not None and info.get("qKeyType") in _QLIK_KEY_TYPES:
            # Qlik's own engine already confirmed this field is
            # effectively a key in THIS table — treat as ratio 1.0
            # directly rather than trusting qnTotalDistinctValues (an
            # associative-model-wide count that can exceed this
            # table's own qnRows, e.g. a table whose key participates
            # in a GROUP BY/aggregate reduction elsewhere — seen in
            # practice: qnTotalDistinctValues=20 with qnRows=10 on a
            # real PRIMARY_KEY field).
            return (1, 1)
        # NOT a confirmed per-table key (qKeyType is ANY_KEY/NOT_KEY, or
        # missing entirely) — `qnTotalDistinctValues` must NEVER be trusted
        # here even capped by this table's own row count: it is an
        # ASSOCIATIVE-MODEL-WIDE distinct count (the same number reused
        # across every table sharing the field), which produces a falsely
        # high ratio for any SMALL table that merely happens to reference a
        # widely-shared key. Real, confirmed case: a 3-row `AlertLog` table
        # and a 720-row `LinkTable` both report `Key_ProductZone` with
        # qKeyType=ANY_KEY and the SAME qnTotalDistinctValues=30 (the
        # associative-wide count) — the old `min(distinct, rows)` shortcut
        # computed ratio 30/3 capped to 3/3 = 1.0 for AlertLog, wrongly
        # flagging it as a unique "one" side when it has real duplicate
        # values, which Analysis Services then rejected outright at
        # refresh ("contains a duplicate value ... not allowed for columns
        # on the one side of a many-to-one relationship"). Only a genuine
        # per-table scan (CSV row data, when available) is trustworthy here.
        return _csv_stats(extracted_dir, table_name, field_cf, csv_stats_cache)

    relationships: list[dict] = []
    seen_pairs: set[frozenset[str]] = set()

    for field_cf, owner_tables in field_owner_tables.items():
        # Score each owning table by how unique this field is there.
        candidates = []
        for t in owner_tables:
            s = stats(t, field_cf)
            if s is None or s[1] == 0:
                continue
            distinct, total = s
            ratio = distinct / total
            candidates.append((t, distinct, total, ratio))

        # Need at least one side where the field looks like a real key
        # (mostly unique) — otherwise this is probably a coincidental
        # shared column name (e.g. a free-text field), not a join key.
        keyish = [c for c in candidates if c[3] >= MIN_UNIQUE_RATIO]
        if not keyish:
            # Qlik's OWN associative engine may still tie these tables
            # together on this field (it's in the "keys" list) even though
            # neither table's copy is reliably unique per-table (both
            # ANY_KEY, or no CSV available to verify) — dropping the
            # relationship entirely would lose real cross-filtering the
            # source app has. Model it as many-to-many instead of picking
            # an arbitrary "one" side (which Analysis Services would reject
            # the moment that side turns out to have a real duplicate).
            if field_cf in covered_fields and len(owner_tables) >= 2:
                primary = owner_tables[0]
                for t in owner_tables[1:]:
                    pair_key = frozenset({t, primary, field_cf})
                    if pair_key in seen_pairs:
                        continue
                    seen_pairs.add(pair_key)
                    relationships.append({
                        "from_table": t,
                        "from_column": _real_field_name(tables, t, field_cf),
                        "to_table": primary,
                        "to_column": _real_field_name(tables, primary, field_cf),
                        "cardinality": "many-to-many",
                        "cross_filter": "both",
                    })
            continue

        # Pick the most key-like table as the "one" side; every other table
        # that also has this field becomes related to it. Ties (e.g. two
        # equally PERFECT_KEY tables) break on iteration order, which for
        # a Qlik-"keys"-sourced group is Qlik's OWN qTables order — in
        # practice Qlik lists the more central/hub-like table first (a
        # real customer-dimension case confirmed this: CustomerDim before
        # CreditDim/ARFact/DisputeCount90d in Qlik's own key group).
        one_side = max(keyish, key=lambda c: c[3])
        keyish_names = {c[0] for c in keyish}
        for t, distinct, total, ratio in candidates:
            if t == one_side[0]:
                continue
            pair_key = frozenset({t, one_side[0], field_cf})
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)
            real_field_many = _real_field_name(tables, t, field_cf)
            real_field_one = _real_field_name(tables, one_side[0], field_cf)

            # Genuine one-to-one, cross-filtering-both-ways case: BOTH sides
            # of this pair are highly unique on the shared field (i.e. this
            # is a dimension-to-dimension relationship on each side's own
            # key, not a fact table's foreign key into a dimension) AND
            # neither table is a DAX calculated table. The calculated-table
            # exclusion is load-bearing, not cosmetic — a statically
            # asserted 1:1 relationship into/out of a calculated table has
            # been CONFIRMED to fail Power BI Desktop's static project-load
            # validation outright ("Relationship '<guid>' uses an invalid
            # column ID"), independent of crossFilteringBehavior; that
            # failure has been seen twice in this project (a RELATED()-based
            # calculated table and a plain CALENDARAUTO() one), so it's
            # excluded here regardless of how unique its key looks.
            is_one_to_one = (
                t in keyish_names
                and not tables.get(t, {}).get("is_calculated")
                and not tables.get(one_side[0], {}).get("is_calculated")
            )
            if is_one_to_one:
                relationships.append({
                    "from_table": t,
                    "from_column": real_field_many,
                    "to_table": one_side[0],
                    "to_column": real_field_one,
                    "cardinality": "one-to-one",
                    "cross_filter": "both",
                })
                continue

            # Default: many-to-one, single-direction (t = many/fact side,
            # one_side = one/dimension side) — the safe, proven default for
            # every case that isn't a confirmed dimension-to-dimension pair
            # above. A real 1:1 between a fact-shaped table and anything
            # else is usually a sign the two tables should be merged, not
            # related as equals.
            relationships.append({
                "from_table": t,
                "from_column": real_field_many,
                "to_table": one_side[0],
                "to_column": real_field_one,
                "cardinality": "one-to-many",
                "cross_filter": "single",
            })

    return relationships


def _load_data_model_json(extracted_dir: str) -> dict:
    path = os.path.join(extracted_dir, "data_model.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _index_qlik_fields(data_model: dict) -> dict[tuple[str, str], dict]:
    """{(table_name_casefold, field_name_casefold): {"qKeyType", "rows", "distinct"}}
    from data_model.json's own `tables[].qFields[]` — Qlik's own per-field,
    per-table key classification and row/distinct counts, extracted
    directly from the Engine API (no CSV export dependency at all)."""
    out: dict[tuple[str, str], dict] = {}
    for t in data_model.get("tables", []):
        table_cf = (t.get("qName") or "").casefold()
        if not table_cf:
            continue
        rows = t.get("qNoOfRows") or 0
        for f in t.get("qFields", []):
            field_cf = (f.get("qName") or "").casefold()
            if not field_cf:
                continue
            out[(table_cf, field_cf)] = {
                "qKeyType": f.get("qKeyType"),
                "rows": rows,
                "distinct": f.get("qnTotalDistinctValues"),
            }
    return out


def _csv_stats(
    extracted_dir: str, table_name: str, field_cf: str, cache: dict[tuple[str, str], tuple[int, int]],
) -> tuple[int, int] | None:
    """Legacy fallback: read the real exported CSV (when one exists —
    `extracted/<app>/data/<table>.csv`) and count distinct/total for
    `field_cf` directly. Only reached for a (table, field) pair Qlik's own
    data_model.json metadata didn't cover at all."""
    key = (table_name, field_cf)
    if key in cache:
        return cache[key]
    csv_path = os.path.join(extracted_dir, "data", f"{_safe(table_name)}.csv")
    if not os.path.exists(csv_path):
        return None
    real_field = None
    seen = set()
    total = 0
    try:
        with open(csv_path, encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for fname in reader.fieldnames or []:
                if fname.casefold() == field_cf:
                    real_field = fname
                    break
            if real_field is None:
                return None
            for row in reader:
                val = row.get(real_field, "")
                if val != "":
                    seen.add(val)
                    total += 1
    except OSError:
        return None
    result = (len(seen), total)
    cache[key] = result
    return result


def _real_field_name(tables: dict[str, dict], table_name: str, field_casefold: str) -> str:
    for col in tables[table_name].get("columns", []):
        if col["name"].casefold() == field_casefold:
            return col["name"]
    return field_casefold


def _safe(name: str) -> str:
    import re
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)
