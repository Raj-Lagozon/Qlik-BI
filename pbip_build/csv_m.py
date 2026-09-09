"""Generate a Power Query (M) partition source that loads a table's real,
extracted data from a local CSV — instead of trying to reconstruct the
Qlik app's original (often unreachable) source connection/file path.

Numbers and dates are written to CSV as plain en-US-formatted values by
qlik_extract, so parsing here is pinned to "en-US" regardless of the
machine's regional settings, avoiding decimal-separator/date-format
ambiguity entirely.
"""

from __future__ import annotations

import os

_TYPE_LITERAL = {"int64": "Int64.Type", "double": "type number"}


def generate_csv_partition_m(csv_path: str, columns: list[dict]) -> str:
    abs_path = os.path.abspath(csv_path).replace('"', '""')

    statements: list[str] = [
        f'Source = Csv.Document(File.Contents("{abs_path}"), '
        f'[Delimiter=",", Columns={len(columns)}, Encoding=65001, QuoteStyle=QuoteStyle.Csv])',
        "Promoted = Table.PromoteHeaders(Source, [PromoteAllScalars=true])",
    ]
    text_cast = ", ".join(f'{{"{c["name"]}", type text}}' for c in columns)
    statements.append(f"AsText = Table.TransformColumnTypes(Promoted, {{{text_cast}}})")
    step = "AsText"

    date_cols = [c["name"] for c in columns if c.get("data_type") == "dateTime"]
    if date_cols:
        transforms = ", ".join(
            f'{{"{name}", each if _ = null or _ = "" then null else '
            f'#date(1899, 12, 30) + #duration(Number.FromText(_, "en-US"), 0, 0, 0), type date}}'
            for name in date_cols
        )
        statements.append(f"DatesFixed = Table.TransformColumns({step}, {{{transforms}}})")
        step = "DatesFixed"

    numeric_cols = [c for c in columns if c.get("data_type") in ("int64", "double") and c["name"] not in date_cols]
    if numeric_cols:
        transforms = []
        for c in numeric_cols:
            target_type = _TYPE_LITERAL[c["data_type"]]
            wrap = "Int64.From" if c["data_type"] == "int64" else ""
            expr = 'Number.FromText(_, "en-US")'
            if wrap:
                expr = f"{wrap}({expr})"
            transforms.append(
                f'{{"{c["name"]}", each if _ = null or _ = "" then null else {expr}, {target_type}}}'
            )
        statements.append(f"NumbersFixed = Table.TransformColumns({step}, {{{', '.join(transforms)}}})")
        step = "NumbersFixed"

    boolean_cols = [c["name"] for c in columns if c.get("data_type") == "boolean"]
    if boolean_cols:
        transforms = ", ".join(
            f'{{"{name}", each _ = "true" or _ = "1" or _ = "-1", type logical}}'
            for name in boolean_cols
        )
        statements.append(f"BoolsFixed = Table.TransformColumns({step}, {{{transforms}}})")
        step = "BoolsFixed"

    body = ",\n    ".join(statements)
    return f"let\n    {body}\nin\n    {step}"
