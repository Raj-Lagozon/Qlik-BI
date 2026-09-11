"""Derive table relationships directly from the real exported CSV data,
instead of relying solely on Qlik's GetTablesAndKeys association metadata
(whose exact parameter semantics/availability can vary by Qlik Cloud
version) or the LLM's read of it. Comparing actual column names + measuring
how unique each candidate key is per table is deterministic, testable
offline, and doesn't depend on any Engine API subtlety."""

from __future__ import annotations

import csv
import os

MIN_UNIQUE_RATIO = 0.9  # a field must be "mostly unique" in at least one table to count as a key


def infer_relationships(tables: dict[str, dict], extracted_dir: str) -> list[dict]:
    """tables: {table_name: {"columns": [{"name": ..., ...}, ...], ...}}."""
    data_dir = os.path.join(extracted_dir, "data")
    field_owner_tables: dict[str, list[str]] = {}
    for table_name, table in tables.items():
        for col in table.get("columns", []):
            field_owner_tables.setdefault(col["name"].casefold(), []).append(table_name)

    stats_cache: dict[tuple[str, str], tuple[int, int]] = {}  # (table, field_casefold) -> (distinct, total)

    def stats(table_name: str, field_casefold: str) -> tuple[int, int] | None:
        key = (table_name, field_casefold)
        if key in stats_cache:
            return stats_cache[key]
        csv_path = os.path.join(data_dir, f"{_safe(table_name)}.csv")
        if not os.path.exists(csv_path):
            return None
        real_field = None
        seen = set()
        total = 0
        try:
            with open(csv_path, encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                for fname in reader.fieldnames or []:
                    if fname.casefold() == field_casefold:
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
        stats_cache[key] = result
        return result

    relationships: list[dict] = []
    seen_pairs: set[frozenset[str]] = set()

    for field_cf, owner_tables in field_owner_tables.items():
        if len(owner_tables) < 2:
            continue
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
            continue

        # Pick the most key-like table as the "one" side; every other table
        # that also has this field becomes a "many" side related to it.
        one_side = max(keyish, key=lambda c: c[3])
        for t, distinct, total, ratio in candidates:
            if t == one_side[0]:
                continue
            pair_key = frozenset({t, one_side[0], field_cf})
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)
            real_field_many = _real_field_name(tables, t, field_cf)
            real_field_one = _real_field_name(tables, one_side[0], field_cf)
            # Only call it one-to-one when the "many" side's key is
            # *fully* unique too (not merely mostly-unique): a 1:1
            # cardinality that a later, larger refresh violates makes
            # Power BI reject the refresh outright, so it must be a
            # near-certainty, not a guess off a small sample. When it is
            # genuinely 1:1, RELATED() works in both directions, which is
            # what lets a measure iterate either table.
            one_to_one = ratio >= 0.999 and one_side[3] >= 0.999
            relationships.append({
                "from_table": t,
                "from_column": real_field_many,
                "to_table": one_side[0],
                "to_column": real_field_one,
                "cardinality": "one-to-one" if one_to_one else "one-to-many",
                "cross_filter": "single",
            })

    return relationships


def _real_field_name(tables: dict[str, dict], table_name: str, field_casefold: str) -> str:
    for col in tables[table_name].get("columns", []):
        if col["name"].casefold() == field_casefold:
            return col["name"]
    return field_casefold


def _safe(name: str) -> str:
    import re
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)
