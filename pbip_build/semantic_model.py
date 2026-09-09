"""Write a *.SemanticModel/ folder (TMDL) from the converted
data model / measures / dimensions / variables / RLS artifacts."""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

from .report import write_platform_file


def _guid() -> str:
    return str(uuid.uuid4())


def _indent_block(text: str, tabs: int = 3) -> str:
    prefix = "\t" * tabs
    return "\n".join(prefix + line if line.strip() else line for line in text.splitlines())


def _render_multiline_stmt(first_line_prefix: str, expression: str, cont_tabs: int) -> list[str]:
    """Render `<prefix><first line>` followed by continuation lines indented
    with `cont_tabs` leading tabs each.

    TMDL delimits a multi-line measure/column expression purely by
    indentation: a continuation line only counts as part of the expression if
    it has MORE leading tabs than the `measure`/`column` declaration line.
    An LLM-generated DAX expression is naturally formatted with spaces, not
    tabs — writing it through untouched silently truncates the expression at
    its first line the moment it's parsed back (by pbip-compiler, or by
    Power BI Desktop's own TMDL loader), which is why a broken measure shows
    as just "Name = DIVIDE(" with nothing after.
    """
    expr_lines = expression.splitlines() or [""]
    out = [f"{first_line_prefix}{expr_lines[0]}"]
    cont_prefix = "\t" * cont_tabs
    for cont in expr_lines[1:]:
        stripped = cont.strip()
        out.append(f"{cont_prefix}{stripped}" if stripped else "")
    return out


DATA_TYPE_MAP = {
    "int64": "int64", "double": "double", "string": "string",
    "dateTime": "dateTime", "boolean": "boolean", "decimal": "double",
}


def write_semantic_model(
    sm_dir: str,
    *,
    app_name: str,
    tables: dict[str, dict],       # table_name -> {"columns": [...], "m_expression": str}
    measures_by_table: dict[str, list[dict]],
    calculated_columns_by_table: dict[str, list[dict]],
    hierarchies_by_table: dict[str, list[dict]],
    relationships: list[dict],
    roles: list[dict],
    parameters: list[dict],
) -> None:
    defn_dir = os.path.join(sm_dir, "definition")
    tables_dir = os.path.join(defn_dir, "tables")
    os.makedirs(tables_dir, exist_ok=True)

    # Same as *.Report/.platform + definition.pbir — pbip-compiler doesn't
    # need these, but Power BI Desktop requires them to open the .pbip
    # project directly.
    write_platform_file(sm_dir, item_type="SemanticModel", display_name=app_name)
    with open(os.path.join(sm_dir, "definition.pbism"), "w", encoding="utf-8") as f:
        json.dump({"version": "4.0", "settings": {}}, f, indent=2)

    for table_name, table in tables.items():
        content = _render_table_tmdl(
            table_name,
            table.get("columns", []),
            measures_by_table.get(table_name, []),
            calculated_columns_by_table.get(table_name, []),
            hierarchies_by_table.get(table_name, []),
            table.get("m_expression", ""),
            table_is_hidden=table.get("is_hidden", False),
        )
        with open(os.path.join(tables_dir, f"{_safe(table_name)}.tmdl"), "w", encoding="utf-8") as f:
            f.write(content)

    if parameters:
        _write_parameters_table(tables_dir, parameters)

    with open(os.path.join(defn_dir, "relationships.tmdl"), "w", encoding="utf-8") as f:
        f.write(_render_relationships_tmdl(relationships))

    with open(os.path.join(defn_dir, "model.tmdl"), "w", encoding="utf-8") as f:
        f.write(_render_model_tmdl())

    if roles:
        with open(os.path.join(defn_dir, "roles.tmdl"), "w", encoding="utf-8") as f:
            f.write(_render_roles_tmdl(roles))


def _render_table_tmdl(
    name: str, columns: list[dict], measures: list[dict],
    calc_columns: list[dict], hierarchies: list[dict], m_expression: str,
    table_is_hidden: bool = False,
) -> str:
    lines = [f"table '{name}'", f"\tlineageTag: {_guid()}"]
    if table_is_hidden:
        lines.append("\tisHidden")
    lines.append("")

    for col in columns:
        dtype = DATA_TYPE_MAP.get(col.get("data_type", "string"), "string")
        lines.append(f"\tcolumn '{col['name']}'")
        lines.append(f"\t\tdataType: {dtype}")
        lines.append(f"\t\tlineageTag: {_guid()}")
        lines.append("\t\tsummarizeBy: none")
        lines.append(f"\t\tsourceColumn: {col.get('source_column', col['name'])}")
        if col.get("is_hidden"):
            lines.append("\t\tisHidden")
        lines.append("")
        lines.append("\t\tannotation SummarizationSetBy = Automatic")
        lines.append("")

    for col in calc_columns:
        expr = col.get("expression")
        if not expr:
            print(f"[build] WARNING: calculated column '{col['name']}' on table '{name}' has no expression — skipping it")
            continue
        lines.extend(_render_multiline_stmt(f"\tcolumn '{col['name']}' = ", expr, cont_tabs=2))
        lines.append("\t\tdataType: string")
        lines.append(f"\t\tlineageTag: {_guid()}")
        lines.append("\t\tsummarizeBy: none")
        lines.append("")

    for m in measures:
        expr = m.get("expression")
        if not expr:
            print(f"[build] WARNING: measure '{m['name']}' on table '{name}' has no expression — skipping it "
                  f"(a conversion step produced an incomplete measure; check converted/*.json for '{m['name']}')")
            continue
        lines.extend(_render_multiline_stmt(f"\tmeasure '{m['name']}' = ", expr, cont_tabs=2))
        if m.get("format_string"):
            lines.append(f"\t\tformatString: {m['format_string']}")
        if m.get("is_hidden"):
            lines.append("\t\tisHidden")
        lines.append(f"\t\tlineageTag: {_guid()}")
        lines.append("")

    for h in hierarchies:
        lines.append(f"\thierarchy '{h['name']}'")
        lines.append(f"\t\tlineageTag: {_guid()}")
        lines.append("")
        for level in h.get("levels", []):
            lines.append(f"\t\tlevel '{level['name']}'")
            lines.append(f"\t\t\tcolumn: {level['column']}")
            lines.append("")

    if m_expression:
        lines.append(f"\tpartition '{name}' = m")
        lines.append("\t\tmode: import")
        lines.append("\t\tsource =")
        lines.append(_indent_block(m_expression, tabs=3))
        lines.append("")

    return "\n".join(lines)


def _write_parameters_table(tables_dir: str, parameters: list[dict]) -> None:
    """Power Query parameters aren't real TMDL tables; emit them as a
    documented queries group the M step above can reference by name. Written
    as a comment-only .pq reference file since pbip-compiler's TMDL parser
    doesn't model parameters — Power BI Desktop's own Power Query editor is
    where these get turned into first-class parameters after opening the
    .pbip, so we keep the definitions alongside for that manual step."""
    lines = ["// Power Query parameters (add via Home > Manage Parameters in Power BI Desktop)", ""]
    for p in parameters:
        pq = p.get("power_query")
        if not pq:
            continue
        lines.append(f"// {p['name']}: type={pq.get('type')} default={pq.get('current_value')}")
    with open(os.path.join(tables_dir, "..", "parameters.pq.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def _render_relationships_tmdl(relationships: list[dict]) -> str:
    out = []
    for rel in relationships:
        out.append(f"relationship {_guid()}")
        out.append(f"\tfromTable: {rel['from_table']}")
        out.append(f"\tfromColumn: {rel['from_column']}")
        out.append(f"\ttoTable: {rel['to_table']}")
        out.append(f"\ttoColumn: {rel['to_column']}")
        if rel.get("is_active") is False:
            out.append("\tisActive: false")
        out.append("")
    return "\n".join(out)


def _render_model_tmdl() -> str:
    return (
        "model Model\n"
        "\tculture: en-US\n"
        "\tdefaultPowerBIDataSourceVersion: powerBI_V3\n"
        "\tsourceQueryCulture: en-US\n"
        "\tannotation PBIDesktopVersion: 2.130\n"
    )


def _render_roles_tmdl(roles: list[dict]) -> str:
    out = []
    for role in roles:
        out.append(f"role '{role['name']}'")
        out.append(f"\tmodelPermission: read")
        for perm in role.get("table_permissions", []):
            out.append("")
            out.extend(_render_multiline_stmt(f"\ttablePermission '{perm['table']}' = ", perm["filter"], cont_tabs=2))
        out.append("")
    return "\n".join(out)


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "_-. " else "_" for c in name)
